/**
 * @file wifi_manager.c
 * @brief WiFi Manager Component Implementation
 *
 * NOTE: Event handlers must NOT block (no vTaskDelay).
 * Retry logic uses a dedicated FreeRTOS timer instead.
 */

#include "wifi_manager.h"
#include "esp_wifi.h"
#include "esp_log.h"
#include "esp_event.h"
#include "nvs_flash.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/timers.h"
#include "lwip/inet.h"
#include <string.h>

static const char *TAG = "WIFI_MGR";

/* FreeRTOS event group to signal when we are connected */
static EventGroupHandle_t s_wifi_event_group;

/* Event bits */
#define WIFI_CONNECTED_BIT BIT0
#define WIFI_FAIL_BIT      BIT1

/* Retry timer */
static TimerHandle_t s_retry_timer;

/* WiFi manager state */
static struct {
    wifi_status_t status;
    uint8_t retry_count;
    uint8_t max_retry;
    bool auto_connect;     /* false면 자동 연결/재시도 안 함 (미설정 대기 모드) */
    esp_netif_t *netif;
    char ip_address[16];
} s_wifi_mgr = {
    .status = WIFI_STATUS_DISCONNECTED,
    .retry_count = 0,
    .max_retry = 5,
    .auto_connect = true,
    .netif = NULL,
    .ip_address = {0}
};

/**
 * @brief Timer callback for WiFi retry (runs in timer task, safe to call esp_wifi_connect)
 */
static void wifi_retry_timer_cb(TimerHandle_t xTimer)
{
    ESP_LOGI(TAG, "Retrying connection (attempt %d)...",
             s_wifi_mgr.retry_count);
    s_wifi_mgr.status = WIFI_STATUS_CONNECTING;
    esp_wifi_connect();
}

/**
 * @brief WiFi event handler - must NOT block
 */
static void wifi_event_handler(void *arg, esp_event_base_t event_base,
                                int32_t event_id, void *event_data)
{
    if (event_base == WIFI_EVENT) {
        switch (event_id) {
            case WIFI_EVENT_STA_START:
                if (!s_wifi_mgr.auto_connect) {
                    /* 미설정 대기 모드: 자동 연결하지 않음 (스캔만 가능) */
                    ESP_LOGI(TAG, "WiFi started (대기 모드 — 자동 연결 안 함)");
                    break;
                }
                ESP_LOGI(TAG, "WiFi started, connecting...");
                s_wifi_mgr.status = WIFI_STATUS_CONNECTING;
                esp_wifi_connect();
                break;

            case WIFI_EVENT_STA_DISCONNECTED:
                {
                    wifi_event_sta_disconnected_t *event =
                        (wifi_event_sta_disconnected_t *)event_data;
                    ESP_LOGW(TAG, "Disconnected (reason: %d)", event->reason);

                    /* 자동 연결 모드가 아니면 재시도하지 않음 */
                    if (!s_wifi_mgr.auto_connect) {
                        s_wifi_mgr.status = WIFI_STATUS_DISCONNECTED;
                        break;
                    }

                    s_wifi_mgr.retry_count++;

                    // Retry with delay based on reason
                    uint32_t delay_sec;
                    switch (event->reason) {
                        case 15:  // 4WAY_HANDSHAKE_TIMEOUT
                        case 4:   // ASSOC_EXPIRE
                        case 204: // CONNECTION_FAIL
                            delay_sec = 1;
                            break;
                        case 205: // NO_AP_FOUND
                            delay_sec = 5;
                            break;
                        default:
                            delay_sec = 2;
                            break;
                    }

                    ESP_LOGW(TAG, "Will retry in %lu seconds (attempt %d)...",
                             delay_sec, s_wifi_mgr.retry_count);
                    xTimerChangePeriod(s_retry_timer,
                                       pdMS_TO_TICKS(delay_sec * 1000),
                                       0);
                    xTimerStart(s_retry_timer, 0);
                }
                break;

            case WIFI_EVENT_STA_CONNECTED:
                {
                    wifi_event_sta_connected_t *event =
                        (wifi_event_sta_connected_t *)event_data;
                    ESP_LOGI(TAG, "Connected to AP (SSID: %s, Channel: %d)",
                             event->ssid, event->channel);

                    wifi_ap_record_t ap_info;
                    if (esp_wifi_sta_get_ap_info(&ap_info) == ESP_OK) {
                        ESP_LOGI(TAG, "  RSSI: %d dBm", ap_info.rssi);
                    }
                }
                break;

            default:
                break;
        }
    } else if (event_base == IP_EVENT) {
        if (event_id == IP_EVENT_STA_GOT_IP) {
            ip_event_got_ip_t *event = (ip_event_got_ip_t *)event_data;
            snprintf(s_wifi_mgr.ip_address, sizeof(s_wifi_mgr.ip_address),
                     IPSTR, IP2STR(&event->ip_info.ip));
            ESP_LOGI(TAG, "Got IP address: %s", s_wifi_mgr.ip_address);
            s_wifi_mgr.retry_count = 0;
            s_wifi_mgr.status = WIFI_STATUS_CONNECTED;
            xTimerStop(s_retry_timer, 0);  // Cancel any pending retry
            xEventGroupSetBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
        }
    }
}

