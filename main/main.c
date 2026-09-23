/**
 * @file main.c
 * @brief IIS3DWB Vibration Sensor Application for ESP32-S3
 *
 * This application reads vibration data from the IIS3DWB sensor
 * and monitors acceleration values with WiFi connectivity.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "led_strip.h"
#include "wifi_manager.h"
#include "config_manager.h"
#include "serial_protocol.h"
#include "sensor_streamer.h"
#include "iis3dwb.h"
#include "sdkconfig.h"

static const char *TAG = "MAIN";
static const char *TAG_WIFI = "WIFI_APP";
static const char *TAG_SENSOR = "IIS3DWB";

// IIS3DWB sensor handle
static iis3dwb_handle_t iis3dwb_handle;
static bool iis3dwb_initialized = false;

// ============================================================================
// 상태 LED (멀리서 육안 확인용)
// ----------------------------------------------------------------------------
// 하드웨어는 Kconfig(Status LED Configuration)로 선택:
//   · 기본  : 3핀 RGB LED GPIO 직결 (고객 센서 모듈, UV1 과 동일: R=8 G=3 B=46)
//             LOW 에서 켜짐(공통 애노드). GPIO46 은 리셋 시 풀다운이라 부팅 직후 파랑.
//   · WS2812: DevKitC 계열 RGB LED (GPIO48), led_strip 구동
//
// 상태 정의
//   대기 BOOT : 전원은 들어왔지만 아직 "정상 동작" 판정 전 (부팅·초기화·WiFi 연결 시도)
//               → 파랑 0.5초 깜빡임
//   정상 OK   : WiFi 연결됨 + 진동 데이터가 실제로 흐르는 중
//               → 초록 숨쉬기(2.5초 주기 페이드)
//   이상 FAULT: WiFi 실패/끊김, 센서 초기화 실패, 읽기 오류 반복, 3초 데이터 정지,
//               스트리밍 전송 오류 중 하나라도
//               → 노랑 0.25초 빠른 깜빡임
// ============================================================================
typedef enum { LED_STATE_BOOT = 0, LED_STATE_OK, LED_STATE_FAULT } led_state_t;

#define LED_BRIGHT        120   // WS2812 밝기 (0~255)
#define LED_DATA_STALE_MS 3000  // 이 시간 동안 새 데이터가 없으면 "정지"로 판정

static bool s_led_ready = false;
static volatile led_state_t s_led_state = LED_STATE_BOOT;
static volatile bool     s_sensor_init_failed = false;
static volatile TickType_t s_last_data_tick = 0;
static volatile uint32_t s_sensor_err_streak = 0;
static volatile bool     s_led_monitor_started = false;   // true 가 되면 OK/FAULT 판정 시작
static volatile TickType_t s_monitor_start_tick = 0;      // 감시 시작 시각 (데이터 유예 계산용)
static bool              s_streaming_mode = false;
// USB 직결(시리얼) 모드에서는 WiFi를 의도적으로 띄우지 않는다. 이때 WiFi 미연결을
// "이상"으로 판정하면 정상 스트리밍 중에도 LED가 계속 노랑이 되어 고장으로 오인된다.
// 이 플래그가 true 면 건강 판정에서 WiFi 항목을 아예 제외한다.
static bool              s_serial_mode = false;

#if CONFIG_STATUS_LED_WS2812
// ---------------- WS2812 (DevKit) ----------------
static led_strip_handle_t s_led_strip = NULL;
static void led_rgb(uint8_t r, uint8_t g, uint8_t b)
{
    if (!s_led_ready) return;
    // WS2812 는 매우 밝으므로 LED_BRIGHT 로 스케일
    led_strip_set_pixel(s_led_strip, 0, r * LED_BRIGHT / 255, g * LED_BRIGHT / 255, b * LED_BRIGHT / 255);
    led_strip_refresh(s_led_strip);
}
static bool led_hw_init(void)
{
    led_strip_config_t sc = { .strip_gpio_num = CONFIG_STATUS_LED_GPIO, .max_leds = 1 };
    led_strip_rmt_config_t rc = { .resolution_hz = 10 * 1000 * 1000 };
    if (led_strip_new_rmt_device(&sc, &rc, &s_led_strip) != ESP_OK) return false;
    s_led_ready = true;
    ESP_LOGI(TAG, "상태 LED: WS2812 (GPIO%d)", CONFIG_STATUS_LED_GPIO);
    ESP_LOGI(TAG, "LED 색 자가진단: 빨강 → 초록 → 파랑 → 꺼짐 (각 0.5초)");
    led_rgb(255, 0, 0); vTaskDelay(pdMS_TO_TICKS(500));
    led_rgb(0, 255, 0); vTaskDelay(pdMS_TO_TICKS(500));
    led_rgb(0, 0, 255); vTaskDelay(pdMS_TO_TICKS(500));
    led_rgb(0, 0, 0);   vTaskDelay(pdMS_TO_TICKS(500));
    return true;
}
#else
// ---------------- 3핀 RGB LED, GPIO 직결 + LEDC PWM (고객 모듈 · UV1 과 동일 핀) ----------------
// R=GPIO8, G=GPIO3, B=GPIO46 (기본). CONFIG_STATUS_LED_ACTIVE_LOW=y 면 LOW 가 켜짐.
// LEDC 로 밝기를 조절해 "숨쉬기(breathing)" 같은 부드러운 표현이 가능하다.
#include "driver/ledc.h"
#define LED_R CONFIG_STATUS_LED_RED_GPIO
#define LED_G CONFIG_STATUS_LED_GREEN_GPIO
#define LED_B CONFIG_STATUS_LED_BLUE_GPIO
#define LED_PWM_RES   LEDC_TIMER_10_BIT
#define LED_PWM_MAX   1023
static const ledc_channel_t s_led_ch[3] = { LEDC_CHANNEL_0, LEDC_CHANNEL_1, LEDC_CHANNEL_2 };

/** 0~255 밝기 → LEDC duty (극성 반영) */
static uint32_t led_duty(uint8_t level)
{
    uint32_t d = ((uint32_t)level * LED_PWM_MAX) / 255;
#if CONFIG_STATUS_LED_ACTIVE_LOW
    return LED_PWM_MAX - d;
#else
    return d;
#endif
}
static void led_rgb(uint8_t r, uint8_t g, uint8_t b)
{
    if (!s_led_ready) return;
    const uint8_t v[3] = { r, g, b };
    for (int i = 0; i < 3; i++) {
        ledc_set_duty(LEDC_LOW_SPEED_MODE, s_led_ch[i], led_duty(v[i]));
        ledc_update_duty(LEDC_LOW_SPEED_MODE, s_led_ch[i]);
    }
}
static bool led_hw_init(void)
{
    ledc_timer_config_t tc = {
        .speed_mode = LEDC_LOW_SPEED_MODE, .timer_num = LEDC_TIMER_0,
        .duty_resolution = LED_PWM_RES, .freq_hz = 5000, .clk_cfg = LEDC_AUTO_CLK,
    };
    if (ledc_timer_config(&tc) != ESP_OK) return false;
    const int pins[3] = { LED_R, LED_G, LED_B };
    for (int i = 0; i < 3; i++) {
        gpio_reset_pin(pins[i]);
        ledc_channel_config_t cc = {
            .gpio_num = pins[i], .speed_mode = LEDC_LOW_SPEED_MODE,
            .channel = s_led_ch[i], .timer_sel = LEDC_TIMER_0,
            .duty = led_duty(0), .hpoint = 0,
        };
        if (ledc_channel_config(&cc) != ESP_OK) return false;
    }
    s_led_ready = true;
    led_rgb(0, 0, 0);
    ESP_LOGI(TAG, "상태 LED: 3핀 RGB(PWM)  R=GPIO%d G=GPIO%d B=GPIO%d (%s 에서 켜짐)",
             LED_R, LED_G, LED_B, CONFIG_STATUS_LED_ACTIVE_LOW ? "LOW" : "HIGH");
    ESP_LOGI(TAG, "LED 자가진단: 빨강 → 초록 → 파랑 → 꺼짐 (각 0.5초)");
    led_rgb(255, 0, 0); vTaskDelay(pdMS_TO_TICKS(500));
    led_rgb(0, 255, 0); vTaskDelay(pdMS_TO_TICKS(500));
    led_rgb(0, 0, 255); vTaskDelay(pdMS_TO_TICKS(500));
    led_rgb(0, 0, 0);   vTaskDelay(pdMS_TO_TICKS(500));
    return true;
}
#endif

