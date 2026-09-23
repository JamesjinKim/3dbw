/**
 * @file boardcheck_main.c
 * @brief IIS3DWB 센서 보드 수입검사 펌웨어
 *
 * 보드를 꽂으면 부팅 즉시 아래 6개 항목을 순서대로 검사하고, 사람이 읽는
 * 표와 기계가 읽는 한 줄(`BOARDCHECK_RESULT=...`)을 남긴 뒤 멈춘다.
 * NVS 설정도 WiFi 자격증명도 필요 없다 — 꽂기만 하면 된다.
 *
 *   1. MCU          ESP32-S3 인지, MAC/플래시 크기
 *   2. SPI/WHO_AM_I 센서와 SPI 로 대화가 되는지 (0x7B)
 *   3. 가속도 출력   정지 상태에서 중력 1g 가 보이는지, 값이 고정돼 있지 않은지
 *   4. FIFO/ODR     FIFO 축적 속도로 ODR(26.667kHz)을 역산
 *   5. INT1 전기상태 watermark 도달 시점에 INT1 핀이 실제로 구동되는지
 *   6. INT 발생률    인터럽트가 초당 몇 번 오는지 (기대 ≒417)
 *
 * 5번이 이 펌웨어를 만든 이유다. 보드 한 개에서 INT1 이 플로팅(단선)이라
 * 인터럽트 모드가 동작하지 않았는데, 펌웨어는 조용히 폴링으로 흘러가
 * "느리다" 는 증상만 남겼다. 내부 풀업/풀다운을 번갈아 걸어보면
 * 단선인지 극성 반전인지 바로 갈린다 — 그 판정을 검사 항목으로 고정했다.
 *
 * 설계 원칙
 *   · 검사 항목이 실패해도 다음 항목을 계속 돌린다. 한 번 꽂아 최대한 많이 안다.
 *   · 선행 항목이 실패해 뒤 항목의 의미가 없어지면 SKIP 으로 명시한다
 *     (FAIL 로 적으면 원인이 두 개인 것처럼 보인다).
 *   · 판정 기준은 Kconfig 로 뺀다. 하드웨어가 바뀌면 기준만 고친다.
 */

#include <stdio.h>
#include <string.h>
#include <math.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"

#include "driver/gpio.h"
#include "esp_chip_info.h"
#include "esp_flash.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_timer.h"

#include "iis3dwb.h"

#define BOARDCHECK_VERSION "1.0"

/* watermark 를 64 로 두는 것은 운영 펌웨어(sensor_streamer)와 같은 값이다.
 * 검사 조건이 실제 동작 조건과 달라지면 검사가 통과해도 의미가 없다. */
#define WTM_SAMPLES   64
#define BURST_MAX     IIS3DWB_FIFO_BURST_MAX      /* 64 */
#define ODR_NOMINAL   26667                        /* 사양상 ODR (Hz) */

#define INT1_GPIO     CONFIG_IIS3DWB_INT1_GPIO
#define INT2_GPIO     CONFIG_IIS3DWB_INT2_GPIO

static const char *TAG = "BOARDCHECK";

/* ===================== 결과 표 ===================== */

typedef enum { R_SKIP = 0, R_PASS, R_FAIL, R_WARN } verdict_t;

typedef struct {
    const char *name;
    verdict_t   verdict;
    char        detail[160];   /* 측정값 — PASS 일 때도 남긴다 (추세 비교용) */
    char        hint[384];     /* FAIL 일 때 어디를 볼지 (한글 1자=3B, 넉넉히) */
} result_t;

/* UTF-8 안전 복사: 잘릴 때 문자 경계에서 자른다.
 *
 * snprintf 는 바이트 단위로 자르므로 한글 한 글자 중간에서 끊기면 깨진 바이트가
 * 그대로 터미널과 로그 파일에 남는다. 조치 안내가 깨지면 담당자가 무엇을
 * 봐야 할지 알 수 없다. */
static void copy_utf8(char *dst, size_t cap, const char *src)
{
    if (cap == 0) return;
    if (!src) { dst[0] = '\0'; return; }

    size_t n = strlen(src);
    if (n < cap) { memcpy(dst, src, n + 1); return; }

    n = cap - 1;
    /* 연속 바이트(10xxxxxx) 위에 서 있으면 선행 바이트까지 물러난다. */
    while (n > 0 && ((unsigned char)src[n] & 0xC0) == 0x80) n--;
    memcpy(dst, src, n);
    dst[n] = '\0';
}

#define N_TESTS 6
static result_t R[N_TESTS] = {
    { .name = "MCU" },
    { .name = "SPI / WHO_AM_I" },
    { .name = "가속도 출력" },
    { .name = "FIFO / ODR" },
    { .name = "INT1 전기상태" },
    { .name = "INT 발생률" },
};

