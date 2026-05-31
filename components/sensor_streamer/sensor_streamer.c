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
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/ringbuf.h"
#include "esp_log.h"
#include "esp_timer.h"

static const char *TAG = "STREAMER";

/* 링버퍼 크기: 샘플(6B) 다수 보관. WiFi가 잠깐 느려도 버틸 여유. */
#define RINGBUF_SIZE   (16 * 1024)
/* 샘플 1개 = int16 x,y,z = 6바이트 */
#define SAMPLE_BYTES   6

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
} s = {0};

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
            int sent = sendto(s.sock, packet, len, 0,
                              (struct sockaddr *)&s.dest, sizeof(s.dest));
            if (sent < 0) {
                s.stats.send_errors++;
            } else {
                s.stats.packets_sent++;
                s.stats.samples_sent += collected;
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

    /* 태스크 생성: 센서(높은 우선순위) + 전송 */
    xTaskCreate(sensor_task, "strm_sensor", 4096, NULL, 6, &s.sensor_task_h);
    xTaskCreate(tx_task, "strm_tx", 4096, NULL, 5, &s.tx_task_h);

    ESP_LOGI(TAG, "스트리밍 시작 → %s:%u (%lu Hz)",
             cfg->server_ip, cfg->server_port,
             sensor_streamer_rate_hz(cfg->rate_step));
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