/** 센서 데이터 정상 수신 보고 (읽기 태스크에서 호출). */
static inline void led_report_data_ok(void)
{
    s_last_data_tick = xTaskGetTickCount();
    s_sensor_err_streak = 0;
}
/** 센서 읽기 오류 보고. */
static inline void led_report_data_error(void) { s_sensor_err_streak++; }

/**
 * LED 상태 태스크 — 20ms 마다 표시를 갱신하고, 0.5초마다 상태를 판정한다.
 *   BOOT : 파랑 0.5초 깜빡임            (찾는 중)
 *   OK   : 초록 숨쉬기(2.5초 주기 페이드) (차분하게 살아 있음)
 *   FAULT: 노랑 0.25초 빠른 깜빡임       (긴급)
 * 색을 못 가려도 리듬(숨쉬기 vs 빠른 깜빡임)으로 정상/이상이 구분된다.
 */
#define LED_TICK_MS        20
#define LED_BREATH_MS      2500   // 정상: 숨쉬기 한 주기
#define LED_BREATH_MIN     12     // 숨쉬기 최저 밝기(0~255) — 완전히 꺼지지 않게
#define LED_WAIT_BLINK_MS  500    // 대기: 깜빡 반주기
#define LED_FAULT_BLINK_MS 250    // 이상: 깜빡 반주기