static const char *verdict_str(verdict_t v)
{
    switch (v) {
        case R_PASS: return "PASS";
        case R_FAIL: return "FAIL";
        case R_WARN: return "WARN";
        default:     return "SKIP";
    }
}

/* UTF-8 문자열이 터미널에서 차지하는 칸 수.
 *
 * printf 의 `%-16s` 는 **바이트** 수로 채운다. 항목 이름이 한글이면 한 글자가
 * 3바이트라 열이 어긋나고, 검사표가 읽기 어려워진다. 한글·CJK 는 2칸을
 * 차지하므로 코드포인트를 보고 직접 센다. */
static int disp_width(const char *s)
{
    int w = 0;
    const unsigned char *p = (const unsigned char *)s;
    while (*p) {
        unsigned cp;
        int n;
        if (*p < 0x80)                 { cp = *p;        n = 1; }
        else if ((*p & 0xE0) == 0xC0)  { cp = *p & 0x1F; n = 2; }
        else if ((*p & 0xF0) == 0xE0)  { cp = *p & 0x0F; n = 3; }
        else                           { cp = *p & 0x07; n = 4; }
        for (int i = 1; i < n && p[i]; i++) cp = (cp << 6) | (p[i] & 0x3Fu);
        p += n;
        w += (cp >= 0x1100 &&
              (cp <= 0x115F ||                       /* 한글 자모 */
               (cp >= 0x2E80 && cp <= 0xA4CF) ||     /* CJK 부수~이 */
               (cp >= 0xAC00 && cp <= 0xD7A3) ||     /* 한글 음절 */
               (cp >= 0xF900 && cp <= 0xFAFF) ||     /* CJK 호환 */
               (cp >= 0xFF00 && cp <= 0xFF60))) ? 2 : 1;
    }
    return w;
}

/* 표시 폭 기준으로 오른쪽을 공백으로 채워 열을 맞춘다. */
static void print_padded(const char *s, int width)
{
    int pad = width - disp_width(s);
    printf("%s", s);
    while (pad-- > 0) putchar(' ');
}

#define NAME_COL 18

static void set_result(int i, verdict_t v, const char *detail, const char *hint)
{
    R[i].verdict = v;
    copy_utf8(R[i].detail, sizeof(R[i].detail), detail);
    copy_utf8(R[i].hint, sizeof(R[i].hint), hint);
    /* 진행 상황을 즉시 흘린다 — 중간에 멈춰도 어디까지 갔는지 남는다. */
    printf("[%d/%d] ", i + 1, N_TESTS);
    print_padded(R[i].name, NAME_COL);
    printf("%-4s  %s\n", verdict_str(v), R[i].detail);
    fflush(stdout);
}

/* ===================== 상태 LED ===================== */

#if CONFIG_BOARDCHECK_LED
static void led_init(void)
{
    gpio_config_t io = {
        .pin_bit_mask = (1ULL << CONFIG_BOARDCHECK_LED_RED_GPIO) |
                        (1ULL << CONFIG_BOARDCHECK_LED_GREEN_GPIO) |
                        (1ULL << CONFIG_BOARDCHECK_LED_BLUE_GPIO),
        .mode = GPIO_MODE_OUTPUT,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&io);
}

static void led_rgb(bool r, bool g, bool b)
{
#if CONFIG_BOARDCHECK_LED_ACTIVE_LOW
    const int on = 0, off = 1;
#else
    const int on = 1, off = 0;
#endif
    gpio_set_level((gpio_num_t)CONFIG_BOARDCHECK_LED_RED_GPIO,   r ? on : off);
    gpio_set_level((gpio_num_t)CONFIG_BOARDCHECK_LED_GREEN_GPIO, g ? on : off);
    gpio_set_level((gpio_num_t)CONFIG_BOARDCHECK_LED_BLUE_GPIO,  b ? on : off);
}
#else
static void led_init(void) {}
static void led_rgb(bool r, bool g, bool b) { (void)r; (void)g; (void)b; }
#endif

/* ===================== INT 핀 전기 상태 판정 ===================== */

typedef enum {
    PIN_UNDRIVEN,   /* 아무도 구동 안 함 — 단선 또는 센서가 핀을 안 씀 */
    PIN_DRIVEN_LOW, /* LOW 로 능동 구동 — 극성 반전 */
    PIN_DRIVEN_HIGH,/* HIGH 로 능동 구동 — 정상 (watermark 도달 상태에서) */
    PIN_UNKNOWN,
} pin_state_t;

/* 내부 풀업/풀다운을 번갈아 걸고 레벨을 읽는다.
 *
 *   풀업→1, 풀다운→0 : 핀이 풀 저항만 따라간다 = 아무도 구동하지 않음
 *   풀업→0, 풀다운→0 : 풀업(45k)을 이기고 0 = 외부가 LOW 로 구동 중
 *   풀업→1, 풀다운→1 : 풀다운을 이기고 1 = 외부가 HIGH 로 구동 중
 *
 * 이 함수는 핀 설정을 바꾸므로, 인터럽트를 붙이기 **전에** 호출해야 한다.
 */
