/**
 * @file config_manager.c
 * @brief 디바이스 설정 영구 저장 관리자 구현 (NVS 기반)
 */

#include "config_manager.h"
#include <string.h>
#include "nvs_flash.h"
#include "nvs.h"
#include "esp_log.h"

static const char *TAG = "CONFIG_MGR";

/* NVS 네임스페이스 및 키 */
#define NVS_NAMESPACE   "devcfg"
#define NVS_KEY_SSID    "wifi_ssid"
#define NVS_KEY_PASS    "wifi_pass"
/* 스트리밍 설정 키 */
#define NVS_KEY_SRV_IP   "srv_ip"
#define NVS_KEY_SRV_PORT "srv_port"
#define NVS_KEY_RATE     "stream_rate"
#define NVS_KEY_TRANSP   "transport"

esp_err_t config_manager_init(void)
{
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        // NVS 파티션이 손상되었거나 버전이 바뀐 경우 → 지우고 재초기화
        ESP_LOGW(TAG, "NVS 파티션 재초기화 필요 (%s)", esp_err_to_name(ret));
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "NVS 초기화 실패: %s", esp_err_to_name(ret));
        return ret;
    }
    ESP_LOGI(TAG, "설정 관리자 초기화 완료");
    return ESP_OK;
}

bool config_manager_has_wifi(void)
{
    nvs_handle_t h;
    if (nvs_open(NVS_NAMESPACE, NVS_READONLY, &h) != ESP_OK) {
        return false;  // 네임스페이스 자체가 없으면 저장된 설정 없음
    }

    size_t len = 0;
    esp_err_t ret = nvs_get_str(h, NVS_KEY_SSID, NULL, &len);
    nvs_close(h);

    // SSID 키가 존재하고 길이가 1 이상(빈 문자열 제외)이면 설정 있음
    return (ret == ESP_OK && len > 1);
}

esp_err_t config_manager_save_wifi(const char *ssid, const char *password)
{
    if (ssid == NULL || strlen(ssid) == 0 || strlen(ssid) > CONFIG_MGR_SSID_MAX_LEN) {
        ESP_LOGE(TAG, "잘못된 SSID");
        return ESP_ERR_INVALID_ARG;
    }
    if (password == NULL) {
        password = "";  // 오픈 네트워크 허용
    }
    if (strlen(password) > CONFIG_MGR_PASSWORD_MAX_LEN) {
        ESP_LOGE(TAG, "비밀번호가 너무 깁니다");
        return ESP_ERR_INVALID_ARG;
    }

    nvs_handle_t h;
    esp_err_t ret = nvs_open(NVS_NAMESPACE, NVS_READWRITE, &h);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "NVS 열기 실패: %s", esp_err_to_name(ret));
        return ret;
    }

    ret = nvs_set_str(h, NVS_KEY_SSID, ssid);
    if (ret == ESP_OK) {
        ret = nvs_set_str(h, NVS_KEY_PASS, password);
    }
    if (ret == ESP_OK) {
        ret = nvs_commit(h);  // 실제 Flash에 기록 (커밋 필수)
    }
    nvs_close(h);

    if (ret == ESP_OK) {
        ESP_LOGI(TAG, "WiFi 설정 저장 완료 (SSID: %s)", ssid);
    } else {
        ESP_LOGE(TAG, "WiFi 설정 저장 실패: %s", esp_err_to_name(ret));
    }
    return ret;
}

