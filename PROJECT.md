# TSL2591 프로젝트 변경 내역

## 프로젝트 개요

ESP32-S3와 TSL2591 저조도 센서를 I²C로 연결하고, WiFi를 통해 라즈베리파이 서버로 센서 데이터를 전송하는 IoT 프로젝트입니다.

**개발 환경:** 라즈베리파이 5 (Raspberry Pi OS, Linux ARM64)
**타겟 디바이스:** ESP32-S3 (QFN56, 8MB PSRAM)
**빌드 시스템:** ESP-IDF v5.4.3 + VSCode ESP-IDF Extension

---

## 2025-11-07: 라즈베리파이 5 개발 환경 구축

### 🎯 목표
Windows 기반 개발 환경을 라즈베리파이 5 리눅스 환경으로 마이그레이션하여 24시간 개발/테스트 환경을 구축합니다.

### ✅ 구현 완료 내역

#### 1. ESP-IDF 설치 및 환경 구성

**설치 방법:**
- VSCode ESP-IDF Extension 사용
- ESP-IDF v5.4.3 설치
- 설치 경로: `/home/drjins/esp/v5.4.3/esp-idf`
- 툴체인 경로: `/home/drjins/.espressif`

**설치된 컴포넌트:**
- Python 3.11.2
- CMake 3.25.1
- Ninja build system
- xtensa-esp32s3-elf-gcc 14.2.0

#### 2. VSCode 설정 업데이트

**수정된 파일: `.vscode/settings.json`**

**변경 전 (Windows 설정):**
```json
{
  "idf.espIdfPathWin": "C:\\Users\\drjin\\esp\\v5.4.3\\esp-idf",
  "idf.portWin": "COM4",
  "clangd.path": "C:\\Users\\drjin\\.espressif\\tools\\..."
}
```

**변경 후 (Linux 설정):**
```json
{
  "idf.espIdfPath": "/home/drjins/esp/v5.4.3/esp-idf",
  "idf.pythonBinPath": "/usr/bin/python3",
  "idf.toolsPath": "/home/drjins/.espressif",
  "idf.port": "/dev/ttyACM0",
  "idf.baudRate": "460800",
  "idf.flashBaudRate": "460800"
}
```

#### 3. 빌드 시스템 재구성

**문제:**
- Windows에서 생성된 빌드 캐시가 Linux와 호환되지 않음
- CMake generator 불일치 (Unix Makefiles vs Ninja)

**해결:**
```bash
# 빌드 디렉토리 삭제
rm -rf build

# 타겟 재설정
idf.py set-target esp32s3

# 빌드
idf.py build
```

**빌드 성공:**
- 프로젝트: tsl2591_sensor
- 펌웨어 크기: 765,008 bytes (825KB)
- 남은 공간: 222,832 bytes (27% free)
- 빌드 시간: ~5분 (첫 빌드), ~30초 (재빌드)

#### 4. ESP32-S3 플래시 및 테스트

**연결 정보:**
- 디바이스: `/dev/ttyACM0`
- 칩: ESP32-S3 (QFN56) revision v0.2
- 기능: WiFi, BLE, Embedded PSRAM 8MB
- MAC 주소: 98:a3:16:de:b1:78

**플래시 성공:**
```bash
idf.py -p /dev/ttyACM0 flash
```

**실행 결과:**
```
I (2321) WIFI_MGR: Got IP address: 192.168.0.48
I (2331) WIFI_APP: WiFi connected successfully!
I (2381) TSL2591_APP: TSL2591 initialized successfully!
I (2501) TSL2591_APP: Illuminance: 134.60 lux
I (2941) RGB_LED: Color: RED      (R:1 G:0 B:0)
```

모든 기능이 정상 작동합니다!

#### 5. 문서 업데이트

**수정된 파일:**
- `README.md` - 라즈베리파이 설치 방법 추가
- `PROJECT.md` - 본 파일, 마이그레이션 내역 추가
- `CLAUDE.md` - 개발 환경 정보 업데이트 예정

