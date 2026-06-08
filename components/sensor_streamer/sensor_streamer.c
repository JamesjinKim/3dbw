/**
 * @file sensor_streamer.c
 * @brief IIS3DWB 센서 데이터 WiFi 무선 스트리밍 구현
 *
 * [센서] →SPI→ sensor_task →(ringbuf)→ tx_task →UDP→ [서버]
 *
 * 1단계 구현: 저속 폴링 전송 (UDP).
 * 고속 FIFO 묶음 읽기는 후속 단계에서 추가 (설계 6장 참고).
 */

#include "sensor_streamer.h"

#include <string.h>
#include <errno.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/ringbuf.h"
#include "freertos/semphr.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "sdkconfig.h"

static const char *TAG = "STREAMER";

/* INT1 GPIO (Kconfig). -1 = 인터럽트 미사용(효율 폴링) */
#ifdef CONFIG_IIS3DWB_INT1_GPIO
#define INT1_GPIO  CONFIG_IIS3DWB_INT1_GPIO
#else
#define INT1_GPIO  (-1)
#endif

/* INT 대기 타임아웃(ms)과, 연속 미발생 시 폴링 fallback 임계 */
#define FIFO_INT_TIMEOUT_MS  50
#define FIFO_INT_MISS_MAX    20   /* ≈1초 미발생 → fallback */

/* 링버퍼 크기: 샘플(6B) 다수 보관. WiFi가 잠깐 느려도 버틸 여유. */
#define RINGBUF_SIZE   (16 * 1024)
/* 샘플 1개 = int16 x,y,z = 6바이트 */
#define SAMPLE_BYTES   6
/* FIFO 효율 폴링용 묶음 크기 (폴링 fallback 시): 32샘플 ≈ 1.2ms 주기 */
#define STREAM_WTM_SAMPLES  32
/* INT 모드 watermark: 한 INT에 한 버스트(BURST_MAX=64)로 깔끔히 비우도록 64로 맞춤.
 * (128로 하면 한 INT에 64만 읽혀 FIFO가 차고 덮어써짐 → 실효레이트 저하)
 * 64샘플 ≈ 2.4ms → 초당 ~416 INT, 무난한 부하. */
#define STREAM_INT_WTM      IIS3DWB_FIFO_BURST_MAX  /* 64 */

/* 패킷 헤더 (16바이트) — 수신 프로그램과 바이트 단위로 일치해야 함 */
typedef struct __attribute__((packed)) {
    uint32_t magic;        /* STREAM_MAGIC */
    uint8_t  version;      /* STREAM_PROTO_VER */
    uint8_t  rate_step;    /* 0~4 */
    uint16_t sample_count; /* 이 패킷의 샘플 수 */
    uint32_t seq;          /* 패킷 시퀀스 번호 */
    uint32_t timestamp_ms; /* 부팅 후 ms */
} stream_header_t;

/* 모듈 상태 */
static struct {
    bool running;
    sensor_streamer_config_t cfg;
    RingbufHandle_t ringbuf;
    int sock;
    struct sockaddr_in dest;
    TaskHandle_t sensor_task_h;
    TaskHandle_t tx_task_h;
    sensor_streamer_stats_t stats;
    uint32_t seq;
    /* FIFO watermark 인터럽트 */
    SemaphoreHandle_t fifo_sem;  /* ISR → 태스크 깨움 */
    bool int_enabled;            /* INT 모드 사용 중인지 (fallback 시 false) */
    uint32_t int_count;          /* INT 발생 횟수 (진단) */
} s = {0};

/* FIFO watermark ISR: 세마포어 give만 (SPI 호출 금지) */
static void IRAM_ATTR fifo_isr(void *arg)
{
    s.int_count++;
    BaseType_t hpw = pdFALSE;
    xSemaphoreGiveFromISR(s.fifo_sem, &hpw);
    if (hpw) portYIELD_FROM_ISR();
}

uint32_t sensor_streamer_rate_hz(uint8_t rate_step)
{
    switch (rate_step) {
        case 0: return 1000;    /* 1 kHz */
        case 1: return 3333;    /* 3.3 kHz */
        case 2: return 6667;    /* 6.6 kHz */
        case 3: return 13333;   /* 13.3 kHz */
        case 4: return 26667;   /* 26.6 kHz */
        default: return 1000;
    }
}

/* ===================== 센서 태스크 (생산자) ===================== */
/*
 * 설정 레이트로 센서 raw 데이터를 읽어 링버퍼에 적재.
 * 1단계: 폴링 + vTaskDelay 기반. (고속 단계는 FIFO로 대체 예정)
 */