static void led_status_task(void *pv)
{
    led_state_t prev = LED_STATE_BOOT;
    uint32_t prev_packets = 0, prev_errors = 0;
    TickType_t last_packet_tick = xTaskGetTickCount();
    uint32_t t_ms = 0;

    while (1) {
        // ---- 0.5초마다 판정 ----
        if (t_ms % 500 == 0) {
            led_state_t st = LED_STATE_BOOT;
            if (s_led_monitor_started) {
                TickType_t now = xTaskGetTickCount();
                bool wifi_ok = wifi_manager_is_connected();
                bool data_ok, tx_error = false;
                if (s_streaming_mode) {
                    sensor_streamer_stats_t s;
                    sensor_streamer_get_stats(&s);
                    if (s.packets_sent != prev_packets) last_packet_tick = now;
                    tx_error = (s.send_errors != prev_errors);
                    prev_packets = s.packets_sent; prev_errors = s.send_errors;
                    data_ok = (now - last_packet_tick) < pdMS_TO_TICKS(LED_DATA_STALE_MS);
                } else {
                    data_ok = (now - s_last_data_tick) < pdMS_TO_TICKS(LED_DATA_STALE_MS)
                              && s_sensor_err_streak < 3;
                }
                // 감시 시작 직후 유예: 첫 데이터가 도착하기 전에는 데이터 부재를 이상으로 보지 않음
                if ((now - s_monitor_start_tick) < pdMS_TO_TICKS(LED_DATA_STALE_MS)) data_ok = true;
                // 시리얼 모드는 WiFi를 쓰지 않으므로 WiFi 항목을 판정에서 제외한다.
                bool wifi_fault = (!s_serial_mode && !wifi_ok);
                bool fault = s_sensor_init_failed || wifi_fault || !data_ok || tx_error;
                st = fault ? LED_STATE_FAULT : LED_STATE_OK;
                if (st != prev) {
                    if (st == LED_STATE_OK)
                        ESP_LOGI(TAG, "LED 상태: 정상(초록 숨쉬기) — %s진동 데이터 정상",
                                 s_serial_mode ? "USB 직결 + " : "WiFi 연결 + ");
                    else if (s_serial_mode)
                        // WiFi는 원인이 될 수 없으므로 아예 표시하지 않는다 (오진 방지).
                        ESP_LOGW(TAG, "LED 상태: 이상(노랑 빠른 깜빡임) — 센서초기화실패=%d 데이터=%d 전송오류=%d (USB 직결 — WiFi 무관)",
                                 s_sensor_init_failed, data_ok, tx_error);
                    else
                        ESP_LOGW(TAG, "LED 상태: 이상(노랑 빠른 깜빡임) — 센서초기화실패=%d WiFi=%d 데이터=%d 전송오류=%d",
                                 s_sensor_init_failed, wifi_ok, data_ok, tx_error);
                }
            }
            if (st != prev) { prev = st; t_ms = 0; }
            s_led_state = st;
        }

        // ---- 20ms 마다 렌더 ----
        switch (s_led_state) {
        case LED_STATE_OK: {
            // 코사인 숨쉬기 + 감마(제곱)로 눈에 자연스럽게
            float ph = (float)(t_ms % LED_BREATH_MS) / LED_BREATH_MS;
            float v  = 0.5f - 0.5f * cosf(2.0f * 3.14159265f * ph);   // 0..1
            v = v * v;
            uint8_t g = LED_BREATH_MIN + (uint8_t)(v * (255 - LED_BREATH_MIN));
            led_rgb(0, g, 0);
            break;
        }
        case LED_STATE_FAULT:
            if ((t_ms / LED_FAULT_BLINK_MS) & 1) led_rgb(0, 0, 0); else led_rgb(255, 170, 0);
            break;
        default:
            if ((t_ms / LED_WAIT_BLINK_MS) & 1) led_rgb(0, 0, 0); else led_rgb(0, 0, 255);
            break;
        }
        vTaskDelay(pdMS_TO_TICKS(LED_TICK_MS));
        t_ms += LED_TICK_MS;
    }
}