---

## 2025-11-05: WiFi 기능 추가

### 🎯 목표
ESP32에서 WiFi Station 모드로 AP에 연결하여, 향후 라즈베리파이 서버로 TSL2591 센서 데이터를 전송할 수 있는 기반을 마련합니다.

### ✅ 구현 완료 내역

#### 1. WiFi Manager 컴포넌트 생성

새로운 재사용 가능한 WiFi 관리 컴포넌트를 추가했습니다.

**추가된 파일:**

- **`components/wifi_manager/wifi_manager.h`**
  - WiFi 관리 API 정의
  - WiFi 상태 열거형 (DISCONNECTED, CONNECTING, CONNECTED, FAILED)
  - WiFi 설정 구조체 정의
  - 공개 함수 선언

- **`components/wifi_manager/wifi_manager.c`**
  - WiFi Station 모드 초기화 및 연결 구현
  - WiFi 이벤트 핸들러 (연결, 연결 해제, IP 할당)
  - 자동 재연결 로직 (최대 5회 재시도)
  - FreeRTOS 이벤트 그룹 기반 동기화
  - NVS (Non-Volatile Storage) 자동 초기화
  - IP 주소 문자열 변환 기능

- **`components/wifi_manager/CMakeLists.txt`**
  - WiFi manager 컴포넌트 빌드 설정
  - 필요한 ESP-IDF 컴포넌트 의존성 선언
    - `esp_wifi`: WiFi 드라이버
    - `esp_netif`: 네트워크 인터페이스
    - `nvs_flash`: 비휘발성 스토리지
    - `esp_event`: 이벤트 루프

#### 2. WiFi 설정 메뉴 추가

**수정된 파일: `main/Kconfig.projbuild`**

새로운 "WiFi Configuration" 메뉴를 추가했습니다:

```kconfig
menu "WiFi Configuration"
    config WIFI_SSID
        string "WiFi SSID"
        default "myssid"

    config WIFI_PASSWORD
        string "WiFi Password"
        default "mypassword"

    config WIFI_MAXIMUM_RETRY
        int "Maximum WiFi Retry Count"
        default 5

    config WIFI_SCAN_AUTH_MODE_THRESHOLD
        int "WiFi Scan Auth Mode Threshold"
        default 3
endmenu
```

**설정 가능 항목:**
- WiFi SSID (네트워크 이름)
- WiFi Password (비밀번호)
- 최대 재시도 횟수 (기본값: 5회)
- 인증 모드 임계값 (0=OPEN, 3=WPA2_PSK)

#### 3. 기본 설정 업데이트

**수정된 파일: `sdkconfig.defaults`**

WiFi 기본 설정을 추가했습니다:

```ini
# WiFi Configuration
CONFIG_WIFI_SSID="myssid"
CONFIG_WIFI_PASSWORD="mypassword"
CONFIG_WIFI_MAXIMUM_RETRY=5
CONFIG_WIFI_SCAN_AUTH_MODE_THRESHOLD=3
```

**참고:** 실제 사용 시 `idf.py menuconfig`로 WiFi SSID와 비밀번호를 설정해야 합니다.

#### 4. 메인 애플리케이션 통합

**수정된 파일: `main/blink_example_main.c`**

WiFi 초기화 및 연결 로직을 메인 애플리케이션에 추가했습니다:

**변경 사항:**
- `wifi_manager.h` 헤더 파일 포함
- `TAG_WIFI` 로그 태그 추가
- `init_wifi()` 함수 구현
  - WiFi 초기화
  - WiFi 연결 대기 (30초 타임아웃)
  - IP 주소 획득 및 로그 출력
- `app_main()` 수정
  - WiFi 초기화를 TSL2591 센서 초기화 전에 실행
  - WiFi 연결 실패 시에도 센서 기능은 계속 동작

