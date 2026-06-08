/**
 * @file main.c
 * @brief IIS3DWB Vibration Sensor Application for ESP32-S3
 *
 * This application reads vibration data from the IIS3DWB sensor
 * and monitors acceleration values with WiFi connectivity.
 */

#include <stdio.h>
#include <string.h>
#include <math.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "esp_mac.h"
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

#if 1  // GPIO Test Code - Enabled
// ============================================================================
// GPIO Test Code (Disabled)
// ============================================================================
// GPIO interrupt state tracking
static volatile uint8_t ex2_isr_state[5] = {0};  // Current state from ISR
static volatile uint8_t ex4_isr_state[5] = {0};  // Current state from ISR
static volatile bool ex2_isr_triggered[5] = {false};
static volatile bool ex4_isr_triggered[5] = {false};

// ============================================================================
// GPIO Pin Definitions (from pinmap.png)
// ============================================================================

// EX1 - Output pins (connected to EX2 via LAN cable)
#define EX1_PIN1    GPIO_NUM_4
#define EX1_PIN2    GPIO_NUM_11
#define EX1_PIN3    GPIO_NUM_12
#define EX1_PIN4    GPIO_NUM_13
#define EX1_PIN5    GPIO_NUM_14

// EX2 - Input pins (receives from EX1)
#define EX2_PIN1    GPIO_NUM_6
#define EX2_PIN2    GPIO_NUM_7
#define EX2_PIN3    GPIO_NUM_15
#define EX2_PIN4    GPIO_NUM_17
#define EX2_PIN5    GPIO_NUM_18

// EX3 - Output pins (connected to EX4 via LAN cable)
#define EX3_PIN1    GPIO_NUM_45
#define EX3_PIN2    GPIO_NUM_48
#define EX3_PIN3    GPIO_NUM_47
#define EX3_PIN4    GPIO_NUM_9
#define EX3_PIN5    GPIO_NUM_10

// EX4 - Input pins (receives from EX3)
#define EX4_PIN1    GPIO_NUM_1
#define EX4_PIN2    GPIO_NUM_2
#define EX4_PIN3    GPIO_NUM_42
#define EX4_PIN4    GPIO_NUM_41
#define EX4_PIN5    GPIO_NUM_40

// Pin arrays for easier iteration
static const gpio_num_t ex1_pins[] = {EX1_PIN1, EX1_PIN2, EX1_PIN3, EX1_PIN4, EX1_PIN5};
static const gpio_num_t ex2_pins[] = {EX2_PIN1, EX2_PIN2, EX2_PIN3, EX2_PIN4, EX2_PIN5};
static const gpio_num_t ex3_pins[] = {EX3_PIN1, EX3_PIN2, EX3_PIN3, EX3_PIN4, EX3_PIN5};
static const gpio_num_t ex4_pins[] = {EX4_PIN1, EX4_PIN2, EX4_PIN3, EX4_PIN4, EX4_PIN5};

#define NUM_PINS    5

// ============================================================================
// GPIO Configuration
// ============================================================================

/**
 * @brief Configure output GPIOs (EX1, EX3)
 */
static void configure_output_pins(void)
{
    ESP_LOGI(TAG, "Configuring EX1 output pins...");
    for (int i = 0; i < NUM_PINS; i++) {
        gpio_reset_pin(ex1_pins[i]);
        gpio_set_direction(ex1_pins[i], GPIO_MODE_OUTPUT);
        gpio_set_level(ex1_pins[i], 0);
        ESP_LOGI(TAG, "  EX1 PIN%d: GPIO%d -> OUTPUT", i + 1, ex1_pins[i]);
    }

    ESP_LOGI(TAG, "Configuring EX3 output pins...");
    for (int i = 0; i < NUM_PINS; i++) {
        gpio_reset_pin(ex3_pins[i]);
        gpio_set_direction(ex3_pins[i], GPIO_MODE_OUTPUT);
        gpio_set_level(ex3_pins[i], 0);
        ESP_LOGI(TAG, "  EX3 PIN%d: GPIO%d -> OUTPUT", i + 1, ex3_pins[i]);
    }
}