/** 상태 LED 초기화 + 자가진단 + 상태 태스크 시작. 실패해도 앱은 계속. */
static void led_init(void)
{
    if (!led_hw_init()) {
        ESP_LOGW(TAG, "상태 LED 초기화 실패 — LED 없이 계속 진행");
        return;
    }
    ESP_LOGI(TAG, "LED 상태: 대기(파랑 깜빡임) — 전원 ON, 정상 동작 확인 전");
    xTaskCreate(led_status_task, "led_status", 3072, NULL, 3, NULL);
}

// ANSI Color codes for terminal output
#define COLOR_RESET   "\033[0m"
#define COLOR_RED     "\033[1;31m"
#define COLOR_GREEN   "\033[1;32m"
#define COLOR_YELLOW  "\033[1;33m"
#define COLOR_BLUE    "\033[1;34m"
#define COLOR_MAGENTA "\033[1;35m"
#define COLOR_CYAN    "\033[1;36m"
#define COLOR_WHITE   "\033[1;37m"
#define COLOR_BG_GREEN  "\033[42m"
#define COLOR_BG_RED    "\033[41m"


// ============================================================================
// IIS3DWB Sensor Functions
// ============================================================================

/**
 * @brief Get full-scale string for display
 */
static const char* get_fullscale_string(iis3dwb_fs_xl_t fs)
{
    switch (fs) {
        case IIS3DWB_FS_2G:  return "±2g";
        case IIS3DWB_FS_4G:  return "±4g";
        case IIS3DWB_FS_8G:  return "±8g";
        case IIS3DWB_FS_16G: return "±16g";
        default: return "Unknown";
    }
}

/* 사람이 읽는 g 값(2/4/8/16) → IIS3DWB 레지스터 코드값.
 * 코드값은 순서가 직관과 어긋나므로(±16g가 0x04) 변환은 이 한 곳에만 둔다. */
static iis3dwb_fs_xl_t fs_g_to_code(uint8_t g)
{
    switch (g) {
        case 2:  return IIS3DWB_FS_2G;
        case 4:  return IIS3DWB_FS_4G;
        case 8:  return IIS3DWB_FS_8G;
        case 16: return IIS3DWB_FS_16G;
        default: return IIS3DWB_FS_4G;   /* 알 수 없는 값은 기본 ±4g */
    }
}

/**
 * @brief Initialize IIS3DWB vibration sensor
 *
 * @param full_scale_g 측정 범위 (2/4/8/16 g). NVS 설정이 없으면 4를 넘긴다.
 */
static esp_err_t init_iis3dwb_sensor(uint8_t full_scale_g)
{
    ESP_LOGI(TAG_SENSOR, "Initializing IIS3DWB vibration sensor...");

    iis3dwb_config_t config = {
        .spi_host = CONFIG_IIS3DWB_SPI_HOST,
        .mosi_io_num = CONFIG_IIS3DWB_SPI_MOSI_GPIO,
        .miso_io_num = CONFIG_IIS3DWB_SPI_MISO_GPIO,
        .sclk_io_num = CONFIG_IIS3DWB_SPI_SCLK_GPIO,
        .cs_io_num = CONFIG_IIS3DWB_SPI_CS_GPIO,
        .clk_speed_hz = CONFIG_IIS3DWB_SPI_FREQ_HZ,
        .full_scale = fs_g_to_code(full_scale_g),
        .bandwidth = (iis3dwb_bw_xl_t)CONFIG_IIS3DWB_BANDWIDTH,
    };

    ESP_LOGI(TAG_SENSOR, "SPI Configuration:");
    ESP_LOGI(TAG_SENSOR, "  Host: SPI%d", config.spi_host);
    ESP_LOGI(TAG_SENSOR, "  MOSI: GPIO%d, MISO: GPIO%d", config.mosi_io_num, config.miso_io_num);
    ESP_LOGI(TAG_SENSOR, "  SCLK: GPIO%d, CS: GPIO%d", config.sclk_io_num, config.cs_io_num);
    ESP_LOGI(TAG_SENSOR, "  Clock: %d Hz", config.clk_speed_hz);
    ESP_LOGI(TAG_SENSOR, "  Full-scale: %s", get_fullscale_string(config.full_scale));

    esp_err_t ret = iis3dwb_init(&config, &iis3dwb_handle);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG_SENSOR, "Sensor initialization failed: %s", esp_err_to_name(ret));
        printf("\n");
        printf(COLOR_BG_RED COLOR_WHITE "  ✗ IIS3DWB Sensor Init Failed!  " COLOR_RESET "\n");
        printf(COLOR_RED "  Check SPI wiring and sensor connection" COLOR_RESET "\n");
        printf("\n");
        return ret;
    }

    // Enable accelerometer
    ret = iis3dwb_enable(&iis3dwb_handle, true);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG_SENSOR, "Failed to enable accelerometer: %s", esp_err_to_name(ret));
        return ret;
    }

    iis3dwb_initialized = true;

    // Display success message
    printf("\n");
    printf(COLOR_BG_GREEN COLOR_WHITE "  ★ IIS3DWB Sensor Ready!  " COLOR_RESET "\n");
    printf(COLOR_GREEN "  ┌─────────────────────────────────┐" COLOR_RESET "\n");
    printf(COLOR_GREEN "  │" COLOR_RESET " Full-scale: " COLOR_CYAN "%-19s" COLOR_RESET COLOR_GREEN " │" COLOR_RESET "\n",
           get_fullscale_string(config.full_scale));
    printf(COLOR_GREEN "  │" COLOR_RESET " ODR: " COLOR_YELLOW "26.667 kHz" COLOR_RESET "              " COLOR_GREEN " │" COLOR_RESET "\n");
    printf(COLOR_GREEN "  │" COLOR_RESET " Read interval: " COLOR_MAGENTA "%d ms" COLOR_RESET,
           CONFIG_IIS3DWB_READ_INTERVAL_MS);
    // Padding based on interval length
    int interval_len = (CONFIG_IIS3DWB_READ_INTERVAL_MS >= 1000) ? 4 :
                       (CONFIG_IIS3DWB_READ_INTERVAL_MS >= 100) ? 3 :
                       (CONFIG_IIS3DWB_READ_INTERVAL_MS >= 10) ? 2 : 1;
    for (int i = 0; i < (14 - interval_len); i++) printf(" ");
    printf(COLOR_GREEN "│" COLOR_RESET "\n");
    printf(COLOR_GREEN "  └─────────────────────────────────┘" COLOR_RESET "\n");
    printf("\n");

    return ESP_OK;
}