**애플리케이션 시작 순서:**
1. WiFi 초기화 및 연결
2. TSL2591 센서 초기화
3. RGB LED 초기화
4. 센서 읽기 태스크 시작
5. LED 색상 사이클 루프

#### 5. 빌드 의존성 업데이트

**수정된 파일: `main/CMakeLists.txt`**

```cmake
idf_component_register(SRCS "blink_example_main.c"
                       INCLUDE_DIRS "."
                       REQUIRES tsl2591 wifi_manager)
```

main 컴포넌트가 `wifi_manager` 컴포넌트를 의존성으로 추가합니다.

---

### 📋 WiFi Manager API

#### 초기화 및 해제
- **`wifi_manager_init(config)`**
  - WiFi 초기화 및 AP 연결 시작
  - NVS 초기화
  - WiFi 이벤트 핸들러 등록
  - 반환값: `ESP_OK` (성공), `ESP_ERR_INVALID_ARG` (잘못된 설정), `ESP_FAIL` (초기화 실패)

- **`wifi_manager_deinit()`**
  - WiFi 연결 해제 및 리소스 정리
  - 반환값: `ESP_OK` (성공), `ESP_FAIL` (실패)

#### 상태 확인
- **`wifi_manager_is_connected()`**
  - WiFi 연결 상태 확인
  - 반환값: `true` (연결됨), `false` (연결 안 됨)

- **`wifi_manager_get_status()`**
  - 현재 WiFi 상태 반환
  - 반환값: `WIFI_STATUS_DISCONNECTED`, `WIFI_STATUS_CONNECTING`, `WIFI_STATUS_CONNECTED`, `WIFI_STATUS_FAILED`

- **`wifi_manager_get_ip_string(buffer, size)`**
  - IP 주소를 문자열로 가져오기
  - 반환값: `ESP_OK` (성공), `ESP_ERR_INVALID_STATE` (WiFi 미연결)

#### 동기화
- **`wifi_manager_wait_for_connection(timeout_ms)`**
  - WiFi 연결 완료 대기 (타임아웃 지원)
  - 반환값: `ESP_OK` (연결 성공), `ESP_ERR_TIMEOUT` (타임아웃), `ESP_FAIL` (연결 실패)

---

### 🔧 사용 방법

#### 1. WiFi 설정

```bash
idf.py menuconfig
```

**"WiFi Configuration"** 메뉴에서:
- WiFi SSID 입력
- WiFi Password 입력
- (선택사항) 최대 재시도 횟수 조정

#### 2. 빌드 및 플래시

**라즈베리파이에서:**
```bash
# ESP-IDF 환경 활성화
source ~/esp/v5.4.3/esp-idf/export.sh

# 빌드
idf.py build

# 플래시 및 모니터
idf.py -p /dev/ttyACM0 flash monitor
```

**VSCode에서 (권장):**
- F1 → `ESP-IDF: Build, Flash and start a monitor on your device`

#### 3. 예상 출력

```
I (XXX) WIFI_APP: Initializing WiFi...
I (XXX) WIFI_APP: SSID: MyHomeWiFi
I (XXX) WIFI_MGR: Initializing WiFi manager...
I (XXX) WIFI_MGR: WiFi manager initialized
I (XXX) WIFI_MGR: Connecting to SSID: MyHomeWiFi
I (XXX) WIFI_APP: Waiting for WiFi connection...
I (XXX) WIFI_MGR: WiFi started, connecting to AP...
I (XXX) WIFI_MGR: Connected to AP
I (XXX) WIFI_MGR: Got IP address: 192.168.1.100
I (XXX) WIFI_APP: WiFi connected successfully!
I (XXX) WIFI_APP: IP Address: 192.168.1.100
I (XXX) TSL2591_APP: Configuration:
I (XXX) TSL2591_APP:   I2C Port: 1
...
```

---

### 🏗️ 프로젝트 구조 (업데이트)