/**
 * @brief GPIO ISR handler for EX2 pins
 */
static void IRAM_ATTR ex2_gpio_isr_handler(void *arg)
{
    uint32_t pin_index = (uint32_t)arg;
    if (pin_index < NUM_PINS) {
        ex2_isr_state[pin_index] = gpio_get_level(ex2_pins[pin_index]);
        ex2_isr_triggered[pin_index] = true;
    }
}

/**
 * @brief GPIO ISR handler for EX4 pins
 */
static void IRAM_ATTR ex4_gpio_isr_handler(void *arg)
{
    uint32_t pin_index = (uint32_t)arg;
    if (pin_index < NUM_PINS) {
        ex4_isr_state[pin_index] = gpio_get_level(ex4_pins[pin_index]);
        ex4_isr_triggered[pin_index] = true;
    }
}

/**
 * @brief Configure input GPIOs (EX2, EX4) with GPIO interrupts
 * As per dev.png specification: "EX2,4의 GPIO핀을 입력으로 설정(GPIO인터럽트를 이용할것)"
 */
static void configure_input_pins(void)
{
    // Install GPIO ISR service
    esp_err_t ret = gpio_install_isr_service(0);
    if (ret != ESP_OK && ret != ESP_ERR_INVALID_STATE) {
        ESP_LOGE(TAG, "Failed to install GPIO ISR service: %s", esp_err_to_name(ret));
        return;
    }
    ESP_LOGI(TAG, "GPIO ISR service installed");

    ESP_LOGI(TAG, "Configuring EX2 input pins with GPIO interrupt...");
    for (int i = 0; i < NUM_PINS; i++) {
        gpio_reset_pin(ex2_pins[i]);

        gpio_config_t io_conf = {
            .pin_bit_mask = (1ULL << ex2_pins[i]),
            .mode = GPIO_MODE_INPUT,
            .pull_up_en = GPIO_PULLUP_DISABLE,
            .pull_down_en = GPIO_PULLDOWN_ENABLE,
            .intr_type = GPIO_INTR_ANYEDGE,  // Trigger on both rising and falling edge
        };
        ret = gpio_config(&io_conf);
        if (ret != ESP_OK) {
            ESP_LOGE(TAG, "  EX2 PIN%d: GPIO%d config FAILED (%s)",
                     i + 1, ex2_pins[i], esp_err_to_name(ret));
            continue;
        }

        // Add ISR handler for this pin
        ret = gpio_isr_handler_add(ex2_pins[i], ex2_gpio_isr_handler, (void *)(uint32_t)i);
        if (ret != ESP_OK) {
            ESP_LOGE(TAG, "  EX2 PIN%d: GPIO%d ISR add FAILED (%s)",
                     i + 1, ex2_pins[i], esp_err_to_name(ret));
        } else {
            // Initialize state
            ex2_isr_state[i] = gpio_get_level(ex2_pins[i]);
            ESP_LOGI(TAG, "  EX2 PIN%d: GPIO%d -> INPUT with ISR (initial=%d)",
                     i + 1, ex2_pins[i], ex2_isr_state[i]);
        }
    }

    ESP_LOGI(TAG, "Configuring EX4 input pins with GPIO interrupt...");
    for (int i = 0; i < NUM_PINS; i++) {
        gpio_reset_pin(ex4_pins[i]);

        gpio_config_t io_conf = {
            .pin_bit_mask = (1ULL << ex4_pins[i]),
            .mode = GPIO_MODE_INPUT,
            .pull_up_en = GPIO_PULLUP_DISABLE,
            .pull_down_en = GPIO_PULLDOWN_ENABLE,
            .intr_type = GPIO_INTR_ANYEDGE,  // Trigger on both rising and falling edge
        };
        ret = gpio_config(&io_conf);
        if (ret != ESP_OK) {
            ESP_LOGE(TAG, "  EX4 PIN%d: GPIO%d config FAILED (%s)",
                     i + 1, ex4_pins[i], esp_err_to_name(ret));
            continue;
        }

        // Add ISR handler for this pin
        ret = gpio_isr_handler_add(ex4_pins[i], ex4_gpio_isr_handler, (void *)(uint32_t)i);
        if (ret != ESP_OK) {
            ESP_LOGE(TAG, "  EX4 PIN%d: GPIO%d ISR add FAILED (%s)",
                     i + 1, ex4_pins[i], esp_err_to_name(ret));
        } else {
            // Initialize state
            ex4_isr_state[i] = gpio_get_level(ex4_pins[i]);
            ESP_LOGI(TAG, "  EX4 PIN%d: GPIO%d -> INPUT with ISR (initial=%d)",
                     i + 1, ex4_pins[i], ex4_isr_state[i]);
        }
    }
}