esp_err_t config_manager_load_wifi(config_wifi_cred_t *cred)
{
    if (cred == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    nvs_handle_t h;
    esp_err_t ret = nvs_open(NVS_NAMESPACE, NVS_READONLY, &h);
    if (ret != ESP_OK) {
        return ESP_ERR_NVS_NOT_FOUND;  // 저장된 설정 없음
    }

    size_t ssid_len = sizeof(cred->ssid);
    ret = nvs_get_str(h, NVS_KEY_SSID, cred->ssid, &ssid_len);
    if (ret != ESP_OK) {
        nvs_close(h);
        return ret;
    }

    size_t pass_len = sizeof(cred->password);
    ret = nvs_get_str(h, NVS_KEY_PASS, cred->password, &pass_len);
    if (ret == ESP_ERR_NVS_NOT_FOUND) {
        // 비밀번호 키가 없으면 오픈 네트워크로 간주
        cred->password[0] = '\0';
        ret = ESP_OK;
    }
    nvs_close(h);

    if (ret == ESP_OK) {
        ESP_LOGI(TAG, "WiFi 설정 로드 완료 (SSID: %s)", cred->ssid);
    }
    return ret;
}

/* ===================== 스트리밍 설정 ===================== */

bool config_manager_has_stream(void)
{
    nvs_handle_t h;
    if (nvs_open(NVS_NAMESPACE, NVS_READONLY, &h) != ESP_OK) {
        return false;
    }
    size_t len = 0;
    esp_err_t ret = nvs_get_str(h, NVS_KEY_SRV_IP, NULL, &len);
    nvs_close(h);
    return (ret == ESP_OK && len > 1);  // IP 문자열이 있어야 설정됨
}

esp_err_t config_manager_save_stream(const config_stream_t *cfg)
{
    if (cfg == NULL || strlen(cfg->server_ip) == 0 ||
        strlen(cfg->server_ip) > CONFIG_MGR_IP_MAX_LEN) {
        ESP_LOGE(TAG, "잘못된 스트리밍 설정 (server_ip)");
        return ESP_ERR_INVALID_ARG;
    }
    if (cfg->rate_step > 4) {
        ESP_LOGE(TAG, "rate_step 범위 초과 (0~4)");
        return ESP_ERR_INVALID_ARG;
    }

    nvs_handle_t h;
    esp_err_t ret = nvs_open(NVS_NAMESPACE, NVS_READWRITE, &h);
    if (ret != ESP_OK) {
        return ret;
    }

    ret = nvs_set_str(h, NVS_KEY_SRV_IP, cfg->server_ip);
    if (ret == ESP_OK) ret = nvs_set_u16(h, NVS_KEY_SRV_PORT, cfg->server_port);
    if (ret == ESP_OK) ret = nvs_set_u8(h, NVS_KEY_RATE, cfg->rate_step);
    if (ret == ESP_OK) ret = nvs_set_u8(h, NVS_KEY_TRANSP, cfg->transport);
    if (ret == ESP_OK) ret = nvs_commit(h);
    nvs_close(h);

    if (ret == ESP_OK) {
        ESP_LOGI(TAG, "스트리밍 설정 저장 (서버 %s:%u, rate=%u, %s)",
                 cfg->server_ip, cfg->server_port, cfg->rate_step,
                 cfg->transport ? "TCP" : "UDP");
    } else {
        ESP_LOGE(TAG, "스트리밍 설정 저장 실패: %s", esp_err_to_name(ret));
    }
    return ret;
}

esp_err_t config_manager_load_stream(config_stream_t *cfg)
{
    if (cfg == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    nvs_handle_t h;
    esp_err_t ret = nvs_open(NVS_NAMESPACE, NVS_READONLY, &h);
    if (ret != ESP_OK) {
        return ESP_ERR_NVS_NOT_FOUND;
    }

    size_t ip_len = sizeof(cfg->server_ip);
    ret = nvs_get_str(h, NVS_KEY_SRV_IP, cfg->server_ip, &ip_len);
    if (ret != ESP_OK) {
        nvs_close(h);
        return ret;
    }
    /* 나머지 값은 없으면 기본값 적용 */
    if (nvs_get_u16(h, NVS_KEY_SRV_PORT, &cfg->server_port) != ESP_OK) {
        cfg->server_port = 9000;
    }
    if (nvs_get_u8(h, NVS_KEY_RATE, &cfg->rate_step) != ESP_OK) {
        cfg->rate_step = 0;  // 기본 1kHz
    }
    if (nvs_get_u8(h, NVS_KEY_TRANSP, &cfg->transport) != ESP_OK) {
        cfg->transport = 0;  // 기본 UDP
    }
    nvs_close(h);

    ESP_LOGI(TAG, "스트리밍 설정 로드 (서버 %s:%u, rate=%u)",
             cfg->server_ip, cfg->server_port, cfg->rate_step);
    return ESP_OK;
}

esp_err_t config_manager_factory_reset(void)
{
    nvs_handle_t h;
    esp_err_t ret = nvs_open(NVS_NAMESPACE, NVS_READWRITE, &h);
    if (ret != ESP_OK) {
        // 네임스페이스가 없으면 이미 초기 상태
        return ESP_OK;
    }

    ret = nvs_erase_all(h);  // devcfg 네임스페이스 전체 삭제
    if (ret == ESP_OK) {
        ret = nvs_commit(h);
    }
    nvs_close(h);

    if (ret == ESP_OK) {
        ESP_LOGW(TAG, "공장 초기화 완료 — 모든 설정 삭제됨");
    } else {
        ESP_LOGE(TAG, "공장 초기화 실패: %s", esp_err_to_name(ret));
    }
    return ret;
}