```
TSL2591/
├── components/
│   ├── tsl2591/                # TSL2591 드라이버 컴포넌트
│   │   ├── tsl2591.h
│   │   ├── tsl2591.c
│   │   └── CMakeLists.txt
│   └── wifi_manager/           # WiFi 관리 컴포넌트 (NEW)
│       ├── wifi_manager.h      # WiFi API 정의
│       ├── wifi_manager.c      # WiFi 구현
│       └── CMakeLists.txt      # 빌드 설정
├── main/
│   ├── blink_example_main.c    # 메인 애플리케이션 (수정됨)
│   ├── Kconfig.projbuild       # 프로젝트 설정 메뉴 (WiFi 설정 추가)
│   └── CMakeLists.txt          # 의존성 업데이트
├── CMakeLists.txt
├── sdkconfig.defaults          # WiFi 기본 설정 추가
├── CLAUDE.md
├── README.md
└── PROJECT.md                  # 본 문서 (NEW)
```

---

### 🔍 기술 세부 사항

#### WiFi 연결 흐름

1. **초기화 단계**
   - NVS 플래시 초기화 (WiFi 설정 저장용)
   - TCP/IP 스택 초기화 (`esp_netif_init`)
   - 이벤트 루프 생성
   - WiFi 드라이버 초기화

2. **연결 단계**
   - WiFi Station 인터페이스 생성
   - WiFi 이벤트 핸들러 등록
   - WiFi 모드를 STA로 설정
   - SSID/비밀번호 설정
   - WiFi 시작

3. **이벤트 처리**
   - `WIFI_EVENT_STA_START`: WiFi 시작 → AP 연결 시도
   - `WIFI_EVENT_STA_CONNECTED`: AP 연결 성공
   - `IP_EVENT_STA_GOT_IP`: IP 주소 할당 → 연결 완료
   - `WIFI_EVENT_STA_DISCONNECTED`: 연결 끊김 → 재연결 시도

4. **재연결 로직**
   - 최대 5회까지 자동 재시도
   - 재시도 실패 시 `WIFI_STATUS_FAILED` 상태로 전환

#### 동기화 메커니즘

FreeRTOS 이벤트 그룹을 사용하여 WiFi 연결 상태를 동기화합니다:
- `WIFI_CONNECTED_BIT`: WiFi 연결 성공 플래그
- `WIFI_FAIL_BIT`: WiFi 연결 실패 플래그

`wifi_manager_wait_for_connection()` 함수는 이벤트 비트를 대기하여 블로킹 방식으로 연결 완료를 확인합니다.

---

### 📝 다음 단계 (예정)

#### 2단계: HTTP 클라이언트 구현
- [ ] `http_sender` 컴포넌트 생성
- [ ] 라즈베리파이 서버 IP/포트 설정 추가
- [ ] HTTP POST 요청 구현
- [ ] JSON 직렬화 (cJSON 라이브러리 사용)

#### 3단계: 데이터 전송 로직 통합
- [ ] 센서 데이터 큐 추가
- [ ] `tsl2591_task` 수정 (센서 읽기 → 큐 전송)
- [ ] HTTP 전송 태스크 생성 (큐 → HTTP POST)
- [ ] 타임스탬프 추가 (SNTP 시간 동기화)

#### 4단계: 에러 처리 및 최적화
- [ ] WiFi 연결 끊김 시 재연결 처리
- [ ] HTTP 요청 타임아웃 및 재시도
- [ ] 전송 실패 시 로컬 저장 (선택사항)
- [ ] 전력 최적화 (WiFi Light Sleep)

#### 5단계: 라즈베리파이 서버 구현
- [ ] Flask/FastAPI HTTP 서버
- [ ] `/sensor_data` 엔드포인트
- [ ] 데이터베이스 저장 (SQLite/PostgreSQL)
- [ ] 실시간 대시보드 (선택사항)

---

### 🐛 트러블슈팅

#### WiFi 연결 안 됨
```
E (XXX) WIFI_MGR: Failed to connect to AP after 5 attempts
E (XXX) WIFI_APP: WiFi connection failed
```

