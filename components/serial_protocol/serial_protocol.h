/**
 * @file serial_protocol.h
 * @brief USB 시리얼 JSON 명령 프로토콜 처리기
 *
 * PC 설정 툴과 디바이스 간 USB 시리얼 통신을 담당합니다.
 * 한 줄(개행 종료) = 하나의 JSON 명령 구조이며,
 * 응답은 "#RESP#" 접두사가 붙은 한 줄 JSON으로 반환됩니다.
 * (ESP_LOG 출력과 응답을 구분하기 위한 마커)
 *
 * 지원 명령:
 *   {"cmd":"ping"}                              → 디바이스 식별
 *   {"cmd":"get_info"}                          → 모델/펌웨어/MAC
 *   {"cmd":"scan_wifi"}                         → 주변 WiFi 목록
 *   {"cmd":"set_wifi","ssid":"..","pw":".."}    → WiFi 설정+연결검증+저장
 *   {"cmd":"get_status"}                         → 연결 상태/IP/RSSI
 *   {"cmd":"factory_reset"}                      → 설정 초기화
 */

#ifndef SERIAL_PROTOCOL_H
#define SERIAL_PROTOCOL_H

#ifdef __cplusplus
extern "C" {
#endif

/* 응답 라인 식별 접두사 (PC 툴이 로그와 응답을 구분) */
#define SERIAL_RESP_PREFIX "#RESP#"

/**
 * @brief 시리얼 프로토콜 처리 태스크 시작
 *
 * stdin에서 JSON 명령을 한 줄씩 읽어 처리하는 FreeRTOS 태스크를
 * 생성합니다. app_main에서 1회 호출하며, 디바이스 동작 중 항상
 * 명령을 수신할 수 있도록 백그라운드에서 동작합니다.
 */
void serial_protocol_start(void);

#ifdef __cplusplus
}
#endif

#endif /* SERIAL_PROTOCOL_H */