static void sensor_task(void *arg)
{
    uint32_t rate = sensor_streamer_rate_hz(s.cfg.rate_step);
    /* 폴링 주기 (us). 1kHz=1000us. 고속은 폴링 한계가 있어 후속 FIFO 필요. */
    uint32_t period_us = 1000000u / rate;
    if (period_us == 0) period_us = 1;

    ESP_LOGI(TAG, "센서 태스크 시작 (목표 %lu Hz, 주기 %lu us)", rate, period_us);

    int64_t next = esp_timer_get_time();
    iis3dwb_raw_data_t raw;

    while (s.running) {
        if (iis3dwb_read_raw_data(s.cfg.sensor, &raw) == ESP_OK) {
            uint8_t sample[SAMPLE_BYTES];
            memcpy(&sample[0], &raw.x, 2);
            memcpy(&sample[2], &raw.y, 2);
            memcpy(&sample[4], &raw.z, 2);

            /* 링버퍼에 적재. 가득 차면(소비자가 못 따라감) 드롭. */
            if (xRingbufferSend(s.ringbuf, sample, SAMPLE_BYTES, 0) != pdTRUE) {
                s.stats.dropped++;
            }
        }

        /* 다음 샘플 시각까지 대기 (정밀 주기 유지 시도) */
        next += period_us;
        int64_t now = esp_timer_get_time();
        int64_t wait = next - now;
        if (wait > 1000) {
            vTaskDelay(pdMS_TO_TICKS(wait / 1000));
        } else if (wait < -100000) {
            /* 너무 밀리면 따라잡기 포기하고 기준 리셋 */
            next = now;
        }
    }
    ESP_LOGI(TAG, "센서 태스크 종료");
    vTaskDelete(NULL);
}

/* ===================== 센서 태스크 (생산자, FIFO 고속) ===================== */
/*
 * rate_step >= 1 일 때 사용. 센서 FIFO(26.6kHz)를 버스트로 읽고,
 * 소프트웨어 데시메이션(N개당 1개)으로 목표 레이트를 만든다.
 * (IIS3DWB는 ODR/BDR이 26.6kHz 고정 → 중간 단계는 데시메이션으로 구현)
 */
static uint8_t rate_to_decim(uint8_t rate_step)
{
    switch (rate_step) {
        case 1: return 8;   /* 26667/8 ≈ 3333 Hz */
        case 2: return 4;   /* 26667/4 ≈ 6667 Hz */
        case 3: return 2;   /* 26667/2 ≈ 13333 Hz */
        default: return 1;  /* 4 = 모두 전송 (26.6kHz) */
    }
}

