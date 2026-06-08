/**
 * @file config_manager.h
 * @brief 디바이스 설정(WiFi 자격증명 등) 영구 저장 관리자
 *
 * NVS(Non-Volatile Storage, Flash ROM)에 WiFi SSID/비밀번호를
 * 저장/로드/초기화하는 기능을 제공합니다.
 *
 * 제품화 목적: 사용자가 PC 설정 툴로 WiFi를 입력하면 이 모듈을 통해
 * ROM에 저장되고, 재부팅 후에도 저장된 값으로 자동 연결됩니다.
 */

#ifndef CONFIG_MANAGER_H
#define CONFIG_MANAGER_H

#include "esp_err.h"
#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* WiFi SSID/비밀번호 최대 길이 (802.11 규격 기준) */
#define CONFIG_MGR_SSID_MAX_LEN     32
#define CONFIG_MGR_PASSWORD_MAX_LEN 64

/**
 * @brief 저장된 WiFi 자격증명
 */
typedef struct {
    char ssid[CONFIG_MGR_SSID_MAX_LEN + 1];          /**< SSID (NULL 종료) */
    char password[CONFIG_MGR_PASSWORD_MAX_LEN + 1];  /**< 비밀번호 (NULL 종료) */
} config_wifi_cred_t;

/**
 * @brief 설정 관리자 초기화 (NVS 초기화)
 *
 * app_main 시작 시 1회 호출. wifi_manager가 별도로 nvs_flash_init을
 * 호출하더라도 중복 초기화는 안전하게 처리됩니다.
 *
 * @return ESP_OK 성공, 그 외 NVS 초기화 실패
 */
esp_err_t config_manager_init(void);

/**
 * @brief 저장된 WiFi 설정이 존재하는지 확인
 *
 * 첫 부팅(설정 없음) vs 정상 동작(설정 있음)을 판별하는 데 사용합니다.
 *
 * @return true: 저장된 SSID 있음, false: 없음
 */
bool config_manager_has_wifi(void);

/**
 * @brief WiFi 자격증명을 NVS(ROM)에 저장
 *
 * @param ssid     WiFi SSID (NULL 불가, 1~32바이트)
 * @param password WiFi 비밀번호 (NULL 가능=빈 문자열 처리, 0~64바이트)
 * @return ESP_OK 성공, ESP_ERR_INVALID_ARG 잘못된 인자, 그 외 NVS 오류
 */
esp_err_t config_manager_save_wifi(const char *ssid, const char *password);

/**
 * @brief NVS(ROM)에서 WiFi 자격증명을 로드
 *
 * @param cred 결과를 담을 구조체 포인터 (NULL 불가)
 * @return ESP_OK 성공, ESP_ERR_NVS_NOT_FOUND 저장된 값 없음, 그 외 NVS 오류
 */
esp_err_t config_manager_load_wifi(config_wifi_cred_t *cred);

/* ===================== 스트리밍 설정 ===================== */

#define CONFIG_MGR_IP_MAX_LEN  15   /* "255.255.255.255" */

/**
 * @brief 센서 데이터 무선 스트리밍 설정
 */
typedef struct {
    char     server_ip[CONFIG_MGR_IP_MAX_LEN + 1]; /**< 라즈베리파이 IP */
    uint16_t server_port;                          /**< 수신 포트 (예: 9000) */
    uint8_t  rate_step;                            /**< 샘플레이트 단계 0~4 */
    uint8_t  transport;                            /**< 0=UDP, 1=TCP */
    uint8_t  read_mode;                            /**< 0=폴링(자동), 1=인터럽트 */
} config_stream_t;

/**
 * @brief 스트리밍 설정이 저장되어 있는지 확인
 * @return true: server_ip 있음, false: 없음
 */
bool config_manager_has_stream(void);

/**
 * @brief 스트리밍 설정을 NVS에 저장
 * @param cfg 저장할 설정 (server_ip 필수)
 * @return ESP_OK 성공, ESP_ERR_INVALID_ARG 잘못된 인자, 그 외 NVS 오류
 */
esp_err_t config_manager_save_stream(const config_stream_t *cfg);

/**
 * @brief NVS에서 스트리밍 설정 로드
 * @param cfg 결과를 담을 구조체 (NULL 불가)
 * @return ESP_OK 성공, ESP_ERR_NVS_NOT_FOUND 없음, 그 외 NVS 오류
 */
esp_err_t config_manager_load_stream(config_stream_t *cfg);

/**
 * @brief 저장된 모든 설정 삭제 (공장 초기화)
 *
 * @return ESP_OK 성공, 그 외 NVS 오류
 */
esp_err_t config_manager_factory_reset(void);

#ifdef __cplusplus
}
#endif

#endif /* CONFIG_MANAGER_H */
