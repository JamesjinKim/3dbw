/**
 * @file wifi_manager.h
 * @brief WiFi Manager Component for ESP32
 *
 * This component manages WiFi connectivity in Station (STA) mode,
 * including connection, reconnection, and status monitoring.
 */

#ifndef WIFI_MANAGER_H
#define WIFI_MANAGER_H

#include "esp_err.h"
#include "esp_event.h"
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief WiFi connection status
 */
typedef enum {
    WIFI_STATUS_DISCONNECTED = 0,  /**< WiFi is disconnected */
    WIFI_STATUS_CONNECTING,        /**< WiFi is connecting */
    WIFI_STATUS_CONNECTED,         /**< WiFi is connected and has IP */
    WIFI_STATUS_FAILED             /**< WiFi connection failed */
} wifi_status_t;

/**
 * @brief WiFi manager configuration
 */
typedef struct {
    const char *ssid;              /**< WiFi SSID */
    const char *password;          /**< WiFi password */
    uint8_t max_retry;             /**< Maximum retry attempts */
    uint8_t auth_mode_threshold;   /**< Minimum authentication mode */
    bool auto_connect;             /**< true=시작 시 자동 연결/재시도, false=대기(스캔만) */
} wifi_manager_config_t;

/**
 * @brief Initialize WiFi manager and connect to AP
 *
 * This function initializes the WiFi subsystem, configures the station mode,
 * and attempts to connect to the specified Access Point.
 *
 * @param config Pointer to WiFi manager configuration
 * @return
 *     - ESP_OK: Success
 *     - ESP_ERR_INVALID_ARG: Invalid configuration
 *     - ESP_FAIL: WiFi initialization failed
 */
esp_err_t wifi_manager_init(const wifi_manager_config_t *config);

/**
 * @brief Disconnect and deinitialize WiFi
 *
 * @return
 *     - ESP_OK: Success
 *     - ESP_FAIL: Failed to disconnect
 */
esp_err_t wifi_manager_deinit(void);

/**
 * @brief Get current WiFi connection status
 *
 * @return Current WiFi status
 */
wifi_status_t wifi_manager_get_status(void);

/**
 * @brief Check if WiFi is connected
 *
 * @return
 *     - true: WiFi is connected
 *     - false: WiFi is not connected
 */
bool wifi_manager_is_connected(void);

/**
 * @brief Get IP address as string
 *
 * @param ip_str Buffer to store IP address string (minimum 16 bytes)
 * @param max_len Maximum length of the buffer
 * @return
 *     - ESP_OK: Success
 *     - ESP_ERR_INVALID_ARG: Invalid argument
 *     - ESP_ERR_INVALID_STATE: WiFi not connected
 */
esp_err_t wifi_manager_get_ip_string(char *ip_str, size_t max_len);

/**
 * @brief Wait for WiFi connection with timeout
 *
 * @param timeout_ms Timeout in milliseconds (0 = no timeout)
 * @return
 *     - ESP_OK: Connected successfully
 *     - ESP_ERR_TIMEOUT: Connection timeout
 *     - ESP_FAIL: Connection failed
 */
esp_err_t wifi_manager_wait_for_connection(uint32_t timeout_ms);

/**
 * @brief Scan for available WiFi networks
 *
 * @param max_aps Maximum number of APs to scan (0 = scan all)
 * @return
 *     - ESP_OK: Scan completed successfully
 *     - ESP_FAIL: Scan failed
 */
esp_err_t wifi_manager_scan_networks(uint16_t max_aps);

/**
 * @brief 스캔 결과 1개 항목 (설정 툴 전달용)
 */
typedef struct {
    char ssid[33];   /**< SSID (NULL 종료) */
    int8_t rssi;     /**< 신호 강도 (dBm) */
    uint8_t channel; /**< 채널 */
    uint8_t authmode; /**< wifi_auth_mode_t 값 */
} wifi_scan_result_t;

/**
 * @brief WiFi 스캔 후 결과를 배열로 반환 (로그 출력 없이 데이터만)
 *
 * 설정 툴에 전달할 SSID 목록을 얻기 위해 사용합니다.
 *
 * @param results   결과를 담을 배열 (호출자가 할당)
 * @param max_count results 배열 크기
 * @param out_count [out] 실제 반환된 항목 수
 * @return ESP_OK 성공, 그 외 실패
 */
esp_err_t wifi_manager_scan_results(wifi_scan_result_t *results,
                                    uint16_t max_count, uint16_t *out_count);

/**
 * @brief 런타임에 새로운 WiFi 자격증명으로 (재)연결을 시도
 *
 * wifi_manager_init()이 이미 호출되어 WiFi 스택이 초기화된 상태에서,
 * 동작 중에 다른 SSID/비밀번호로 연결을 바꿀 때 사용합니다.
 * (PC 설정 툴의 set_wifi 명령 처리에 사용)
 *
 * @param ssid     새 SSID
 * @param password 새 비밀번호 (NULL=오픈)
 * @return ESP_OK 연결 시도 시작됨. 실제 연결 결과는
 *         wifi_manager_wait_for_connection()으로 확인
 */
esp_err_t wifi_manager_connect(const char *ssid, const char *password);

/**
 * @brief 현재 연결된 AP의 신호 강도(RSSI) 조회
 *
 * @param out_rssi [out] RSSI 값 (dBm)
 * @return ESP_OK 성공, ESP_ERR_INVALID_STATE 미연결
 */
esp_err_t wifi_manager_get_rssi(int8_t *out_rssi);

#ifdef __cplusplus
}
#endif

#endif /* WIFI_MANAGER_H */