**해결 방법:**
- SSID와 비밀번호 확인 (`idf.py menuconfig`)
- WiFi AP가 켜져 있는지 확인
- 2.4GHz WiFi 사용 확인 (ESP32는 5GHz 미지원)
- 인증 모드 확인 (WPA2-PSK 권장)

#### IP 주소를 받지 못함
```
W (XXX) WIFI_MGR: Connection timeout
```

**해결 방법:**
- 라우터의 DHCP 서버 확인
- WiFi 신호 강도 확인
- 타임아웃 시간 증가 (30초 → 60초)

---

### 📚 참고 자료

- **ESP-IDF WiFi 드라이버**: [WiFi Driver Documentation](https://docs.espressif.com/projects/esp-idf/en/latest/esp32/api-guides/wifi.html)
- **ESP-IDF 네트워크 인터페이스**: [ESP-NETIF Documentation](https://docs.espressif.com/projects/esp-idf/en/latest/esp32/api-reference/network/esp_netif.html)
- **FreeRTOS 이벤트 그룹**: [Event Groups Documentation](https://www.freertos.org/event-groups-API.html)
- **NVS 스토리지**: [Non-Volatile Storage Documentation](https://docs.espressif.com/projects/esp-idf/en/latest/esp32/api-reference/storage/nvs_flash.html)

---

### ✍️ 작성자 노트

이번 업데이트는 ESP32 IoT 프로젝트의 첫 번째 단계로 WiFi 연결 기능을 추가했습니다. WiFi manager 컴포넌트는 재사용 가능하도록 설계되었으며, 다른 ESP-IDF 프로젝트에서도 쉽게 통합할 수 있습니다.

다음 단계에서는 HTTP 클라이언트를 구현하여 센서 데이터를 라즈베리파이 서버로 전송하는 기능을 추가할 예정입니다.

---

## 2025-11-05 (추가): WiFi 디버깅 및 개선

### 🔧 문제 발견 및 해결

#### 발견된 문제
초기 WiFi 구현 후 테스트 중 다음 문제들이 발견되었습니다:

1. **WiFi 스캔 타이밍 오류**
   ```
   W (573) wifi:sta_scan: STA is connecting, scan are not allowed!
   E (573) WIFI_MGR: WiFi scan failed: ESP_ERR_WIFI_STATE
   ```
   - WiFi 연결 프로세스가 시작된 후 스캔을 시도하여 실패
   - 사용자에게 안테나 문제로 오인될 수 있음

2. **안테나 연결 진단 어려움**
   - 안테나 미연결 시 명확한 진단 정보 부족
   - 신호 강도(RSSI) 정보 미표시

### ✅ 개선 사항

#### 1. WiFi 신호 강도(RSSI) 표시 추가

**수정된 파일: `components/wifi_manager/wifi_manager.c`**

WiFi 연결 성공 시 신호 강도를 표시하도록 이벤트 핸들러를 개선했습니다:

```c
case WIFI_EVENT_STA_CONNECTED:
    {
        wifi_event_sta_connected_t *event = (wifi_event_sta_connected_t *)event_data;
        ESP_LOGI(TAG, "Connected to AP");
        ESP_LOGI(TAG, "  SSID: %s", event->ssid);
        ESP_LOGI(TAG, "  Channel: %d", event->channel);

        // Get RSSI (signal strength)
        wifi_ap_record_t ap_info;
        if (esp_wifi_sta_get_ap_info(&ap_info) == ESP_OK) {
            ESP_LOGI(TAG, "  RSSI: %d dBm", ap_info.rssi);
        }
    }
    break;
```

**출력 예시:**
```
I (5513) WIFI_MGR: Connected to AP
I (5513) WIFI_MGR:   SSID: shinho2.4G
I (5513) WIFI_MGR:   Channel: 10
I (5513) WIFI_MGR:   RSSI: -79 dBm
```

**RSSI 해석:**
- -30 ~ -50 dBm: 매우 좋음 ⭐⭐⭐⭐⭐
- -50 ~ -70 dBm: 좋음 ⭐⭐⭐⭐
- -70 ~ -80 dBm: 약함 ⭐⭐⭐ (연결 가능하지만 불안정할 수 있음)
- -80 dBm 이하: 매우 약함 ⭐⭐ (연결 어려움)

#### 2. WiFi 네트워크 스캔 기능 추가

**수정된 파일:**
- `components/wifi_manager/wifi_manager.h`
- `components/wifi_manager/wifi_manager.c`

안테나 연결 상태를 진단하기 위한 WiFi 스캔 기능을 추가했습니다:

```c
/**
 * @brief Scan for available WiFi networks
 *
 * @param max_aps Maximum number of APs to scan (0 = scan all)
 * @return
 *     - ESP_OK: Scan completed successfully
 *     - ESP_FAIL: Scan failed
 */
esp_err_t wifi_manager_scan_networks(uint16_t max_aps);
```

**구현 특징:**
- 주변 WiFi 네트워크를 스캔하여 SSID, 채널, RSSI, 인증 방식 표시
- 네트워크가 하나도 발견되지 않으면 안테나 문제로 진단
- 표 형식으로 결과를 깔끔하게 출력

**출력 예시:**
```
I (XXX) WIFI_MGR: Scanning for WiFi networks...
I (XXX) WIFI_MGR: Found 5 WiFi networks:
I (XXX) WIFI_MGR: ------------------------------------------------------------
I (XXX) WIFI_MGR: SSID                              Channel  RSSI  Auth
I (XXX) WIFI_MGR: ------------------------------------------------------------
I (XXX) WIFI_MGR: shinho2.4G                              10   -45  WPA2
I (XXX) WIFI_MGR: NeighborWiFi                             1   -67  WPA2
I (XXX) WIFI_MGR: CoffeeShop_Guest                        11   -72  OPEN
I (XXX) WIFI_MGR: ------------------------------------------------------------
```

**안테나 미연결 시 경고:**
```
W (XXX) WIFI_MGR: No WiFi networks found!
W (XXX) WIFI_MGR: Possible causes:
W (XXX) WIFI_MGR:   1. No antenna connected
W (XXX) WIFI_MGR:   2. Antenna switch set to wrong position
W (XXX) WIFI_MGR:   3. WiFi hardware issue
```

#### 3. WiFi 스캔 타이밍 최적화

**수정된 파일: `main/blink_example_main.c`**

**변경 전 (문제 있는 코드):**
```c
wifi_manager_init(&wifi_config);  // WiFi 시작 (자동 연결 시작)
wifi_manager_scan_networks(10);   // 스캔 시도 → 오류 발생!
wifi_manager_wait_for_connection(30000);
```

**변경 후 (수정된 코드):**
```c
wifi_manager_init(&wifi_config);  // WiFi 시작
wifi_manager_wait_for_connection(30000);  // 바로 연결 대기
```

스캔 기능은 남겨두었지만, 기본적으로는 사용하지 않도록 수정했습니다. 필요시 `wifi_manager_init()` 호출 전에 별도로 스캔을 실행할 수 있습니다.

#### 4. 연결 실패 시 체크리스트 추가

**수정된 파일: `main/blink_example_main.c`**

WiFi 연결 실패 시 사용자가 확인해야 할 항목을 명확히 표시하도록 개선했습니다:

```c
if (ret != ESP_OK) {
    ESP_LOGE(TAG_WIFI, "WiFi connection failed");
    ESP_LOGE(TAG_WIFI, "Please check:");
    ESP_LOGE(TAG_WIFI, "  1. Antenna is properly connected");
    ESP_LOGE(TAG_WIFI, "  2. SSID '%s' exists in scan results above", CONFIG_WIFI_SSID);
    ESP_LOGE(TAG_WIFI, "  3. Password is correct");
    ESP_LOGE(TAG_WIFI, "  4. Router is 2.4GHz (ESP32 doesn't support 5GHz)");
    return ret;
}
```

### 📊 테스트 결과

#### 성공적인 연결 로그 예시:

```
I (323) WIFI_APP: Initializing WiFi...
I (323) WIFI_APP: Target SSID: shinho2.4G
I (333) WIFI_MGR: Initializing WiFi manager...
I (443) WIFI_MGR: WiFi manager initialized
I (443) WIFI_MGR: Connecting to SSID: shinho2.4G
I (453) WIFI_APP: ==============================================
I (453) WIFI_APP: Attempting to connect to: shinho2.4G
I (463) WIFI_APP: ==============================================
I (473) WIFI_MGR: WiFi started, connecting to AP...
I (1643) wifi:new:<10,2>, old:<1,0>, ap:<255,255>, sta:<10,2>, prof:1
I (1643) wifi:state: init -> auth (0xb0)
I (1653) wifi:state: auth -> assoc (0x0)
I (5513) wifi:connected with shinho2.4G, aid = 5, channel 10, 40D
I (5513) wifi:security: WPA2-PSK, phy: bgn, rssi: -79
I (5513) WIFI_MGR: Connected to AP
I (5513) WIFI_MGR:   SSID: shinho2.4G
I (5513) WIFI_MGR:   Channel: 10
I (5513) WIFI_MGR:   RSSI: -79 dBm
I (5543) WIFI_MGR: Got IP address: 192.168.1.100
I (5543) WIFI_MGR: Connected to AP successfully
I (5543) WIFI_APP: WiFi connected successfully!
I (5543) WIFI_APP: IP Address: 192.168.1.100
```

#### 연결 성공 확인 지표:
- ✅ WiFi 연결: `connected with shinho2.4G`
- ✅ 보안: WPA2-PSK
- ✅ 채널: 10
- ✅ 신호 강도: -79 dBm (약함, 개선 권장)
- ✅ IP 할당: 192.168.1.100

### 🛠️ WiFi 설정

**설정 예시 (`sdkconfig.defaults`):**
```ini
CONFIG_WIFI_SSID="your-ssid-2.4G"
CONFIG_WIFI_PASSWORD="your-password"
CONFIG_WIFI_MAXIMUM_RETRY=5
CONFIG_WIFI_SCAN_AUTH_MODE_THRESHOLD=3
```
> 실제 WiFi 정보는 menuconfig 또는 설정 툴(NVS)로 입력하세요. 저장소에는 더미 값만 둡니다.

### 📝 알려진 제한사항

1. **신호 강도 약함 (-79 dBm)**
   - 현재 테스트 환경에서 신호가 약함
   - 권장 개선 방법:
     - ESP32를 WiFi 라우터에 1~3m 이내로 이동
     - 안테나 방향을 수직으로 조정
     - 금속 물체나 벽 등 장애물 제거

2. **ESP32-S3 안테나 요구사항**
   - U.FL/IPEX 커넥터가 있는 경우 외부 안테나 필수
   - PCB 안테나가 있는 보드는 별도 안테나 불필요
   - 일부 보드는 안테나 선택 스위치/점퍼 확인 필요

3. **5GHz WiFi 미지원**
   - ESP32는 2.4GHz WiFi만 지원
   - 라우터가 듀얼 밴드인 경우 2.4GHz 대역 사용 확인 필요

### 🔍 디버깅 도구

WiFi 문제 진단을 위한 새로운 함수:

```c
// 주변 WiFi 네트워크 스캔 (안테나 테스트용)
esp_err_t wifi_manager_scan_networks(uint16_t max_aps);
```

사용 예시 (필요시):
```c
// WiFi 초기화 전에 스캔하여 안테나 상태 확인
wifi_manager_scan_networks(10);  // 최대 10개 네트워크 표시
```

---

**마지막 업데이트:** 2025-11-05
**버전:** 1.1.1 (WiFi 디버깅 개선)