static pin_state_t probe_pin(int gpio, int lv_out[3])
{
    if (gpio < 0) return PIN_UNKNOWN;

    gpio_config_t io = {
        .pin_bit_mask = 1ULL << gpio,
        .mode = GPIO_MODE_INPUT,
        .intr_type = GPIO_INTR_DISABLE,
    };
    const struct { gpio_pullup_t up; gpio_pulldown_t down; } cfg[3] = {
        { GPIO_PULLUP_ENABLE,  GPIO_PULLDOWN_DISABLE },
        { GPIO_PULLUP_DISABLE, GPIO_PULLDOWN_ENABLE  },
        { GPIO_PULLUP_DISABLE, GPIO_PULLDOWN_DISABLE },
    };
    for (int i = 0; i < 3; i++) {
        io.pull_up_en = cfg[i].up;
        io.pull_down_en = cfg[i].down;
        gpio_config(&io);
        vTaskDelay(pdMS_TO_TICKS(3));     /* 핀 용량 충방전 대기 */
        lv_out[i] = gpio_get_level((gpio_num_t)gpio);
    }

    if (lv_out[0] == 1 && lv_out[1] == 0) return PIN_UNDRIVEN;
    if (lv_out[0] == 0 && lv_out[1] == 0) return PIN_DRIVEN_LOW;
    if (lv_out[0] == 1 && lv_out[1] == 1) return PIN_DRIVEN_HIGH;
    return PIN_UNKNOWN;
}

/* ===================== 인터럽트 ===================== */

static volatile uint32_t g_int_count;
static SemaphoreHandle_t g_int_sem;

/* 운영 펌웨어와 같은 방식: watermark 는 레벨 신호라 쌓여 있는 동안 HIGH 를
 * 유지한다. ISR 재진입 폭주를 막으려 진입 즉시 해당 핀 INT 를 끄고,
 * FIFO 를 비운 태스크가 다시 켠다. */
static void IRAM_ATTR int1_isr(void *arg)
{
    gpio_intr_disable((gpio_num_t)(intptr_t)arg);
    g_int_count++;
    BaseType_t hpw = pdFALSE;
    xSemaphoreGiveFromISR(g_int_sem, &hpw);
    if (hpw) portYIELD_FROM_ISR();
}

/* ===================== 검사 항목 ===================== */

static iis3dwb_handle_t s_sensor;
static iis3dwb_raw_data_t s_burst[BURST_MAX];   /* 스택 절약 위해 정적 */

static bool test_1_mcu(void)
{
    esp_chip_info_t ci;
    esp_chip_info(&ci);

    uint8_t mac[6] = {0};
    esp_read_mac(mac, ESP_MAC_WIFI_STA);

    uint32_t fsize = 0;
    esp_flash_get_size(NULL, &fsize);

    char d[128];
    snprintf(d, sizeof(d),
             "ESP32-S3 rev%d.%d, %d코어, flash %luKB, MAC %02X:%02X:%02X:%02X:%02X:%02X",
             ci.revision / 100, ci.revision % 100, ci.cores,
             (unsigned long)(fsize / 1024),
             mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);

    if (ci.model != CHIP_ESP32S3) {
        set_result(0, R_FAIL, d, "이 검사 펌웨어는 ESP32-S3 전용입니다. 보드 MCU 를 확인하세요.");
        return false;
    }
    set_result(0, R_PASS, d, NULL);
    return true;
}

static bool test_2_spi(void)
{
    iis3dwb_config_t cfg = {
        .spi_host    = CONFIG_IIS3DWB_SPI_HOST,
        .mosi_io_num = CONFIG_IIS3DWB_SPI_MOSI_GPIO,
        .miso_io_num = CONFIG_IIS3DWB_SPI_MISO_GPIO,
        .sclk_io_num = CONFIG_IIS3DWB_SPI_SCLK_GPIO,
        .cs_io_num   = CONFIG_IIS3DWB_SPI_CS_GPIO,
        .clk_speed_hz = CONFIG_IIS3DWB_SPI_FREQ_HZ,
        .full_scale  = (iis3dwb_fs_xl_t)CONFIG_IIS3DWB_FULL_SCALE,
        .bandwidth   = (iis3dwb_bw_xl_t)CONFIG_IIS3DWB_BANDWIDTH,
    };

    esp_err_t err = iis3dwb_init(&cfg, &s_sensor);
    if (err != ESP_OK) {
        char d[128];
        snprintf(d, sizeof(d), "%s (MOSI=%d MISO=%d SCLK=%d CS=%d @%dHz)",
                 esp_err_to_name(err),
                 cfg.mosi_io_num, cfg.miso_io_num, cfg.sclk_io_num,
                 cfg.cs_io_num, cfg.clk_speed_hz);
        set_result(1, R_FAIL, d,
                   err == ESP_ERR_NOT_FOUND
                     ? "WHO_AM_I 불일치. MISO 단선 / CS 미동작 / 센서 전원(VDD·VDDIO 2.1~3.6V) 을 확인하세요."
                     : "SPI 버스 초기화 실패. 핀 번호 충돌 또는 다른 장치가 같은 핀을 쓰는지 확인하세요.");
        return false;
    }

    uint8_t id = 0;
    iis3dwb_get_device_id(&s_sensor, &id);
    char d[64];
    snprintf(d, sizeof(d), "WHO_AM_I=0x%02X (기대 0x%02X)", id, IIS3DWB_DEVICE_ID);
    set_result(1, R_PASS, d, NULL);
    return true;
}