esp_err_t wifi_manager_init(const wifi_manager_config_t *config)
{
    if (!config || !config->ssid) {
        ESP_LOGE(TAG, "Invalid configuration");
        return ESP_ERR_INVALID_ARG;
    }

    ESP_LOGI(TAG, "Initializing WiFi manager...");

    /* Initialize NVS */
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_LOGW(TAG, "Erasing NVS flash...");
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to initialize NVS: %s", esp_err_to_name(ret));
        return ret;
    }

    /* Initialize TCP/IP stack */
    ESP_ERROR_CHECK(esp_netif_init());

    /* Create default event loop */
    ESP_ERROR_CHECK(esp_event_loop_create_default());

    /* Create WiFi event group */
    s_wifi_event_group = xEventGroupCreate();
    if (!s_wifi_event_group) {
        ESP_LOGE(TAG, "Failed to create event group");
        return ESP_FAIL;
    }

    /* Create retry timer (one-shot) */
    s_retry_timer = xTimerCreate("wifi_retry", pdMS_TO_TICKS(1000),
                                  pdFALSE, NULL, wifi_retry_timer_cb);
    if (!s_retry_timer) {
        ESP_LOGE(TAG, "Failed to create retry timer");
        return ESP_FAIL;
    }

    /* Create default WiFi STA interface */
    s_wifi_mgr.netif = esp_netif_create_default_wifi_sta();
    if (!s_wifi_mgr.netif) {
        ESP_LOGE(TAG, "Failed to create netif");
        return ESP_FAIL;
    }

    /* Initialize WiFi with default config */
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    /* Register event handlers */
    esp_event_handler_instance_t instance_any_id;
    esp_event_handler_instance_t instance_got_ip;
    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT,
                                                         ESP_EVENT_ANY_ID,
                                                         &wifi_event_handler,
                                                         NULL,
                                                         &instance_any_id));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT,
                                                         IP_EVENT_STA_GOT_IP,
                                                         &wifi_event_handler,
                                                         NULL,
                                                         &instance_got_ip));

    /* Configure WiFi - follows ESP-IDF example order: mode -> config -> start */
    wifi_config_t wifi_config = {0};
    strncpy((char *)wifi_config.sta.ssid, config->ssid, sizeof(wifi_config.sta.ssid) - 1);
    if (config->password) {
        strncpy((char *)wifi_config.sta.password, config->password, sizeof(wifi_config.sta.password) - 1);
    }
    wifi_config.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;
    wifi_config.sta.sae_pwe_h2e = WPA3_SAE_PWE_BOTH;
    wifi_config.sta.sae_h2e_identifier[0] = '\0';
    wifi_config.sta.pmf_cfg.capable = true;
    wifi_config.sta.pmf_cfg.required = false;

    /* Store max retry count (0 = infinite retry) */
    s_wifi_mgr.max_retry = config->max_retry;
    /* 자동 연결 여부 (미설정 대기 모드면 false로 들어옴) */
    s_wifi_mgr.auto_connect = config->auto_connect;

    /* ESP-IDF example order: set_mode -> set_config -> start */
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
    ESP_ERROR_CHECK(esp_wifi_start());
    /* esp_wifi_connect() is called from STA_START event handler */

    /* Set TX power to maximum (20.5 dBm) for weak signal environments */
    int8_t tx_power = 84;  // 84 = 20.5 dBm (unit: 0.25 dBm)
    esp_wifi_set_max_tx_power(tx_power);
    esp_wifi_get_max_tx_power(&tx_power);
    ESP_LOGI(TAG, "WiFi TX power: %.2f dBm", tx_power * 0.25);

    ESP_LOGI(TAG, "WiFi manager initialized");
    ESP_LOGI(TAG, "Connecting to SSID: %s", config->ssid);

    return ESP_OK;
}

esp_err_t wifi_manager_deinit(void)
{
    ESP_LOGI(TAG, "Deinitializing WiFi manager...");

    if (s_retry_timer) {
        xTimerStop(s_retry_timer, 0);
        xTimerDelete(s_retry_timer, 0);
        s_retry_timer = NULL;
    }

    esp_err_t ret = esp_wifi_stop();
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to stop WiFi: %s", esp_err_to_name(ret));
        return ret;
    }

    ret = esp_wifi_deinit();
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to deinit WiFi: %s", esp_err_to_name(ret));
        return ret;
    }

    if (s_wifi_event_group) {
        vEventGroupDelete(s_wifi_event_group);
        s_wifi_event_group = NULL;
    }

    s_wifi_mgr.status = WIFI_STATUS_DISCONNECTED;
    ESP_LOGI(TAG, "WiFi manager deinitialized");

    return ESP_OK;
}