/**
 * @brief IIS3DWB sensor reading task
 */
static void iis3dwb_read_task(void *pvParameters)
{
    iis3dwb_accel_data_t accel;
    float temperature;
    uint32_t sample_count = 0;

    ESP_LOGI(TAG_SENSOR, "Starting vibration monitoring task...");
    ESP_LOGI(TAG_SENSOR, "============================================");

    while (1) {
        if (!iis3dwb_initialized) {
            vTaskDelay(pdMS_TO_TICKS(1000));
            continue;
        }

        sample_count++;

        // Read acceleration data
        esp_err_t ret = iis3dwb_read_accel_data(&iis3dwb_handle, &accel);
        if (ret == ESP_OK) {
            // Calculate total acceleration magnitude
            float magnitude = sqrtf(accel.x_mg * accel.x_mg +
                                    accel.y_mg * accel.y_mg +
                                    accel.z_mg * accel.z_mg);

            ESP_LOGI(TAG_SENSOR, "[%lu] Accel: X=%+8.2f mg, Y=%+8.2f mg, Z=%+8.2f mg | Mag=%.2f mg",
                     sample_count, accel.x_mg, accel.y_mg, accel.z_mg, magnitude);

            // 데이터 정상 수신 → LED 상태 감시 태스크에 보고 (정상이면 초록 상시 점등)
            led_report_data_ok();

            // Alert for high vibration (magnitude > 2000 mg = 2g)
            if (magnitude > 2000.0f) {
                ESP_LOGW(TAG_SENSOR, "  ⚠ High vibration detected! (%.2f mg)", magnitude);
            }
        } else {
            ESP_LOGE(TAG_SENSOR, "Failed to read acceleration data: %s", esp_err_to_name(ret));
            led_report_data_error();     // 반복되면 LED 노랑(이상)
        }

        // Read temperature periodically (every 10 samples)
        if (sample_count % 10 == 0) {
            ret = iis3dwb_read_temperature(&iis3dwb_handle, &temperature);
            if (ret == ESP_OK) {
                ESP_LOGI(TAG_SENSOR, "  Temperature: %.1f °C", temperature);
            }
        }

        vTaskDelay(pdMS_TO_TICKS(CONFIG_IIS3DWB_READ_INTERVAL_MS));
    }
}

// ============================================================================
// WiFi Initialization (optional, for monitoring)
// ============================================================================

/**
 * @brief WiFi 초기화
 *
 * 제품화 구조: WiFi 자격증명은 NVS(ROM) 저장값을 우선 사용합니다.
 *
 *  - NVS 저장 설정 있음 → 해당 SSID로 연결 (파이썬 설정 툴 주입값)
 *  - NVS 저장 설정 없음 → menuconfig 값(CONFIG_WIFI_SSID/PASSWORD)으로 폴백
 *
 * @return ESP_OK 연결 성공, 그 외 연결 실패
 */