static bool test_3_accel(void)
{
    if (iis3dwb_enable(&s_sensor, true) != ESP_OK) {
        set_result(2, R_FAIL, "가속도계 활성화 실패 (CTRL1_XL 쓰기 오류)",
                   "SPI 쓰기가 되지 않습니다. MOSI/SCLK 배선을 확인하세요.");
        return false;
    }
    vTaskDelay(pdMS_TO_TICKS(20));

    const int N = 32;
    int32_t sum[3] = {0, 0, 0};
    int16_t mn[3] = {INT16_MAX, INT16_MAX, INT16_MAX};
    int16_t mx[3] = {INT16_MIN, INT16_MIN, INT16_MIN};

    for (int i = 0; i < N; i++) {
        iis3dwb_raw_data_t r;
        if (iis3dwb_read_raw_data(&s_sensor, &r) != ESP_OK) {
            set_result(2, R_FAIL, "가속도 레지스터 읽기 실패",
                       "SPI 통신이 중간에 끊깁니다. 배선 접촉과 SPI 클럭(8MHz)을 확인하세요.");
            return false;
        }
        const int16_t v[3] = { r.x, r.y, r.z };
        for (int a = 0; a < 3; a++) {
            sum[a] += v[a];
            if (v[a] < mn[a]) mn[a] = v[a];
            if (v[a] > mx[a]) mx[a] = v[a];
        }
        vTaskDelay(pdMS_TO_TICKS(2));
    }

    const float s = s_sensor.sensitivity;          /* mg/LSB */
    const float mg[3] = { sum[0] / (float)N * s,
                          sum[1] / (float)N * s,
                          sum[2] / (float)N * s };
    const float mag = sqrtf(mg[0] * mg[0] + mg[1] * mg[1] + mg[2] * mg[2]);
    const int p2p = (mx[0] - mn[0]) + (mx[1] - mn[1]) + (mx[2] - mn[2]);

    char d[128];
    snprintf(d, sizeof(d), "X=%.0f Y=%.0f Z=%.0f mg, |a|=%.0f mg, p2p=%d LSB",
             mg[0], mg[1], mg[2], mag, p2p);

    /* 값이 한 번도 안 변하면 센서가 응답은 하지만 변환을 못 하는 상태다.
     * 26.6kHz 로 도는 센서에서 32회 읽어 3축 모두 완전 고정은 정상이 아니다. */
    if (p2p == 0) {
        set_result(2, R_FAIL, d,
                   "출력이 완전히 고정되어 있습니다. 센서가 변환을 하지 않는 상태입니다 "
                   "(전원 불안정 또는 센서 불량).");
        return false;
    }
    /* 정지 상태라면 합성 가속도가 중력 1g 근처여야 한다.
     * 검사대에서 보드를 들고 있으면 흔들려 벗어날 수 있어 WARN 으로 둔다. */
    if (mag < 700.0f || mag > 1300.0f) {
        set_result(2, R_WARN, d,
                   "정지 상태의 합성 가속도가 1g(1000mg) 범위를 벗어났습니다. "
                   "보드를 평평한 곳에 내려놓고 다시 검사하세요. 그래도 같으면 센서 교정/불량을 의심합니다.");
        return true;      /* 후속 검사는 계속 진행 */
    }
    set_result(2, R_PASS, d, NULL);
    return true;
}

