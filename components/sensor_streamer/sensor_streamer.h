/**
 * @file sensor_streamer.h
 * @brief IIS3DWB 센서 데이터 WiFi 무선 스트리밍
 *
 * 센서에서 가속도 데이터를 읽어 UDP(기본)로 서버(라즈베리파이)에 전송합니다.
 * 생산자(sensor_task) - 소비자(tx_task) 구조 + 링버퍼로,
 * SPI 읽기와 WiFi 전송이 서로 막지 않도록 분리되어 있습니다.
 *
 * 설계: docs/02-design/features/wifi-sensor-streaming.design.md
 */

#ifndef SENSOR_STREAMER_H
#define SENSOR_STREAMER_H

#include "esp_err.h"
#include <stdint.h>
#include <stdbool.h>
#include "iis3dwb.h"

#ifdef __cplusplus
extern "C" {
#endif

/* 패킷 프로토콜 (수신 프로그램과 일치해야 함) */
#define STREAM_MAGIC        0x49495333u  /* "IIS3" */
#define STREAM_PROTO_VER    1
#define STREAM_SAMPLES_PER_PACKET 200    /* 200샘플 × 6B = 1200B + 16B 헤더 */

/* 전송 프로토콜 */
typedef enum {
    STREAM_TRANSPORT_UDP = 0,
    STREAM_TRANSPORT_TCP = 1,
} stream_transport_type_t;

/**
 * @brief 스트리머 설정
 */
typedef struct {
    iis3dwb_handle_t *sensor;   /**< 초기화된 센서 핸들 */
    const char *server_ip;      /**< 수신 서버 IP */
    uint16_t server_port;       /**< 수신 서버 포트 */
    uint8_t rate_step;          /**< 샘플레이트 단계 0~4 */
    stream_transport_type_t transport; /**< 전송 프로토콜 */
    uint8_t read_mode;          /**< 0=폴링(자동), 1=인터럽트 (FIFO 모드에서만 적용) */
} sensor_streamer_config_t;

/**
 * @brief 스트리밍 통계
 */
typedef struct {
    uint32_t packets_sent;   /**< 전송한 패킷 수 */
    uint32_t samples_sent;   /**< 전송한 샘플 수 */
    uint32_t dropped;        /**< 링버퍼 오버런으로 버린 샘플 수 */
    uint32_t send_errors;    /**< 전송 실패 횟수 */
    uint32_t int_count;      /**< FIFO watermark INT 발생 횟수 (인터럽트 모드 진단) */
} sensor_streamer_stats_t;

/**
 * @brief rate_step(0~4)에 대응하는 유효 샘플레이트(Hz) 반환
 */
uint32_t sensor_streamer_rate_hz(uint8_t rate_step);

/**
 * @brief 스트리밍 시작 (sensor_task + tx_task 생성, 소켓 오픈)
 *
 * @param cfg 설정 (sensor, server_ip 필수)
 * @return ESP_OK 성공, 그 외 실패
 */
esp_err_t sensor_streamer_start(const sensor_streamer_config_t *cfg);

/**
 * @brief 스트리밍 정지 (태스크 종료, 소켓 닫기)
 */
void sensor_streamer_stop(void);

/**
 * @brief 현재 통계 조회
 */
void sensor_streamer_get_stats(sensor_streamer_stats_t *out);

#ifdef __cplusplus
}
#endif

#endif /* SENSOR_STREAMER_H */