static void sensor_task_fifo(void *arg)
{
    uint8_t decim = rate_to_decim(s.cfg.rate_step);
    uint32_t phase = 0;

    ESP_LOGI(TAG, "센서 태스크(FIFO) 시작 (26.6kHz, 데시메이션 1/%u)", decim);

    if (iis3dwb_fifo_enable(s.cfg.sensor, IIS3DWB_BDR_26667) != ESP_OK) {
        ESP_LOGE(TAG, "FIFO 활성화 실패");
        s.running = false;
        vTaskDelete(NULL);
        return;
    }

    /* === FIFO watermark 인터럽트 설정 (INT1 GPIO 유효 시) === */
    s.int_enabled = false;
    if (INT1_GPIO >= 0) {
        s.fifo_sem = xSemaphoreCreateBinary();
        if (s.fifo_sem) {
            gpio_config_t io = {
                .pin_bit_mask = 1ULL << INT1_GPIO,
                .mode = GPIO_MODE_INPUT,
                .intr_type = GPIO_INTR_POSEDGE,
                .pull_down_en = GPIO_PULLDOWN_ENABLE,
                .pull_up_en = GPIO_PULLUP_DISABLE,
            };
            gpio_config(&io);
            /* ISR 서비스 (이미 설치돼 있으면 INVALID_STATE 무시) */
            esp_err_t isr_ret = gpio_install_isr_service(0);
            if (isr_ret == ESP_OK || isr_ret == ESP_ERR_INVALID_STATE) {
                gpio_isr_handler_add(INT1_GPIO, fifo_isr, NULL);
                /* 센서 측: WTM 설정 + INT1 라우팅 */
                iis3dwb_fifo_set_watermark(s.cfg.sensor, STREAM_INT_WTM);
                iis3dwb_fifo_route_int1(s.cfg.sensor, true);
                s.int_enabled = true;
                ESP_LOGI(TAG, "FIFO 인터럽트 모드 (INT1=IO%d, WTM=%d)",
                         INT1_GPIO, STREAM_INT_WTM);
            }
        }
    }
    if (!s.int_enabled) {
        ESP_LOGI(TAG, "FIFO 효율 폴링 모드 (INT 미사용)");
    }

    iis3dwb_raw_data_t burst[IIS3DWB_FIFO_BURST_MAX];

    /* 효율 폴링용 대기 시간 (INT 미사용 시) */
    uint32_t batch_ms = (STREAM_WTM_SAMPLES * 1000u) / 26667u;
    TickType_t poll_wait = pdMS_TO_TICKS(batch_ms);
    if (poll_wait < 1) poll_wait = 1;
    uint32_t int_miss = 0;

    while (s.running) {
        if (s.int_enabled) {
            /* INT 대기: watermark 도달 시 ISR이 깨움. timeout으로 미발생 감지 */
            bool got = (xSemaphoreTake(s.fifo_sem,
                        pdMS_TO_TICKS(FIFO_INT_TIMEOUT_MS)) == pdTRUE);
            if (!got) {
                /* INT 안 옴 → fallback 카운트 (오배선/오설정 대비) */
                uint16_t a = 0;
                iis3dwb_fifo_count(s.cfg.sensor, &a);
                if (a == 0 && ++int_miss > FIFO_INT_MISS_MAX) {
                    s.int_enabled = false;
                    ESP_LOGW(TAG, "INT 미발생 → 효율 폴링 fallback");
                }
            } else {
                int_miss = 0;
            }
        } else {
            vTaskDelay(poll_wait);  /* 효율 폴링 (fallback 또는 INT 미사용) */
        }

        uint16_t avail = 0;
        if (iis3dwb_fifo_count(s.cfg.sensor, &avail) != ESP_OK) {
            continue;
        }
        /* 쌓인 만큼 버스트로 모두 비움 */
        while (avail > 0 && s.running) {
            uint16_t want = avail > IIS3DWB_FIFO_BURST_MAX ? IIS3DWB_FIFO_BURST_MAX : avail;
            uint16_t got = 0;
            if (iis3dwb_read_fifo(s.cfg.sensor, burst, want, &got) != ESP_OK || got == 0) {
                break;
            }
            for (uint16_t i = 0; i < got; i++) {
                if ((phase++ % decim) != 0) continue;  /* 데시메이션 */
                uint8_t sample[SAMPLE_BYTES];
                memcpy(&sample[0], &burst[i].x, 2);
                memcpy(&sample[2], &burst[i].y, 2);
                memcpy(&sample[4], &burst[i].z, 2);
                if (xRingbufferSend(s.ringbuf, sample, SAMPLE_BYTES, 0) != pdTRUE) {
                    s.stats.dropped++;
                }
            }
            avail -= got;
        }
    }

    /* INT 정리 */
    if (INT1_GPIO >= 0) {
        iis3dwb_fifo_route_int1(s.cfg.sensor, false);
        gpio_isr_handler_remove(INT1_GPIO);
    }
    if (s.fifo_sem) {
        vSemaphoreDelete(s.fifo_sem);
        s.fifo_sem = NULL;
    }
    iis3dwb_fifo_disable(s.cfg.sensor);
    ESP_LOGI(TAG, "센서 태스크(FIFO) 종료");
    vTaskDelete(NULL);
}

/* ===================== 전송 태스크 (소비자) ===================== */
/*
 * 링버퍼에서 샘플을 모아 패킷(헤더+샘플배열)으로 조립 후 UDP 전송.
 */
static void tx_task(void *arg)
{
    ESP_LOGI(TAG, "전송 태스크 시작 (서버 %s:%u)",
             s.cfg.server_ip, s.cfg.server_port);

    /* 패킷 버퍼: 헤더 + 최대 샘플 */
    static uint8_t packet[sizeof(stream_header_t) + STREAM_SAMPLES_PER_PACKET * SAMPLE_BYTES];
    uint16_t collected = 0;  /* 모은 샘플 수 */
    uint8_t *payload = packet + sizeof(stream_header_t);

    while (s.running) {
        size_t item_size = 0;
        /* 링버퍼에서 1샘플 꺼내기 (최대 100ms 대기) */
        void *item = xRingbufferReceive(s.ringbuf, &item_size, pdMS_TO_TICKS(100));
        if (item != NULL) {
            if (item_size == SAMPLE_BYTES && collected < STREAM_SAMPLES_PER_PACKET) {
                memcpy(payload + collected * SAMPLE_BYTES, item, SAMPLE_BYTES);
                collected++;
            }
            vRingbufferReturnItem(s.ringbuf, item);
        }

        /* 패킷이 가득 찼으면 전송 */
        if (collected >= STREAM_SAMPLES_PER_PACKET) {
            stream_header_t *h = (stream_header_t *)packet;
            h->magic = STREAM_MAGIC;
            h->version = STREAM_PROTO_VER;
            h->rate_step = s.cfg.rate_step;
            h->sample_count = collected;
            h->seq = s.seq++;
            h->timestamp_ms = (uint32_t)(esp_timer_get_time() / 1000);

            size_t len = sizeof(stream_header_t) + collected * SAMPLE_BYTES;
            int sent = -1;
            /* ENOMEM(lwip TX 버퍼 일시 부족) 시 양보하며 재시도.
             * INT 모드는 버스트로 몰려 순간 버퍼 고갈이 잦음 → 최대 8회 재시도. */
            for (int attempt = 0; attempt < 8; attempt++) {
                sent = sendto(s.sock, packet, len, 0,
                              (struct sockaddr *)&s.dest, sizeof(s.dest));
                if (sent >= 0) break;
                if (errno != ENOMEM) break;   /* 다른 오류는 재시도 무의미 */
                vTaskDelay(1);                 /* 버퍼 회복 대기 */
            }
            if (sent >= 0) {
                s.stats.packets_sent++;
                s.stats.samples_sent += collected;
            } else {
                s.stats.send_errors++;
            }
            collected = 0;
        }
    }
    ESP_LOGI(TAG, "전송 태스크 종료");
    vTaskDelete(NULL);
}