static bool test_4_fifo(void)
{
    /* FIFO 를 완전히 비운 상태에서 출발시켜 축적 속도를 잰다.
     * BYPASS → CONTINUOUS 전환이 FIFO 를 리셋한다. */
    iis3dwb_fifo_disable(&s_sensor);
    vTaskDelay(pdMS_TO_TICKS(5));

    if (iis3dwb_fifo_enable(&s_sensor, IIS3DWB_BDR_26667) != ESP_OK) {
        set_result(3, R_FAIL, "FIFO 활성화 실패 (FIFO_CTRL 쓰기 오류)",
                   "SPI 쓰기 오류입니다. 배선을 확인하세요.");
        return false;
    }

    /* 8ms → 약 213 샘플. FIFO 상한(512)에 닿지 않아 속도를 정확히 잴 수 있다. */
    const int64_t t0 = esp_timer_get_time();
    vTaskDelay(pdMS_TO_TICKS(8));
    uint16_t count = 0;
    uint8_t st = 0;
    esp_err_t err = iis3dwb_fifo_status(&s_sensor, &count, &st);
    const int64_t dt_us = esp_timer_get_time() - t0;

    if (err != ESP_OK) {
        set_result(3, R_FAIL, "FIFO_STATUS 읽기 실패",
                   "SPI 통신 오류입니다. 배선을 확인하세요.");
        return false;
    }

    const float hz = count * 1000000.0f / (float)dt_us;
    char d[128];
    snprintf(d, sizeof(d), "%u워드 / %.1fms → %.0f Hz (사양 %d Hz)",
             count, dt_us / 1000.0f, hz, ODR_NOMINAL);

    if (count == 0) {
        set_result(3, R_FAIL, d,
                   "FIFO 에 아무것도 쌓이지 않습니다. 가속도계가 꺼져 있거나 "
                   "FIFO 배치(BDR) 설정이 먹지 않았습니다.");
        return false;
    }
    if (count >= 512) {
        set_result(3, R_WARN, d,
                   "FIFO 가 측정 창 안에 가득 찼습니다(512). 속도를 정확히 잴 수 없습니다 — "
                   "시스템 부하가 높거나 SPI 가 느립니다.");
        return true;
    }
    if (hz < CONFIG_BOARDCHECK_ODR_MIN) {
        set_result(3, R_FAIL, d,
                   "ODR 이 사양(26667Hz)보다 낮습니다. 센서 클럭 이상 또는 "
                   "CTRL 레지스터가 기대와 다르게 설정된 상태입니다.");
        return false;
    }
    set_result(3, R_PASS, d, NULL);
    return true;
}

/* INT1 을 watermark 로 라우팅하고, **실제로 watermark 에 도달한 시점에**
 * 핀의 전기 상태를 읽는다. 도달 전에 읽으면 LOW 가 정상이라 판정이 뒤집힌다. */
static bool test_5_int_pin(int *int2_lv, pin_state_t *int2_state)
{
    if (INT1_GPIO < 0) {
        set_result(4, R_SKIP, "CONFIG_IIS3DWB_INT1_GPIO=-1 (핀 미사용 설정)", NULL);
        return false;
    }

    esp_err_t e1 = iis3dwb_fifo_set_watermark(&s_sensor, WTM_SAMPLES);
    esp_err_t e2 = iis3dwb_fifo_route_int1(&s_sensor, true);
    if (e1 != ESP_OK || e2 != ESP_OK) {
        set_result(4, R_FAIL, "watermark/INT1 라우팅 레지스터 쓰기 실패",
                   "SPI 쓰기 오류입니다. 배선을 확인하세요.");
        return false;
    }

    /* 레지스터가 실제로 들어갔는지 되읽어 확인한다. 쓰기가 조용히 실패하면
     * 이후 모든 판정이 "센서가 신호를 안 준다" 로 잘못 흐른다. */
    uint8_t int1_ctrl = 0, fifo_ctrl1 = 0;
    iis3dwb_read_register(&s_sensor, IIS3DWB_REG_INT1_CTRL, &int1_ctrl);
    iis3dwb_read_register(&s_sensor, IIS3DWB_REG_FIFO_CTRL1, &fifo_ctrl1);
    if (!(int1_ctrl & IIS3DWB_INT1_FIFO_TH)) {
        char d[96];
        snprintf(d, sizeof(d), "INT1_CTRL=0x%02X — FIFO_TH 비트가 서지 않음", int1_ctrl);
        set_result(4, R_FAIL, d,
                   "센서가 watermark 인터럽트를 INT1 로 내보내도록 설정되지 않았습니다. "
                   "SPI 쓰기가 반영되지 않는 상태입니다.");
        return false;
    }

    /* watermark 도달까지 대기 (64샘플 ≒ 2.4ms, 넉넉히 200ms 상한) */
    uint16_t count = 0;
    uint8_t st = 0;
    bool wtm = false;
    for (int i = 0; i < 40 && !wtm; i++) {
        vTaskDelay(pdMS_TO_TICKS(5));
        if (iis3dwb_fifo_status(&s_sensor, &count, &st) != ESP_OK) break;
        wtm = (st & IIS3DWB_FIFO_STATUS_WTM) != 0;
    }
    if (!wtm) {
        char d[96];
        snprintf(d, sizeof(d), "watermark 미도달 (FIFO=%u STATUS2=0x%02X)", count, st);
        set_result(4, R_FAIL, d,
                   "센서 내부에서 watermark 조건이 서지 않습니다. 앞 항목(FIFO/ODR)을 먼저 보세요.");
        return false;
    }

    /* 대조군 INT2 를 먼저 읽는다 — 커넥터 전체 문제인지 INT1 단독인지 가른다. */
    *int2_state = probe_pin(INT2_GPIO, int2_lv);

    int lv[3];
    pin_state_t ps = probe_pin(INT1_GPIO, lv);

    char d[128];
    snprintf(d, sizeof(d), "IO%d 풀업=%d 풀다운=%d 플로팅=%d (FIFO=%u WTM=1, INT1_CTRL=0x%02X FIFO_CTRL1=%u)",
             INT1_GPIO, lv[0], lv[1], lv[2], count, int1_ctrl, fifo_ctrl1);

    switch (ps) {
    case PIN_DRIVEN_HIGH:
        set_result(4, R_PASS, d, NULL);
        return true;
    case PIN_UNDRIVEN:
        set_result(4, R_FAIL, d,
                   "INT1 을 아무도 구동하지 않습니다 = 단선. 센서가 watermark 도달을 "
                   "보고하는데도 핀이 뜨지 않으므로 센서 INT1 패드 → 보드간 커넥터 → "
                   "ESP32 IO 까지의 경로 중 한 곳이 끊겼습니다. (INT2 대조군 결과를 함께 보세요)");
        return false;
    case PIN_DRIVEN_LOW:
        set_result(4, R_FAIL, d,
                   "INT1 이 LOW 로 능동 구동됩니다 = 극성 반전. 센서 INT 출력이 "
                   "active-low 로 설정됐거나(CTRL3_C.H_LACTIVE) 반전 버퍼가 끼어 있습니다.");
        return false;
    default:
        set_result(4, R_FAIL, d,
                   "핀 거동이 일관되지 않습니다 (풀업→0, 풀다운→1). 신호선이 다른 "
                   "네트와 단락됐을 가능성이 있습니다.");
        return false;
    }
}