wifi_status_t wifi_manager_get_status(void)
{
    return s_wifi_mgr.status;
}

bool wifi_manager_is_connected(void)
{
    return (s_wifi_mgr.status == WIFI_STATUS_CONNECTED);
}

esp_err_t wifi_manager_get_ip_string(char *ip_str, size_t max_len)
{
    if (!ip_str || max_len < 16) {
        return ESP_ERR_INVALID_ARG;
    }

    if (!wifi_manager_is_connected()) {
        return ESP_ERR_INVALID_STATE;
    }

    strncpy(ip_str, s_wifi_mgr.ip_address, max_len - 1);
    ip_str[max_len - 1] = '\0';

    return ESP_OK;
}

esp_err_t wifi_manager_wait_for_connection(uint32_t timeout_ms)
{
    if (!s_wifi_event_group) {
        ESP_LOGE(TAG, "WiFi manager not initialized");
        return ESP_ERR_INVALID_STATE;
    }

    TickType_t timeout_ticks = (timeout_ms == 0) ? portMAX_DELAY : pdMS_TO_TICKS(timeout_ms);

    /* Wait for connection or failure */
    EventBits_t bits = xEventGroupWaitBits(s_wifi_event_group,
                                            WIFI_CONNECTED_BIT | WIFI_FAIL_BIT,
                                            pdFALSE,
                                            pdFALSE,
                                            timeout_ticks);

    if (bits & WIFI_CONNECTED_BIT) {
        ESP_LOGI(TAG, "Connected to AP successfully");
        return ESP_OK;
    } else if (bits & WIFI_FAIL_BIT) {
        ESP_LOGE(TAG, "Failed to connect to AP");
        return ESP_FAIL;
    } else {
        ESP_LOGW(TAG, "Connection timeout");
        return ESP_ERR_TIMEOUT;
    }
}

esp_err_t wifi_manager_scan_networks(uint16_t max_aps)
{
    ESP_LOGI(TAG, "Scanning for WiFi networks...");

    wifi_scan_config_t scan_config = {
        .ssid = NULL,
        .bssid = NULL,
        .channel = 0,
        .show_hidden = true
    };

    esp_err_t ret = esp_wifi_scan_start(&scan_config, true);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "WiFi scan failed: %s", esp_err_to_name(ret));
        return ret;
    }

    uint16_t ap_count = 0;
    esp_wifi_scan_get_ap_num(&ap_count);

    if (ap_count == 0) {
        ESP_LOGW(TAG, "No WiFi networks found!");
        ESP_LOGW(TAG, "Possible causes:");
        ESP_LOGW(TAG, "  1. No antenna connected");
        ESP_LOGW(TAG, "  2. Antenna switch set to wrong position");
        ESP_LOGW(TAG, "  3. WiFi hardware issue");
        return ESP_FAIL;
    }

    ESP_LOGI(TAG, "Found %d WiFi networks:", ap_count);

    if (max_aps > 0 && ap_count > max_aps) {
        ap_count = max_aps;
    }

    wifi_ap_record_t *ap_list = malloc(sizeof(wifi_ap_record_t) * ap_count);
    if (!ap_list) {
        ESP_LOGE(TAG, "Failed to allocate memory for AP list");
        return ESP_ERR_NO_MEM;
    }

    esp_wifi_scan_get_ap_records(&ap_count, ap_list);

    ESP_LOGI(TAG, "------------------------------------------------------------");
    ESP_LOGI(TAG, "%-32s  Channel  RSSI  Auth", "SSID");
    ESP_LOGI(TAG, "------------------------------------------------------------");

    for (int i = 0; i < ap_count; i++) {
        const char *auth_mode;
        switch (ap_list[i].authmode) {
            case WIFI_AUTH_OPEN: auth_mode = "OPEN"; break;
            case WIFI_AUTH_WEP: auth_mode = "WEP"; break;
            case WIFI_AUTH_WPA_PSK: auth_mode = "WPA"; break;
            case WIFI_AUTH_WPA2_PSK: auth_mode = "WPA2"; break;
            case WIFI_AUTH_WPA_WPA2_PSK: auth_mode = "WPA/WPA2"; break;
            case WIFI_AUTH_WPA3_PSK: auth_mode = "WPA3"; break;
            default: auth_mode = "Unknown"; break;
        }

        ESP_LOGI(TAG, "%-32s  %7d  %4d  %s",
                 (char *)ap_list[i].ssid,
                 ap_list[i].primary,
                 ap_list[i].rssi,
                 auth_mode);
    }

    ESP_LOGI(TAG, "------------------------------------------------------------");

    free(ap_list);
    return ESP_OK;
}

