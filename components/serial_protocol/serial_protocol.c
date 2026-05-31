/**
 * @file serial_protocol.c
 * @brief USB 시리얼 JSON 명령 프로토콜 처리기 구현
 *
 * ESP32-S3 내장 USB Serial/JTAG 포트를 통해 PC 설정 툴과 통신합니다.
 * 드라이버를 직접 사용하여 로그 콘솔과 독립적인 양방향 채널을 확보합니다.
 */

#include "serial_protocol.h"
#include "config_manager.h"
#include "wifi_manager.h"

#include <string.h>
#include <stdio.h>
#include <fcntl.h>
#include <unistd.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/usb_serial_jtag.h"
#include "driver/usb_serial_jtag_vfs.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_app_desc.h"
#include "cJSON.h"

static const char *TAG = "SERIAL_PROTO";

/* 제품 식별 시그니처 — PC 툴이 우리 제품인지 확인 */
#define PRODUCT_NAME      "IIS3DWB-VIB-SENSOR"
#define PROTOCOL_VERSION  1

/* 라인 버퍼 / 읽기 버퍼 크기 */
#define LINE_BUF_SIZE     512
#define READ_CHUNK_SIZE   128

/* set_wifi 연결 검증 타임아웃 (ms) — 이 시간 내 연결 못하면 실패로 간주 */
#define WIFI_VERIFY_TIMEOUT_MS  15000

/* 스캔 결과 최대 개수 */
#define MAX_SCAN_RESULTS  20

/**
 * @brief JSON 문자열을 응답 마커와 함께 stdout으로 전송 후 해제
 *
 * 콘솔(USB Serial/JTAG)과 동일한 stdout 경로로 출력합니다.
 * PC 툴은 "#RESP#" 접두사로 응답과 로그를 구분합니다.
 */
static void send_response(cJSON *resp)
{
    char *json = cJSON_PrintUnformatted(resp);
    if (json) {
        /* stdout(=USB JTAG 콘솔)으로 전송. PC 툴은 #RESP# 접두사로 식별 */
        printf("%s%s\n", SERIAL_RESP_PREFIX, json);
        fflush(stdout);
        free(json);
    }
    cJSON_Delete(resp);
}

/**
 * @brief 간단한 에러 응답 전송
 */
static void send_error(const char *reason)
{
    cJSON *resp = cJSON_CreateObject();
    cJSON_AddStringToObject(resp, "result", "error");
    cJSON_AddStringToObject(resp, "reason", reason);
    send_response(resp);
}

/**
 * @brief MAC 주소 문자열 생성 (XX:XX:XX:XX:XX:XX)
 */