static bool test_6_int_rate(bool pin_ok)
{
    if (INT1_GPIO < 0) {
        set_result(5, R_SKIP, "INT1 핀 미사용 설정", NULL);
        return false;
    }

    /* 5번이 이미 실패했으면 결론은 정해져 있다. 확인 사격만 짧게 한다. */
    const int secs = pin_ok ? CONFIG_BOARDCHECK_INT_SECONDS : 3;

    g_int_sem = xSemaphoreCreateBinary();
    if (!g_int_sem) {
        set_result(5, R_FAIL, "세마포어 생성 실패 (메모리 부족)", NULL);
        return false;
    }
    g_int_count = 0;

    /* 순서가 중요하다. 앞 항목에서 이미 INT1 을 라우팅했으므로 이 시점에 핀은
     * **이미 HIGH** 다. 여기서 레벨 트리거를 먼저 켜면, 핸들러를 붙이기 전에
     * 레벨 인터럽트가 걸린다. 레벨 신호는 핸들러가 원인을 없애 줘야 내려가는데
     * 핸들러가 없으니 영원히 다시 걸려 CPU 가 ISR 에서 못 빠져나온다
     * (증상: 검사 5번까지 찍고 완전 정지. 실제로 겪었다).
     * 그래서 인터럽트는 꺼 둔 채 설정 → 서비스 설치 → 핸들러 등록 → 마지막에 켠다. */
    gpio_config_t io = {
        .pin_bit_mask = 1ULL << INT1_GPIO,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_ENABLE,   /* 미구동 시 확실히 LOW 로 두어 오검출 방지 */
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&io);

    esp_err_t isr_ret = gpio_install_isr_service(0);
    if (isr_ret != ESP_OK && isr_ret != ESP_ERR_INVALID_STATE) {
        set_result(5, R_FAIL, "GPIO ISR 서비스 설치 실패", NULL);
        return false;
    }
    if (gpio_isr_handler_add((gpio_num_t)INT1_GPIO, int1_isr,
                             (void *)(intptr_t)INT1_GPIO) != ESP_OK) {
        set_result(5, R_FAIL, "ISR 등록 실패", NULL);
        return false;
    }
    /* 핸들러가 준비된 뒤에야 레벨 트리거를 켠다. */
    gpio_set_intr_type((gpio_num_t)INT1_GPIO, GPIO_INTR_HIGH_LEVEL);
    gpio_intr_enable((gpio_num_t)INT1_GPIO);

    uint32_t samples = 0, overrun = 0, timeouts = 0;
    const int64_t t0 = esp_timer_get_time();
    const int64_t t_end = t0 + (int64_t)secs * 1000000;

    while (esp_timer_get_time() < t_end) {
        if (xSemaphoreTake(g_int_sem, pdMS_TO_TICKS(200)) != pdTRUE) {
            timeouts++;
            /* INT 가 오지 않아도 FIFO 는 계속 찬다. 비우지 않으면 오버런 통계가
             * 의미를 잃으므로, 타임아웃 경로에서도 한 번 비운다. */
        }
        uint16_t avail = 0;
        uint8_t st = 0;
        if (iis3dwb_fifo_status(&s_sensor, &avail, &st) != ESP_OK) {
            avail = 0;
        } else if (st & IIS3DWB_FIFO_STATUS_OVR) {
            overrun++;
        }
        while (avail > 0) {
            uint16_t want = avail > BURST_MAX ? BURST_MAX : avail;
            uint16_t got = 0;
            if (iis3dwb_read_fifo(&s_sensor, s_burst, want, &got) != ESP_OK || got == 0) break;
            samples += got;
            avail -= got;
        }
        /* ISR 이 껐던 INT 를 되살린다. 이 줄을 건너뛰는 경로가 있으면
         * 인터럽트가 영구히 죽는다 — 어떤 오류 경로에서도 반드시 지난다. */
        gpio_intr_enable((gpio_num_t)INT1_GPIO);
    }

    const float elapsed = (esp_timer_get_time() - t0) / 1000000.0f;
    const uint32_t ints = g_int_count;
    const float rate = ints / elapsed;
    const float sps = samples / elapsed;

    /* 정리 순서도 설치의 역순이어야 한다. 서비스를 먼저 내리면 아직 HIGH 인
     * 레벨 인터럽트가 핸들러 없이 남아 같은 폭주가 난다. 신호원(센서 라우팅)
     * 부터 끊고 → 핀 인터럽트 끄고 → 핸들러 제거 → 서비스 해제. */
    iis3dwb_fifo_route_int1(&s_sensor, false);
    gpio_intr_disable((gpio_num_t)INT1_GPIO);
    gpio_set_intr_type((gpio_num_t)INT1_GPIO, GPIO_INTR_DISABLE);
    gpio_isr_handler_remove((gpio_num_t)INT1_GPIO);
    gpio_uninstall_isr_service();

    char d[128];
    snprintf(d, sizeof(d), "%lu회/%.1f초 = %.0f/s (기대 %d/s), 샘플 %.0f Hz, 오버런 %lu",
             (unsigned long)ints, elapsed, rate, ODR_NOMINAL / WTM_SAMPLES, sps,
             (unsigned long)overrun);

    if (ints == 0) {
        set_result(5, R_FAIL, d,
                   pin_ok ? "INT1 핀은 구동되는데 ESP32 가 인터럽트를 받지 못합니다. "
                            "GPIO 번호가 실제 배선과 다른지 확인하세요."
                          : "앞 항목(INT1 전기상태)의 원인 그대로입니다. 그쪽 조치를 먼저 하세요.");
        return false;
    }
    if (rate < CONFIG_BOARDCHECK_INT_RATE_MIN) {
        set_result(5, R_FAIL, d,
                   "인터럽트가 오기는 하지만 너무 드뭅니다. 신호가 약하거나 중간에 "
                   "떨어지는 접촉 불량을 의심하세요.");
        return false;
    }
    if (rate > CONFIG_BOARDCHECK_INT_RATE_MAX) {
        set_result(5, R_FAIL, d,
                   "인터럽트가 기대치를 크게 넘습니다. 핀이 노이즈로 떨고 있습니다 — "
                   "배선 길이·접지·풀업 저항을 확인하세요.");
        return false;
    }
    if (timeouts > 0) {
        char h[160];
        snprintf(h, sizeof(h),
                 "측정 중 인터럽트가 %lu회 끊겼습니다(200ms 무응답). 간헐 접촉 불량을 의심하세요.",
                 (unsigned long)timeouts);
        set_result(5, R_WARN, d, h);
        return true;
    }
    set_result(5, R_PASS, d, NULL);
    return true;
}