// ============================================================================
// Signal Test Functions
// ============================================================================

/**
 * @brief Set all output pins to specified level
 */
static void set_all_outputs(int level)
{
    // Set EX1 outputs
    for (int i = 0; i < NUM_PINS; i++) {
        gpio_set_level(ex1_pins[i], level);
    }
    // Set EX3 outputs
    for (int i = 0; i < NUM_PINS; i++) {
        gpio_set_level(ex3_pins[i], level);
    }
}

/**
 * @brief Clear ISR triggered flags
 */
static void clear_isr_flags(void)
{
    for (int i = 0; i < NUM_PINS; i++) {
        ex2_isr_triggered[i] = false;
        ex4_isr_triggered[i] = false;
    }
}

/**
 * @brief Test EX1 -> EX2 signal transfer using GPIO interrupt state
 * @param expected_level Expected input level (0 or 1)
 * @return Number of failed pins (0 = all pass)
 */
static int test_ex1_to_ex2(int expected_level)
{
    int failures = 0;

    ESP_LOGI(TAG, "  EX1 -> EX2 (expected: %s)", expected_level ? "HIGH" : "LOW");

    for (int i = 0; i < NUM_PINS; i++) {
        // Read current level (also updates ISR state)
        int actual = gpio_get_level(ex2_pins[i]);
        int isr_val = ex2_isr_state[i];
        bool triggered = ex2_isr_triggered[i];

        const char *status = (actual == expected_level) ? "PASS" : "FAIL";

        if (actual != expected_level) {
            failures++;
            ESP_LOGE(TAG, "    PIN%d: GPIO%d->GPIO%d = %d (ISR:%d,T:%d) [%s]",
                     i + 1, ex1_pins[i], ex2_pins[i], actual, isr_val, triggered, status);
        } else {
            ESP_LOGI(TAG, "    PIN%d: GPIO%d->GPIO%d = %d (ISR:%d,T:%d) [%s]",
                     i + 1, ex1_pins[i], ex2_pins[i], actual, isr_val, triggered, status);
        }
    }

    return failures;
}

/**
 * @brief Test EX3 -> EX4 signal transfer using GPIO interrupt state
 * @param expected_level Expected input level (0 or 1)
 * @return Number of failed pins (0 = all pass)
 */
static int test_ex3_to_ex4(int expected_level)
{
    int failures = 0;

    ESP_LOGI(TAG, "  EX3 -> EX4 (expected: %s)", expected_level ? "HIGH" : "LOW");

    for (int i = 0; i < NUM_PINS; i++) {
        // Read current level (also updates ISR state)
        int actual = gpio_get_level(ex4_pins[i]);
        int isr_val = ex4_isr_state[i];
        bool triggered = ex4_isr_triggered[i];

        const char *status = (actual == expected_level) ? "PASS" : "FAIL";

        if (actual != expected_level) {
            failures++;
            ESP_LOGE(TAG, "    PIN%d: GPIO%d->GPIO%d = %d (ISR:%d,T:%d) [%s]",
                     i + 1, ex3_pins[i], ex4_pins[i], actual, isr_val, triggered, status);
        } else {
            ESP_LOGI(TAG, "    PIN%d: GPIO%d->GPIO%d = %d (ISR:%d,T:%d) [%s]",
                     i + 1, ex3_pins[i], ex4_pins[i], actual, isr_val, triggered, status);
        }
    }

    return failures;
}

/**
 * @brief Run complete signal transfer test with GPIO interrupts
 */