static void get_mac_string(char *buf, size_t len)
{
    uint8_t mac[6];
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
    snprintf(buf, len, "%02X:%02X:%02X:%02X:%02X:%02X",
             mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
}

/* ===================== 명령 핸들러 ===================== */

static void handle_ping(void)
{
    cJSON *resp = cJSON_CreateObject();
    cJSON_AddStringToObject(resp, "result", "ok");
    cJSON_AddStringToObject(resp, "product", PRODUCT_NAME);
    cJSON_AddNumberToObject(resp, "proto", PROTOCOL_VERSION);
    send_response(resp);
}

static void handle_get_info(void)
{
    char mac[18];
    get_mac_string(mac, sizeof(mac));

    const esp_app_desc_t *app_desc = esp_app_get_description();

    cJSON *resp = cJSON_CreateObject();
    cJSON_AddStringToObject(resp, "result", "ok");
    cJSON_AddStringToObject(resp, "product", PRODUCT_NAME);
    cJSON_AddStringToObject(resp, "fw_version", app_desc ? app_desc->version : "unknown");
    cJSON_AddStringToObject(resp, "mac", mac);
    cJSON_AddBoolToObject(resp, "wifi_configured", config_manager_has_wifi());
    send_response(resp);
}

static void handle_scan_wifi(void)
{
    static wifi_scan_result_t results[MAX_SCAN_RESULTS];
    uint16_t count = 0;

    esp_err_t ret = wifi_manager_scan_results(results, MAX_SCAN_RESULTS, &count);
    if (ret != ESP_OK) {
        send_error("scan_failed");
        return;
    }

    cJSON *resp = cJSON_CreateObject();
    cJSON_AddStringToObject(resp, "result", "ok");
    cJSON *arr = cJSON_AddArrayToObject(resp, "networks");
    for (uint16_t i = 0; i < count; i++) {
        if (strlen(results[i].ssid) == 0) {
            continue;  // 숨겨진 SSID 제외
        }
        cJSON *net = cJSON_CreateObject();
        cJSON_AddStringToObject(net, "ssid", results[i].ssid);
        cJSON_AddNumberToObject(net, "rssi", results[i].rssi);
        cJSON_AddNumberToObject(net, "channel", results[i].channel);
        cJSON_AddNumberToObject(net, "auth", results[i].authmode);
        cJSON_AddItemToArray(arr, net);
    }
    send_response(resp);
}

static void handle_set_wifi(cJSON *root)
{
    cJSON *ssid_item = cJSON_GetObjectItem(root, "ssid");
    cJSON *pw_item   = cJSON_GetObjectItem(root, "pw");

    if (!cJSON_IsString(ssid_item) || strlen(ssid_item->valuestring) == 0) {
        send_error("invalid_ssid");
        return;
    }
    const char *ssid = ssid_item->valuestring;
    const char *pw = cJSON_IsString(pw_item) ? pw_item->valuestring : "";

    ESP_LOGI(TAG, "set_wifi 요청: SSID=%s", ssid);

    /* 1) 실제 연결을 먼저 시도하여 자격증명 검증 */
    esp_err_t ret = wifi_manager_connect(ssid, pw);
    if (ret != ESP_OK) {
        send_error("connect_start_failed");
        return;
    }

    ret = wifi_manager_wait_for_connection(WIFI_VERIFY_TIMEOUT_MS);
    if (ret != ESP_OK) {
        /* 연결 실패 → 저장하지 않음 (잘못된 비번이 ROM에 남지 않도록) */
        ESP_LOGW(TAG, "연결 검증 실패 — 저장 안 함");
        send_error("connect_failed");
        return;
    }

    /* 2) 연결 성공 → NVS(ROM)에 저장 */
    ret = config_manager_save_wifi(ssid, pw);
    if (ret != ESP_OK) {
        send_error("save_failed");
        return;
    }

    /* 3) 연결 정보 응답 */
    char ip[16] = "";
    wifi_manager_get_ip_string(ip, sizeof(ip));
    int8_t rssi = 0;
    wifi_manager_get_rssi(&rssi);

    cJSON *resp = cJSON_CreateObject();
    cJSON_AddStringToObject(resp, "result", "ok");
    cJSON_AddStringToObject(resp, "ssid", ssid);
    cJSON_AddStringToObject(resp, "ip", ip);
    cJSON_AddNumberToObject(resp, "rssi", rssi);
    cJSON_AddBoolToObject(resp, "saved", true);
    send_response(resp);
}

static void handle_get_status(void)
{
    cJSON *resp = cJSON_CreateObject();
    cJSON_AddStringToObject(resp, "result", "ok");

    bool connected = wifi_manager_is_connected();
    cJSON_AddBoolToObject(resp, "connected", connected);

    if (connected) {
        char ip[16] = "";
        wifi_manager_get_ip_string(ip, sizeof(ip));
        int8_t rssi = 0;
        wifi_manager_get_rssi(&rssi);
        cJSON_AddStringToObject(resp, "ip", ip);
        cJSON_AddNumberToObject(resp, "rssi", rssi);
    }

    /* 저장된 SSID 정보 */
    config_wifi_cred_t cred;
    if (config_manager_load_wifi(&cred) == ESP_OK) {
        cJSON_AddStringToObject(resp, "saved_ssid", cred.ssid);
    }
    send_response(resp);
}

static void handle_factory_reset(void)
{
    esp_err_t ret = config_manager_factory_reset();
    if (ret != ESP_OK) {
        send_error("reset_failed");
        return;
    }
    cJSON *resp = cJSON_CreateObject();
    cJSON_AddStringToObject(resp, "result", "ok");
    cJSON_AddStringToObject(resp, "message", "factory_reset_done");
    send_response(resp);
}

/**
 * @brief 한 줄(JSON 명령)을 파싱하여 적절한 핸들러로 분기
 */
static void process_line(const char *line)
{
    cJSON *root = cJSON_Parse(line);
    if (!root) {
        send_error("invalid_json");
        return;
    }

    cJSON *cmd_item = cJSON_GetObjectItem(root, "cmd");
    if (!cJSON_IsString(cmd_item)) {
        send_error("missing_cmd");
        cJSON_Delete(root);
        return;
    }

    const char *cmd = cmd_item->valuestring;
    ESP_LOGI(TAG, "명령 수신: %s", cmd);

    if (strcmp(cmd, "ping") == 0) {
        handle_ping();
    } else if (strcmp(cmd, "get_info") == 0) {
        handle_get_info();
    } else if (strcmp(cmd, "scan_wifi") == 0) {
        handle_scan_wifi();
    } else if (strcmp(cmd, "set_wifi") == 0) {
        handle_set_wifi(root);
    } else if (strcmp(cmd, "get_status") == 0) {
        handle_get_status();
    } else if (strcmp(cmd, "factory_reset") == 0) {
        handle_factory_reset();
    } else {
        send_error("unknown_cmd");
    }

    cJSON_Delete(root);
}

/**
 * @brief 시리얼 수신 태스크 — USB JTAG 드라이버에서 직접 바이트 수신
 *
 * 콘솔은 UART(primary)를 쓰고, 이 태스크는 USB Serial/JTAG 드라이버를
 * 직접 사용해 PC 툴과 통신합니다. 로그(UART)와 통신(USB)이 분리됩니다.
 */
static void serial_protocol_task(void *arg)
{
    static char line_buf[LINE_BUF_SIZE];
    size_t line_len = 0;
    uint8_t chunk[READ_CHUNK_SIZE];

    ESP_LOGI(TAG, "시리얼 프로토콜 태스크 시작 (USB JTAG read_bytes)");

    while (1) {
        int n = usb_serial_jtag_read_bytes(chunk, sizeof(chunk), pdMS_TO_TICKS(100));
        if (n <= 0) {
            continue;
        }
        for (int i = 0; i < n; i++) {
            char c = (char)chunk[i];
            if (c == '\n' || c == '\r') {
                if (line_len > 0) {
                    line_buf[line_len] = '\0';
                    process_line(line_buf);
                    line_len = 0;
                }
            } else if (line_len < LINE_BUF_SIZE - 1) {
                line_buf[line_len++] = c;
            } else {
                line_len = 0;
                send_error("line_too_long");
            }
        }
    }
}

void serial_protocol_start(void)
{
    /* 콘솔이 USB JTAG를 secondary로 사용 중. 우리 드라이버를 설치하여
     * RX(입력)를 직접 수신한다. (콘솔이 이미 설치했으면 INVALID_STATE → 무시) */
    usb_serial_jtag_driver_config_t cfg = {
        .tx_buffer_size = 1024,
        .rx_buffer_size = 1024,
    };
    esp_err_t ret = usb_serial_jtag_driver_install(&cfg);
    usb_serial_jtag_vfs_use_driver();

    ESP_LOGI(TAG, "시리얼 프로토콜 초기화 (드라이버 설치: %s)", esp_err_to_name(ret));

    xTaskCreate(serial_protocol_task, "serial_proto", 6144, NULL, 6, NULL);
}