/* ===================== 보고 ===================== */

static void print_header(void)
{
    printf("\n");
    printf("════════════════════════════════════════════════════════════════\n");
    printf(" IIS3DWB 보드 검사  v%s   (빌드 %s %s)\n",
           BOARDCHECK_VERSION, __DATE__, __TIME__);
    printf("════════════════════════════════════════════════════════════════\n");
    printf(" 핀맵  SPI%d  MOSI=%d MISO=%d SCLK=%d CS=%d @%d Hz\n",
           CONFIG_IIS3DWB_SPI_HOST, CONFIG_IIS3DWB_SPI_MOSI_GPIO,
           CONFIG_IIS3DWB_SPI_MISO_GPIO, CONFIG_IIS3DWB_SPI_SCLK_GPIO,
           CONFIG_IIS3DWB_SPI_CS_GPIO, CONFIG_IIS3DWB_SPI_FREQ_HZ);
    printf("       INT1=IO%d  INT2=IO%d (대조군)  watermark=%d샘플\n",
           INT1_GPIO, INT2_GPIO, WTM_SAMPLES);
    printf("────────────────────────────────────────────────────────────────\n");
    fflush(stdout);
}

static void print_summary(int int2_lv[3], pin_state_t int2_state)
{
    int n_fail = 0, n_warn = 0;
    for (int i = 0; i < N_TESTS; i++) {
        if (R[i].verdict == R_FAIL) n_fail++;
        else if (R[i].verdict == R_WARN) n_warn++;
    }

    printf("────────────────────────────────────────────────────────────────\n");
    if (INT2_GPIO >= 0) {
        const char *s2 = int2_state == PIN_DRIVEN_HIGH ? "HIGH 구동"
                       : int2_state == PIN_DRIVEN_LOW  ? "LOW 구동"
                       : int2_state == PIN_UNDRIVEN    ? "미구동"
                                                       : "판정불가";
        printf(" 참고  INT2(IO%d) 풀업=%d 풀다운=%d → %s\n",
               INT2_GPIO, int2_lv[0], int2_lv[1], s2);
        /* INT1 이 미구동인데 INT2 는 구동되면 커넥터 전체가 아니라 INT1 한 가닥만
         * 끊긴 것이다. 수리 범위를 좁혀 주는 정보라 요약에 올린다. */
        if (R[4].verdict == R_FAIL && int2_state != PIN_UNDRIVEN) {
            printf("       → INT2 는 살아 있으므로 커넥터 전체가 아니라 INT1 한 가닥 문제입니다.\n");
        }
    }

    if (n_fail) {
        printf("────────────────────────────────────────────────────────────────\n");
        printf(" 조치\n");
        for (int i = 0; i < N_TESTS; i++) {
            if (R[i].verdict == R_FAIL && R[i].hint[0]) {
                printf("  · [%s] %s\n", R[i].name, R[i].hint);
            }
        }
    }
    if (n_warn) {
        printf("────────────────────────────────────────────────────────────────\n");
        printf(" 확인 필요\n");
        for (int i = 0; i < N_TESTS; i++) {
            if (R[i].verdict == R_WARN && R[i].hint[0]) {
                printf("  · [%s] %s\n", R[i].name, R[i].hint);
            }
        }
    }

    const char *overall = n_fail ? "FAIL" : (n_warn ? "WARN" : "PASS");
    printf("════════════════════════════════════════════════════════════════\n");
    printf("  판정: %s   (실패 %d · 주의 %d · 전체 %d)\n",
           overall, n_fail, n_warn, N_TESTS);
    printf("════════════════════════════════════════════════════════════════\n");

    /* 호스트 프로그램이 읽는 줄. 사람이 읽는 표가 바뀌어도 이 줄은 고정한다. */
    printf("BOARDCHECK_RESULT=%s\n", overall);
    for (int i = 0; i < N_TESTS; i++) {
        printf("BOARDCHECK_ITEM=%d|%s|%s|%s\n",
               i + 1, R[i].name, verdict_str(R[i].verdict), R[i].detail);
    }
    printf("BOARDCHECK_DONE\n");
    fflush(stdout);

    led_rgb(n_fail != 0, n_fail == 0, false);
}