static void signal_test_task(void *pvParameters)
{
    uint32_t test_count = 0;
    uint32_t pass_count = 0;
    uint32_t fail_count = 0;

    ESP_LOGI(TAG, "Starting GPIO signal transfer test (with ISR)...");
    ESP_LOGI(TAG, "============================================");
    ESP_LOGI(TAG, "Test Configuration:");
    ESP_LOGI(TAG, "  EX1 (OUTPUT) <-> EX2 (INPUT+ISR) via LAN cable");
    ESP_LOGI(TAG, "  EX3 (OUTPUT) <-> EX4 (INPUT+ISR) via LAN cable");
    ESP_LOGI(TAG, "  Output format: PIN: OUT->IN = val (ISR:val,T:triggered) [status]");
    ESP_LOGI(TAG, "============================================");

    while (1) {
        test_count++;
        int total_failures = 0;

        ESP_LOGI(TAG, "");
        ESP_LOGI(TAG, "===== Test #%lu =====", test_count);

        // ===== Phase 1: Set HIGH and verify =====
        ESP_LOGI(TAG, "[Phase 1] Setting all outputs HIGH...");
        clear_isr_flags();  // Clear ISR flags before changing output
        set_all_outputs(1);
        vTaskDelay(pdMS_TO_TICKS(50));  // Short delay for signal stabilization and ISR to trigger

        total_failures += test_ex1_to_ex2(1);
        total_failures += test_ex3_to_ex4(1);

        // Wait 0.5 seconds (as per specification)
        vTaskDelay(pdMS_TO_TICKS(500));

        // ===== Phase 2: Set LOW and verify =====
        ESP_LOGI(TAG, "[Phase 2] Setting all outputs LOW...");
        clear_isr_flags();  // Clear ISR flags before changing output
        set_all_outputs(0);
        vTaskDelay(pdMS_TO_TICKS(50));  // Short delay for signal stabilization and ISR to trigger

        total_failures += test_ex1_to_ex2(0);
        total_failures += test_ex3_to_ex4(0);

        // ===== Test Result =====
        if (total_failures == 0) {
            pass_count++;
            ESP_LOGI(TAG, ">>> Test #%lu: ALL PASS <<<", test_count);
        } else {
            fail_count++;
            ESP_LOGE(TAG, ">>> Test #%lu: FAILED (%d errors) <<<", test_count, total_failures);
        }

        ESP_LOGI(TAG, "Statistics: Total=%lu, Pass=%lu, Fail=%lu",
                 test_count, pass_count, fail_count);

        // Wait before next test cycle
        vTaskDelay(pdMS_TO_TICKS(2000));
    }
}
#endif  // GPIO Test Code - Currently disabled

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

/**
 * @brief Initialize IIS3DWB vibration sensor
 */