esp_err_t wifi_manager_scan_results(wifi_scan_result_t *results,
                                    uint16_t max_count, uint16_t *out_count)
{
    if (!results || max_count == 0 || !out_count) {
        return ESP_ERR_INVALID_ARG;
    }
    *out_count = 0;

    wifi_scan_config_t scan_config = {
        .ssid = NULL,
        .bssid = NULL,
        .channel = 0,
        .show_hidden = false
    };

    esp_err_t ret = esp_wifi_scan_start(&scan_config, true);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "스캔 실패: %s", esp_err_to_name(ret));
        return ret;
    }

    uint16_t ap_count = 0;
    esp_wifi_scan_get_ap_num(&ap_count);
    if (ap_count == 0) {
        return ESP_OK;  // 결과 0개 (정상)
    }

    wifi_ap_record_t *ap_list = malloc(sizeof(wifi_ap_record_t) * ap_count);
    if (!ap_list) {
        esp_wifi_clear_ap_list();
        return ESP_ERR_NO_MEM;
    }

    esp_wifi_scan_get_ap_records(&ap_count, ap_list);

    uint16_t n = (ap_count < max_count) ? ap_count : max_count;
    for (uint16_t i = 0; i < n; i++) {
        strncpy(results[i].ssid, (char *)ap_list[i].ssid, sizeof(results[i].ssid) - 1);
        results[i].ssid[sizeof(results[i].ssid) - 1] = '\0';
        results[i].rssi = ap_list[i].rssi;
        results[i].channel = ap_list[i].primary;
        results[i].authmode = (uint8_t)ap_list[i].authmode;
    }
    *out_count = n;

    free(ap_list);
    return ESP_OK;
}

esp_err_t wifi_manager_connect(const char *ssid, const char *password)
{
    if (!ssid || strlen(ssid) == 0) {
        return ESP_ERR_INVALID_ARG;
    }
    if (!s_wifi_event_group) {
        ESP_LOGE(TAG, "WiFi 매니저가 초기화되지 않음");
        return ESP_ERR_INVALID_STATE;
    }

    ESP_LOGI(TAG, "새 자격증명으로 재연결 시도 (SSID: %s)", ssid);

    /* 사용자가 실제 WiFi를 설정하므로 이제부터 자동 연결/재시도 활성화 */
    s_wifi_mgr.auto_connect = true;

    /* 진행 중인 재시도 타이머 중지 및 상태 초기화 */
    xTimerStop(s_retry_timer, 0);
    s_wifi_mgr.retry_count = 0;
    s_wifi_mgr.ip_address[0] = '\0';
    xEventGroupClearBits(s_wifi_event_group, WIFI_CONNECTED_BIT | WIFI_FAIL_BIT);

    /* 기존 연결 해제 (연결되어 있을 수 있음) */
    esp_wifi_disconnect();

    /* 새 설정 적용 */
    wifi_config_t wifi_config = {0};
    strncpy((char *)wifi_config.sta.ssid, ssid, sizeof(wifi_config.sta.ssid) - 1);
    if (password) {
        strncpy((char *)wifi_config.sta.password, password, sizeof(wifi_config.sta.password) - 1);
    }
    wifi_config.sta.threshold.authmode = WIFI_AUTH_OPEN;  // 오픈/암호화 모두 허용
    wifi_config.sta.sae_pwe_h2e = WPA3_SAE_PWE_BOTH;
    wifi_config.sta.pmf_cfg.capable = true;
    wifi_config.sta.pmf_cfg.required = false;

    esp_err_t ret = esp_wifi_set_config(WIFI_IF_STA, &wifi_config);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "WiFi 설정 적용 실패: %s", esp_err_to_name(ret));
        return ret;
    }

    s_wifi_mgr.status = WIFI_STATUS_CONNECTING;
    ret = esp_wifi_connect();
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "esp_wifi_connect 실패: %s", esp_err_to_name(ret));
        return ret;
    }

    return ESP_OK;
}

esp_err_t wifi_manager_get_rssi(int8_t *out_rssi)
{
    if (!out_rssi) {
        return ESP_ERR_INVALID_ARG;
    }
    if (!wifi_manager_is_connected()) {
        return ESP_ERR_INVALID_STATE;
    }
    wifi_ap_record_t ap_info;
    esp_err_t ret = esp_wifi_sta_get_ap_info(&ap_info);
    if (ret == ESP_OK) {
        *out_rssi = ap_info.rssi;
    }
    return ret;
}