static esp_err_t init_wifi(void)
{
    // Always show MAC address first (available before WiFi connection)
    uint8_t mac[6];
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
    printf("\n");
    printf(COLOR_CYAN "  ┌─────────────────────────────────┐" COLOR_RESET "\n");
    printf(COLOR_CYAN "  │" COLOR_RESET " MAC : " COLOR_MAGENTA "%02X:%02X:%02X:%02X:%02X:%02X" COLOR_RESET "         " COLOR_CYAN "│" COLOR_RESET "\n",
           mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    printf(COLOR_CYAN "  └─────────────────────────────────┘" COLOR_RESET "\n");

    // ────────────────────────────────────────────────────────────────
    // WiFi 자격증명 출처는 빌드 옵션(CONFIG_WIFI_PREFER_NVS)으로 결정한다.
    //
    //  · 기본(n) — flash/monitor 개발용:
    //      NVS를 보지 않고 Kconfig 값(CONFIG_WIFI_SSID/PASSWORD)으로 곧바로 연결.
    //      config-tool의 NVS 주입과 완전 독립 (UV1 스타일).
    //
    //  · CONFIG_WIFI_PREFER_NVS=y — 고객 배포용:
    //      NVS(devcfg)에 저장된 값이 있으면 그것으로, 없으면 Kconfig 값으로 폴백.
    //      운영: flash(배포툴) → NVS 주입 → 파워 온 시 자동 연결.
    // ────────────────────────────────────────────────────────────────
    const char *ssid = CONFIG_WIFI_SSID;
    const char *password = CONFIG_WIFI_PASSWORD;

#if CONFIG_WIFI_PREFER_NVS
    config_wifi_cred_t nvs_cred;
    if (config_manager_has_wifi() &&
        config_manager_load_wifi(&nvs_cred) == ESP_OK) {
        ssid = nvs_cred.ssid;
        password = nvs_cred.password;
        ESP_LOGI(TAG_WIFI, "WiFi 자격증명 출처: NVS(devcfg) — 설정 툴 주입값");
    } else {
        ESP_LOGI(TAG_WIFI, "WiFi 자격증명 출처: Kconfig 기본값 (NVS 미설정)");
    }
#else
    ESP_LOGI(TAG_WIFI, "WiFi 자격증명 출처: Kconfig (개발 빌드 — NVS 무시)");
#endif

    wifi_manager_config_t wifi_config = {
        .ssid = ssid,
        .password = password,
        .max_retry = CONFIG_WIFI_MAXIMUM_RETRY,
        .auth_mode_threshold = CONFIG_WIFI_SCAN_AUTH_MODE_THRESHOLD,
        .auto_connect = true,
    };

    ESP_LOGI(TAG_WIFI, "Connecting to: %s", ssid);

    esp_err_t ret = wifi_manager_init(&wifi_config);
    if (ret != ESP_OK) {
        printf("\n");
        printf(COLOR_BG_RED COLOR_WHITE "  ✗ WiFi Init Failed!  " COLOR_RESET "\n");
        printf("\n");
        return ret;
    }

    ret = wifi_manager_wait_for_connection(30000);
    if (ret != ESP_OK) {
        printf("\n");
        printf(COLOR_BG_RED COLOR_WHITE "  ✗ WiFi Connection Failed!  " COLOR_RESET "\n");
        printf(COLOR_RED "  Could not connect to: %s" COLOR_RESET "\n", ssid);
        printf("\n");
        return ret;
    }

    char ip_str[16];
    if (wifi_manager_get_ip_string(ip_str, sizeof(ip_str)) == ESP_OK) {
        // Green background for WiFi success - highly visible
        printf("\n");
        printf(COLOR_BG_GREEN COLOR_WHITE "  ★ WiFi Connected Successfully!  " COLOR_RESET "\n");
        printf(COLOR_GREEN "  ┌─────────────────────────────────┐" COLOR_RESET "\n");
        printf(COLOR_GREEN "  │" COLOR_RESET " SSID: " COLOR_CYAN "%-24s" COLOR_RESET COLOR_GREEN " │" COLOR_RESET "\n", ssid);
        printf(COLOR_GREEN "  │" COLOR_RESET " IP  : " COLOR_YELLOW "%-24s" COLOR_RESET COLOR_GREEN " │" COLOR_RESET "\n", ip_str);
        printf(COLOR_GREEN "  │" COLOR_RESET " MAC : " COLOR_MAGENTA "%02X:%02X:%02X:%02X:%02X:%02X" COLOR_RESET "         " COLOR_GREEN "│" COLOR_RESET "\n",
               mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
        printf(COLOR_GREEN "  └─────────────────────────────────┘" COLOR_RESET "\n");
        printf("\n");
    }

    return ESP_OK;
}

// ============================================================================
// Main Application
// ============================================================================

void app_main(void)
{
    ESP_LOGI(TAG, "========================================");
    ESP_LOGI(TAG, "IIS3DWB Vibration Sensor Application");
    ESP_LOGI(TAG, "========================================");
    ESP_LOGI(TAG, "");

    // ===== 온보드 RGB LED 초기화 (부팅 직후) =====
    led_init();

    // ===== Config Manager Initialization (NVS) =====
    // WiFi 자격증명 등 영구 설정을 ROM에서 읽기 위해 가장 먼저 초기화
    ESP_LOGI(TAG, "Step 0: Config Manager (NVS) Initialization");
    esp_err_t ret = config_manager_init();
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Config manager 초기화 실패: %s", esp_err_to_name(ret));
    }

    // ===== Serial Protocol Task =====
    // PC 설정 툴 명령을 항상 수신할 수 있도록 백그라운드 태스크 시작
    // (WiFi 연결 여부와 무관하게 동작 — 동작 중 WiFi 재설정 가능)
    ESP_LOGI(TAG, "Step 0.5: Starting Serial Config Protocol");
    serial_protocol_start();

    // ===== 스트리밍 설정 선(先)판독 =====
    // 전송 방식을 먼저 확인한다. USB 직결이면 WiFi를 아예 띄우지 않아
    // 부팅이 빨라지고(최대 60초 연결 대기 제거) 소비전력도 준다.
    config_stream_t scfg = {0};
    bool has_stream = config_manager_has_stream();
    if (has_stream) {
        if (config_manager_load_stream(&scfg) != ESP_OK) {
            has_stream = false;
        }
    }
    bool serial_mode = (has_stream && scfg.transport == 2);

    // ===== WiFi Initialization =====
    // NVS에 저장된 설정으로 연결. 설정 없으면 설정 대기 모드.
    // 시리얼(USB 직결) 모드에서는 WiFi가 전혀 필요 없으므로 통째로 건너뛴다.
    if (!serial_mode) {
        ESP_LOGI(TAG, "Step 1: WiFi Initialization");
        ret = init_wifi();
        if (ret == ESP_ERR_NOT_FOUND) {
            ESP_LOGW(TAG_WIFI, "WiFi 미설정 — 설정 툴 대기 중 (센서 기능은 계속 동작)");
        } else if (ret != ESP_OK) {
            ESP_LOGW(TAG_WIFI, "WiFi 연결 실패 — WiFi 없이 계속 진행");
        } else {
            ESP_LOGI(TAG, "WiFi connected successfully!");
        }
    } else {
        ESP_LOGI(TAG, "Step 1: (USB 직결 모드 — WiFi 초기화 생략)");
    }

    vTaskDelay(pdMS_TO_TICKS(1000));

    // ===== IIS3DWB Sensor Initialization =====
    ESP_LOGI(TAG, "");
    ESP_LOGI(TAG, "Step 2: IIS3DWB Sensor Initialization");
    // NVS 설정이 있으면 저장된 측정 범위를, 없으면 기본 ±4g 를 적용한다.
    ret = init_iis3dwb_sensor(has_stream ? scfg.full_scale_g : 4);
    if (ret != ESP_OK) {
        ESP_LOGW(TAG_SENSOR, "Sensor init failed, continuing without sensor...");
        s_sensor_init_failed = true;     // → LED 노랑(이상)
    } else {
        ESP_LOGI(TAG, "IIS3DWB sensor initialized successfully!");
    }

    vTaskDelay(pdMS_TO_TICKS(500));

    // 스트리밍 활성 여부 미리 판단 (스트리밍이면 모니터링 태스크와 센서 경합 방지)
    // 시리얼 모드는 WiFi 연결이 필요 없다.
    bool streaming_active = (iis3dwb_initialized && has_stream
                             && (serial_mode || wifi_manager_is_connected()));

    // ===== Start IIS3DWB Sensor Reading Task =====
    if (iis3dwb_initialized && !streaming_active) {
        // 스트리밍이 아닐 때만 모니터링 태스크 가동 (FIFO와 SPI/센서 경합 방지)
        ESP_LOGI(TAG, "");
        ESP_LOGI(TAG, "Step 3: Starting Vibration Monitoring");
        xTaskCreate(iis3dwb_read_task, "iis3dwb_read", 4096, NULL, 5, NULL);
    } else if (iis3dwb_initialized && streaming_active) {
        ESP_LOGI(TAG, "Step 3: (스트리밍 모드 — 모니터링 태스크 생략, 센서 경합 방지)");
    } else {
        // 센서 초기화 실패 → 대기(LED 노랑=이상). GPIO 테스트로 빠지지 않고 원인을 알린다.
        printf("\n");
        printf(COLOR_BG_RED COLOR_WHITE "  ✗ 센서를 찾을 수 없습니다  " COLOR_RESET "\n");
        printf(COLOR_RED  "  IIS3DWB WHO_AM_I 응답 없음 — SPI 배선/전원/센서 실장을 확인하세요." COLOR_RESET "\n");
        printf(COLOR_YELLOW "  이 디바이스는 정상 동작할 수 없습니다. 하드웨어 점검이 필요합니다." COLOR_RESET "\n\n");
        ESP_LOGE(TAG_SENSOR, "센서 없음 — 진동 측정 불가 (LED 노랑으로 이상 표시)");
    }

    // ===== LED 상태 감시 시작 (여기부터 파랑→초록/노랑으로 판정) =====
    s_streaming_mode = streaming_active;
    s_serial_mode = serial_mode;                 // WiFi 미연결을 이상으로 보지 않게
    s_last_data_tick = xTaskGetTickCount();      // 판정 유예(첫 데이터 대기)
    s_monitor_start_tick = s_last_data_tick;
    s_led_monitor_started = true;

    // ===== Step 4: 센서 데이터 스트리밍 =====
    // 조건: 센서 정상 + 스트리밍 설정 존재 + (시리얼 모드이거나 WiFi 연결됨)
    // 시리얼 모드는 WiFi 연결이 필요 없다.
    bool can_stream = iis3dwb_initialized && has_stream &&
                      (serial_mode || wifi_manager_is_connected());

    if (can_stream) {
        /* 시리얼(USB 직결) 모드의 설정 복구는 PC 설정 툴이 esptool 경로로
         * 수행한다(NVS 바이너리를 직접 플래시). 이때 칩은 ROM 부트로더로
         * 진입하므로 앱이 무엇을 하고 있든 무관하며, 부팅 직후의 대기 창도
         * 필요 없다. 시리얼 프로토콜 명령에는 스트리밍 설정을 바꾸는 것이
         * 없으므로 여기서 시간을 벌어봐야 되돌릴 수 있는 것도 없다. */

        ESP_LOGI(TAG, "");
        ESP_LOGI(TAG, "Step 4: 센서 데이터 스트리밍 시작");
        sensor_streamer_config_t st = {
            .sensor       = &iis3dwb_handle,
            .server_ip    = scfg.server_ip,
            .server_port  = scfg.server_port,
            .rate_step    = scfg.rate_step,
            .transport    = (stream_transport_type_t)scfg.transport,
            .read_mode    = scfg.read_mode,
            .full_scale_g = scfg.full_scale_g,
        };
        esp_err_t sret = sensor_streamer_start(&st);
        if (sret == ESP_OK) {
            /* 시리얼 모드에서는 printf 출력이 USB 패킷 스트림에 그대로 섞여
             * 앞쪽 패킷을 깨뜨린다(로그 레벨 NONE 은 printf 를 막지 못함).
             * 따라서 이 안내 배너는 WiFi 모드에서만 출력한다. */
            if (!serial_mode) {
                printf("\n");
                printf(COLOR_BG_GREEN COLOR_WHITE "  📡 센서 데이터 스트리밍 중!  " COLOR_RESET "\n");
                printf(COLOR_GREEN "  → %s:%u (%lu Hz)" COLOR_RESET "\n",
                       scfg.server_ip, scfg.server_port,
                       sensor_streamer_rate_hz(scfg.rate_step));
                printf("\n");
            }
        } else {
            ESP_LOGE(TAG, "스트리밍 시작 실패: %s", esp_err_to_name(sret));
        }
    } else {
        ESP_LOGI(TAG, "스트리밍 비활성 (센서/WiFi/서버설정 중 하나 미충족)");
    }

    // ===== Main Loop (스트리밍 통계 주기 출력) =====
    while (1) {
        vTaskDelay(pdMS_TO_TICKS(10000));
        sensor_streamer_stats_t st;
        sensor_streamer_get_stats(&st);
        if (st.packets_sent > 0 || st.dropped > 0) {
            /* FIFO오버런: 센서가 오래된 샘플을 덮어쓴 횟수. 드롭(링버퍼 거부)과
             * 다른 경로의 유실이므로 함께 봐야 한다 — 이 값이 0 이 아니면
             * "드롭=0" 이어도 샘플을 잃고 있다. */
            ESP_LOGI(TAG, "[스트리밍] 패킷=%lu 샘플=%lu 드롭=%lu 에러=%lu INT=%lu FIFO오버런=%lu",
                     st.packets_sent, st.samples_sent, st.dropped, st.send_errors,
                     st.int_count, st.fifo_overrun);
        }
    }
}