static esp_err_t init_iis3dwb_sensor(void)
{
    ESP_LOGI(TAG_SENSOR, "Initializing IIS3DWB vibration sensor...");

    iis3dwb_config_t config = {
        .spi_host = CONFIG_IIS3DWB_SPI_HOST,
        .mosi_io_num = CONFIG_IIS3DWB_SPI_MOSI_GPIO,
        .miso_io_num = CONFIG_IIS3DWB_SPI_MISO_GPIO,
        .sclk_io_num = CONFIG_IIS3DWB_SPI_SCLK_GPIO,
        .cs_io_num = CONFIG_IIS3DWB_SPI_CS_GPIO,
        .clk_speed_hz = CONFIG_IIS3DWB_SPI_FREQ_HZ,
        .full_scale = (iis3dwb_fs_xl_t)CONFIG_IIS3DWB_FULL_SCALE,
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

            // Alert for high vibration (magnitude > 2000 mg = 2g)
            if (magnitude > 2000.0f) {
                ESP_LOGW(TAG_SENSOR, "  ⚠ High vibration detected! (%.2f mg)", magnitude);
            }
        } else {
            ESP_LOGE(TAG_SENSOR, "Failed to read acceleration data: %s", esp_err_to_name(ret));
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
 * 제품화 구조: WiFi 자격증명을 컴파일 타임 Kconfig가 아니라
 * NVS(ROM)에 저장된 런타임 값에서 가져옵니다.
 *
 *  - 저장된 설정 있음 → 해당 SSID로 연결 시도 후 대기
 *  - 저장된 설정 없음 → WiFi 스택만 초기화하고 [설정 대기 모드]로 진입
 *    (PC 설정 툴의 scan_wifi/set_wifi 명령을 받을 수 있도록 스택은 살려둠)
 *
 * @return ESP_OK 연결 성공, ESP_ERR_NOT_FOUND 저장된 설정 없음(대기 모드),
 *         그 외 연결 실패
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

    // NVS에서 저장된 WiFi 자격증명 로드 시도
    config_wifi_cred_t cred;
    bool has_saved = (config_manager_load_wifi(&cred) == ESP_OK);

    // WiFi 스택 초기화 (저장값 있으면 그 SSID, 없으면 자리표시자)
    // 설정 없어도 스택은 살려서 설정 툴 명령(scan/set)을 받을 수 있게 함
    wifi_manager_config_t wifi_config = {
        .ssid = has_saved ? cred.ssid : "",
        .password = has_saved ? cred.password : "",
        .max_retry = CONFIG_WIFI_MAXIMUM_RETRY,
        .auth_mode_threshold = CONFIG_WIFI_SCAN_AUTH_MODE_THRESHOLD,
        /* 저장된 설정이 있을 때만 자동 연결. 없으면 대기(스캔만) → 크래시 방지 */
        .auto_connect = has_saved,
    };

    esp_err_t ret = wifi_manager_init(&wifi_config);
    if (ret != ESP_OK) {
        printf("\n");
        printf(COLOR_BG_RED COLOR_WHITE "  ✗ WiFi Init Failed!  " COLOR_RESET "\n");
        printf("\n");
        return ret;
    }

    if (!has_saved) {
        // 저장된 설정 없음 → 설정 대기 모드
        printf("\n");
        printf(COLOR_YELLOW "  ⚙ WiFi 설정이 없습니다. 설정 대기 모드로 진입합니다." COLOR_RESET "\n");
        printf(COLOR_YELLOW "    PC 설정 툴을 USB로 연결하여 WiFi를 설정하세요." COLOR_RESET "\n");
        printf("\n");
        return ESP_ERR_NOT_FOUND;
    }

    // 저장된 설정으로 연결 대기 (30초 타임아웃 — 실패해도 동작 계속)
    ESP_LOGI(TAG_WIFI, "저장된 설정으로 연결 시도: %s", cred.ssid);
    ret = wifi_manager_wait_for_connection(30000);
    if (ret != ESP_OK) {
        printf("\n");
        printf(COLOR_BG_RED COLOR_WHITE "  ✗ WiFi Connection Failed!  " COLOR_RESET "\n");
        printf(COLOR_RED "  저장된 WiFi에 연결 실패: %s" COLOR_RESET "\n", cred.ssid);
        printf(COLOR_YELLOW "  설정 툴로 WiFi를 다시 설정할 수 있습니다." COLOR_RESET "\n");
        printf("\n");
        return ret;
    }

    char ip_str[16];
    if (wifi_manager_get_ip_string(ip_str, sizeof(ip_str)) == ESP_OK) {
        // Green background for WiFi success - highly visible
        printf("\n");
        printf(COLOR_BG_GREEN COLOR_WHITE "  ★ WiFi Connected Successfully!  " COLOR_RESET "\n");
        printf(COLOR_GREEN "  ┌─────────────────────────────────┐" COLOR_RESET "\n");
        printf(COLOR_GREEN "  │" COLOR_RESET " SSID: " COLOR_CYAN "%-24s" COLOR_RESET COLOR_GREEN " │" COLOR_RESET "\n", cred.ssid);
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

    // ===== WiFi Initialization =====
    // NVS에 저장된 설정으로 연결. 설정 없으면 설정 대기 모드.
    ESP_LOGI(TAG, "Step 1: WiFi Initialization");
    ret = init_wifi();
    if (ret == ESP_ERR_NOT_FOUND) {
        ESP_LOGW(TAG_WIFI, "WiFi 미설정 — 설정 툴 대기 중 (센서 기능은 계속 동작)");
    } else if (ret != ESP_OK) {
        ESP_LOGW(TAG_WIFI, "WiFi 연결 실패 — WiFi 없이 계속 진행");
    } else {
        ESP_LOGI(TAG, "WiFi connected successfully!");
    }

    vTaskDelay(pdMS_TO_TICKS(1000));

    // ===== IIS3DWB Sensor Initialization =====
    ESP_LOGI(TAG, "");
    ESP_LOGI(TAG, "Step 2: IIS3DWB Sensor Initialization");
    ret = init_iis3dwb_sensor();
    if (ret != ESP_OK) {
        ESP_LOGW(TAG_SENSOR, "Sensor init failed, continuing without sensor...");
    } else {
        ESP_LOGI(TAG, "IIS3DWB sensor initialized successfully!");
    }

    vTaskDelay(pdMS_TO_TICKS(500));

    // 스트리밍 활성 여부 미리 판단 (스트리밍이면 모니터링 태스크와 센서 경합 방지)
    bool streaming_active = (iis3dwb_initialized && wifi_manager_is_connected()
                             && config_manager_has_stream());

    // ===== Start IIS3DWB Sensor Reading Task =====
    if (iis3dwb_initialized && !streaming_active) {
        // 스트리밍이 아닐 때만 모니터링 태스크 가동 (FIFO와 SPI/센서 경합 방지)
        ESP_LOGI(TAG, "");
        ESP_LOGI(TAG, "Step 3: Starting Vibration Monitoring");
        xTaskCreate(iis3dwb_read_task, "iis3dwb_read", 4096, NULL, 5, NULL);
    } else if (iis3dwb_initialized && streaming_active) {
        ESP_LOGI(TAG, "Step 3: (스트리밍 모드 — 모니터링 태스크 생략, 센서 경합 방지)");
    } else {
        ESP_LOGW(TAG, "");
        ESP_LOGW(TAG, "No sensor available. Starting GPIO test mode...");
        ESP_LOGI(TAG, "");
        ESP_LOGI(TAG, "Step 3: GPIO Signal Transfer Test");
        configure_output_pins();
        configure_input_pins();
        xTaskCreate(signal_test_task, "gpio_test", 4096, NULL, 5, NULL);
    }

    // ===== Step 4: WiFi 센서 데이터 스트리밍 =====
    // 조건: 센서 정상 + WiFi 연결됨 + 스트리밍 설정(서버 IP) 존재
    if (iis3dwb_initialized && wifi_manager_is_connected() && config_manager_has_stream()) {
        config_stream_t scfg;
        if (config_manager_load_stream(&scfg) == ESP_OK) {
            ESP_LOGI(TAG, "");
            ESP_LOGI(TAG, "Step 4: WiFi 센서 데이터 스트리밍 시작");
            sensor_streamer_config_t st = {
                .sensor = &iis3dwb_handle,
                .server_ip = scfg.server_ip,
                .server_port = scfg.server_port,
                .rate_step = scfg.rate_step,
                .transport = (stream_transport_type_t)scfg.transport,
                .read_mode = scfg.read_mode,
            };
            esp_err_t sret = sensor_streamer_start(&st);
            if (sret == ESP_OK) {
                printf("\n");
                printf(COLOR_BG_GREEN COLOR_WHITE "  📡 센서 데이터 스트리밍 중!  " COLOR_RESET "\n");
                printf(COLOR_GREEN "  → %s:%u (%lu Hz)" COLOR_RESET "\n",
                       scfg.server_ip, scfg.server_port,
                       sensor_streamer_rate_hz(scfg.rate_step));
                printf("\n");
            } else {
                ESP_LOGE(TAG, "스트리밍 시작 실패: %s", esp_err_to_name(sret));
            }
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
            ESP_LOGI(TAG, "[스트리밍] 패킷=%lu 샘플=%lu 드롭=%lu 에러=%lu INT=%lu",
                     st.packets_sent, st.samples_sent, st.dropped, st.send_errors,
                     st.int_count);
        }
    }
}