/* ===================== 공개 API ===================== */

esp_err_t sensor_streamer_start(const sensor_streamer_config_t *cfg)
{
    if (!cfg || !cfg->sensor || !cfg->server_ip) {
        return ESP_ERR_INVALID_ARG;
    }
    if (s.running) {
        ESP_LOGW(TAG, "이미 실행 중");
        return ESP_ERR_INVALID_STATE;
    }

    memset(&s, 0, sizeof(s));
    s.cfg = *cfg;
    s.sock = -1;

    /* 현재는 UDP만 구현 (TCP는 후속) */
    if (cfg->transport != STREAM_TRANSPORT_UDP) {
        ESP_LOGW(TAG, "TCP는 아직 미구현 — UDP로 진행");
        s.cfg.transport = STREAM_TRANSPORT_UDP;
    }

    /* UDP 소켓 생성 */
    s.sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (s.sock < 0) {
        ESP_LOGE(TAG, "소켓 생성 실패");
        return ESP_FAIL;
    }
    memset(&s.dest, 0, sizeof(s.dest));
    s.dest.sin_family = AF_INET;
    s.dest.sin_port = htons(cfg->server_port);
    if (inet_aton(cfg->server_ip, &s.dest.sin_addr) == 0) {
        ESP_LOGE(TAG, "잘못된 서버 IP: %s", cfg->server_ip);
        close(s.sock);
        s.sock = -1;
        return ESP_ERR_INVALID_ARG;
    }

    /* 링버퍼 (바이트 단위, NO_SPLIT로 샘플 경계 유지) */
    s.ringbuf = xRingbufferCreate(RINGBUF_SIZE, RINGBUF_TYPE_NOSPLIT);
    if (!s.ringbuf) {
        ESP_LOGE(TAG, "링버퍼 생성 실패");
        close(s.sock);
        s.sock = -1;
        return ESP_ERR_NO_MEM;
    }

    s.running = true;

    /* 태스크 생성: 센서(높은 우선순위) + 전송.
     * rate_step==0 → 폴링(검증된 1kHz 경로), >=1 → FIFO 고속 버스트. */
    if (cfg->rate_step == 0) {
        xTaskCreate(sensor_task, "strm_sensor", 4096, NULL, 6, &s.sensor_task_h);
    } else {
        xTaskCreate(sensor_task_fifo, "strm_fifo", 4096, NULL, 6, &s.sensor_task_h);
    }
    xTaskCreate(tx_task, "strm_tx", 4096, NULL, 5, &s.tx_task_h);

    ESP_LOGI(TAG, "스트리밍 시작 → %s:%u (%lu Hz, %s)",
             cfg->server_ip, cfg->server_port,
             sensor_streamer_rate_hz(cfg->rate_step),
             cfg->rate_step == 0 ? "폴링" : "FIFO");
    return ESP_OK;
}

void sensor_streamer_stop(void)
{
    if (!s.running) return;
    s.running = false;
    /* 태스크가 self-delete 하도록 잠시 대기 */
    vTaskDelay(pdMS_TO_TICKS(200));
    if (s.sock >= 0) {
        close(s.sock);
        s.sock = -1;
    }
    if (s.ringbuf) {
        vRingbufferDelete(s.ringbuf);
        s.ringbuf = NULL;
    }
    ESP_LOGI(TAG, "스트리밍 정지");
}

void sensor_streamer_get_stats(sensor_streamer_stats_t *out)
{
    if (out) *out = s.stats;
}