/* ===================== 진입점 ===================== */

void app_main(void)
{
    /* 드라이버가 찍는 초기화 로그가 검사 표 사이에 섞이면 읽기 어렵다.
     * 경고 이상만 남긴다 — 실패 원인은 검사 표의 detail 에 이미 담긴다. */
    esp_log_level_set("IIS3DWB", ESP_LOG_WARN);
    esp_log_level_set("spi", ESP_LOG_WARN);
    /* 핀진단이 풀업/풀다운을 6번 바꾸는데, gpio 드라이버가 그때마다 설정 한 줄을
     * 찍어 검사 표 한가운데를 비집고 들어온다. 판정에 필요한 레벨 값은 표에 이미 있다. */
    esp_log_level_set("gpio", ESP_LOG_WARN);

    led_init();
    led_rgb(false, false, true);        /* 검사 중 = 파랑 */

    print_header();

    int int2_lv[3] = {-1, -1, -1};
    pin_state_t int2_state = PIN_UNKNOWN;

    bool ok_mcu = test_1_mcu();
    (void)ok_mcu;                        /* MCU 가 달라도 나머지는 계속 본다 */

    if (test_2_spi()) {
        test_3_accel();
        if (test_4_fifo()) {
            bool pin_ok = test_5_int_pin(int2_lv, &int2_state);
            test_6_int_rate(pin_ok);
        } else {
            set_result(4, R_SKIP, "FIFO 검사 실패로 건너뜀", NULL);
            set_result(5, R_SKIP, "FIFO 검사 실패로 건너뜀", NULL);
        }
    } else {
        set_result(2, R_SKIP, "SPI 통신 실패로 건너뜀", NULL);
        set_result(3, R_SKIP, "SPI 통신 실패로 건너뜀", NULL);
        set_result(4, R_SKIP, "SPI 통신 실패로 건너뜀", NULL);
        set_result(5, R_SKIP, "SPI 통신 실패로 건너뜀", NULL);
    }

    print_summary(int2_lv, int2_state);

    ESP_LOGI(TAG, "검사 종료. 다시 검사하려면 보드를 재부팅하세요 (USB 재연결).");
    while (1) {
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}
