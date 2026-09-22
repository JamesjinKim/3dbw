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
#define STREAM_PROTO_VER    2
#define STREAM_SAMPLES_PER_PACKET 200    /* 200샘플 × 6B = 1200B + 18B 헤더 = 1218B */

/* 전송 프로토콜 */
typedef enum {
    STREAM_TRANSPORT_UDP    = 0,
    STREAM_TRANSPORT_TCP    = 1,
    STREAM_TRANSPORT_SERIAL = 2,   /* USB 직결 — 로그를 끄고 패킷만 전송 */
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
    uint8_t full_scale_g;       /**< 측정 범위 2/4/8/16 (g) — 패킷 헤더에 실어 보냄 */
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
 * @param cfg 설정 (sensor 필수, server_ip는 시리얼 전송이 아닐 때 필수)
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

/**
 * @brief 시리얼(USB 직결) 스트리밍이 동작 중인지 여부
 *
 * 시리얼 모드에서는 USB Serial/JTAG 엔드포인트를 패킷 스트림이 독점한다.
 * 이때 다른 컴포넌트가 printf 등으로 같은 엔드포인트에 무언가를 쓰면
 * 전송 중인 패킷 한가운데에 바이트가 끼어들어 그 패킷이 깨진다.
 * (로그 레벨을 NONE으로 낮춰도 printf 는 막히지 않는다 — 레벨 검사는
 *  esp_log_writev 안에 있고, 콘솔 VFS 의 USB 복제는 그와 무관하다.)
 * 공유 엔드포인트에 쓰기 전에 이 술어로 확인하라고 공개한 함수다.
 *
 * @return true 시리얼 스트리밍 중 (USB 포트에 쓰지 말 것)
 */
bool sensor_streamer_is_serial_streaming(void);

#ifdef __cplusplus
}
#endif

#endif /* SENSOR_STREAMER_H */
