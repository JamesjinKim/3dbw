# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 프로젝트 개요

ESP32-S3 기반 GPIO 신호 전송 테스트 프로젝트입니다. ESP-IDF 프레임워크를 사용하여 개발되었으며, 4개의 커넥터(EX1~EX4) 간 GPIO 신호 전송을 테스트합니다.

**주요 기능:**
- WiFi 연결 및 상태 모니터링 (색상 강조 표시)
- GPIO 신호 전송 테스트 (EX1→EX2, EX3→EX4)
- GPIO 인터럽트를 이용한 입력 감지
- MAC 주소 및 IP 주소 색상 표시

**현재 개발 환경:**
- **플랫폼:** 라즈베리파이 5 (Raspberry Pi OS, Linux 6.12.47+rpt-rpi-2712, ARM64)
- **ESP-IDF:** v5.4.3 (경로: `/home/shinho/esp/v5.4.3/esp-idf`)
- **툴체인:** `/home/shinho/.espressif`
- **Python:** 3.11.2
- **CMake:** 3.25.1
- **빌드 시스템:** Ninja
- **타겟:** ESP32-S3 (QFN56, 8MB PSRAM)
- **시리얼 포트:** `/dev/ttyACM0`
- **IDE:** VSCode + ESP-IDF Extension

## 프로젝트 구조

- **components/iis3dwb/**: IIS3DWB 진동 센서 드라이버 컴포넌트
  - `iis3dwb.h`: 드라이버 헤더 파일 (레지스터 정의, API 선언)
  - `iis3dwb.c`: 드라이버 구현 (SPI 통신, 센서 제어, 가속도 계산)
  - `CMakeLists.txt`: 컴포넌트 빌드 설정
- **components/wifi_manager/**: WiFi 관리 컴포넌트
  - `wifi_manager.h`: WiFi 관리 API 선언
  - `wifi_manager.c`: WiFi 연결, 재연결, 상태 모니터링 구현
  - `CMakeLists.txt`: 컴포넌트 빌드 설정
- **components/tsl2591/**: TSL2591 조도 센서 드라이버 (레거시, 미사용)
- **main/**: 메인 애플리케이션 코드
  - `blink_example_main.c`: app_main() 진입점, WiFi 초기화, 센서 읽기 태스크
  - `Kconfig.projbuild`: 프로젝트 설정 메뉴 정의 (WiFi, SPI 핀, 센서 설정)
  - `CMakeLists.txt`: 메인 컴포넌트 빌드 설정
- **sdkconfig.defaults**: 기본 설정 파일

## 주요 명령어

### 개발 환경 설정 (라즈베리파이)
```bash
# ESP-IDF 환경 활성화 (매번 터미널 시작 시 필요)
source ~/esp/v5.4.3/esp-idf/export.sh

# 또는 .bashrc에 alias 추가
echo "alias get_idf='. ~/esp/v5.4.3/esp-idf/export.sh'" >> ~/.bashrc

# 타겟 칩 설정 (프로젝트 설정 전 필수)
idf.py set-target esp32s3      # ESP32-S3용 (현재 프로젝트)
```

### 프로젝트 설정
```bash
# 프로젝트 설정 메뉴 열기
idf.py menuconfig

# 설정 가능 항목:
# - WiFi Configuration: SSID, Password, 재시도 횟수
# - IIS3DWB Vibration Sensor Configuration: SPI 핀, 풀스케일, 대역폭, 읽기 주기
```

### 빌드 및 플래시 (라즈베리파이)
```bash
# 프로젝트 빌드
idf.py build

# ESP32-S3 연결 확인
ls -l /dev/ttyACM*

# 빌드 + 플래시 + 시리얼 모니터
idf.py -p /dev/ttyACM0 flash monitor

# 시리얼 모니터만 실행
idf.py -p /dev/ttyACM0 monitor
# (종료: Ctrl+])

# 빌드 디렉토리 정리
rm -rf build
idf.py fullclean
```

### VSCode에서 빌드 (권장)
```
F1 → ESP-IDF: Build your project
F1 → ESP-IDF: Flash your project
F1 → ESP-IDF: Monitor your device
F1 → ESP-IDF: Build, Flash and start a monitor on your device
```

## 아키텍처 핵심 개념

### IIS3DWB 드라이버 구조

드라이버는 계층적 API 구조로 설계되었습니다:

1. **저수준 SPI 통신 계층** (`iis3dwb.c` 내부)
   - `iis3dwb_write_register()`: 단일 레지스터 쓰기
   - `iis3dwb_read_register()`: 단일 레지스터 읽기
   - `iis3dwb_read_registers()`: 연속 레지스터 읽기
   - ESP-IDF의 `spi_device_polling_transmit()` API 사용

2. **센서 제어 계층** (공개 API)
   - `iis3dwb_init()`: SPI 초기화, 센서 ID 확인, 기본 설정
   - `iis3dwb_enable()`: 가속도계 전원 제어
   - `iis3dwb_set_full_scale()`: 풀스케일 설정 (±2g/±4g/±8g/±16g)
   - `iis3dwb_set_bandwidth()`: 저역통과 필터 대역폭 설정

3. **데이터 처리 계층** (공개 API)
   - `iis3dwb_read_raw_data()`: 3축 원시 가속도 값 읽기
   - `iis3dwb_read_accel_data()`: mg 단위 가속도 값 읽기
   - `iis3dwb_read_temperature()`: 센서 온도 읽기

### IIS3DWB 센서 사양

- **인터페이스**: SPI (최대 10MHz), Mode 3 (CPOL=1, CPHA=1)
- **WHO_AM_I**: 0x0F 레지스터, 값 0x7B
- **출력 데이터율(ODR)**: 26.667 kHz (고정)
- **대역폭**: DC ~ 6 kHz (초광대역 진동 모니터링용)
- **풀스케일 옵션**: ±2g, ±4g, ±8g, ±16g
- **감도**:
  - ±2g: 0.061 mg/LSB
  - ±4g: 0.122 mg/LSB
  - ±8g: 0.244 mg/LSB
  - ±16g: 0.488 mg/LSB
- **FIFO**: 3KB 내장 (512샘플)

### SPI 통신 프로토콜

IIS3DWB SPI 통신 규칙:
- 읽기: 주소 바이트 MSB = 1 (0x80 | reg_addr)
- 쓰기: 주소 바이트 MSB = 0 (0x00 | reg_addr)
- 연속 읽기: 자동 주소 증가 (CTRL3_C.IF_INC 비트 설정 필요)

### 설정 시스템 (Kconfig)

`main/Kconfig.projbuild`에서 다음 파라미터를 설정 가능:

**WiFi 설정:**
- SSID, Password
- 최대 재시도 횟수
- 인증 모드 임계값

**IIS3DWB 설정:**
- SPI 호스트 (SPI2/SPI3)
- GPIO 핀 (MOSI, MISO, SCLK, CS)
- SPI 클럭 속도 (100kHz ~ 10MHz)
- 풀스케일 (±2g/±4g/±8g/±16g)
- 저역통과 필터 대역폭
- 센서 읽기 주기

설정 값은 `CONFIG_IIS3DWB_*` 및 `CONFIG_WIFI_*` 매크로로 코드에서 사용됩니다.

### 컴포넌트 구조

- `components/iis3dwb`은 독립적인 재사용 가능한 드라이버
- `components/wifi_manager`는 WiFi STA 모드 관리 컴포넌트
- `main` 컴포넌트는 `CMakeLists.txt`에서 `REQUIRES iis3dwb wifi_manager`로 의존성 선언
- 다른 ESP-IDF 프로젝트에서 컴포넌트 디렉토리를 복사하여 재사용 가능

## 하드웨어 연결

### GPIO 커넥터 핀맵

**EX1 (출력) → EX2 (입력) - 랜케이블로 연결:**
| PIN | EX1 (출력) | EX2 (입력) |
|-----|-----------|-----------|
| 1 | GPIO4 | GPIO6 |
| 2 | GPIO11 | GPIO7 |
| 3 | GPIO12 | GPIO15 |
| 4 | GPIO13 | GPIO17 |
| 5 | GPIO14 | GPIO18 |

**EX3 (출력) → EX4 (입력) - 랜케이블로 연결:**
| PIN | EX3 (출력) | EX4 (입력) |
|-----|-----------|-----------|
| 1 | GPIO45 | GPIO1 |
| 2 | GPIO48 | GPIO2 |
| 3 | GPIO47 | GPIO42 |
| 4 | GPIO9 | GPIO41 |
| 5 | GPIO10 | GPIO40 |

### 터미널 색상 표시

WiFi 및 디바이스 정보가 터미널에 색상으로 강조 표시됩니다:

**WiFi 연결 성공 시 (녹색 배경):**
```
  ★ WiFi Connected Successfully!
  ┌─────────────────────────────────┐
  │ SSID: YourSSID                  │
  │ IP  : 192.168.1.100             │  (노란색)
  │ MAC : 98:A3:16:DE:B1:70         │  (마젠타)
  └─────────────────────────────────┘
```

**WiFi 연결 실패 시 (빨간색 배경):**
```
  ✗ WiFi Connection Failed!
  Could not connect to: YourSSID
```

**MAC 주소 (WiFi 연결 전에도 표시, 시안색 박스):**
```
  ┌─────────────────────────────────┐
  │ MAC : 30:ED:A0:21:3D:3C         │  (마젠타)
  └─────────────────────────────────┘
```

### WiFi 안테나
- ESP32-S3 보드에 U.FL/IPEX 커넥터가 있는 경우 외부 안테나 필수
- PCB 안테나가 있는 보드는 별도 안테나 불필요
- 안테나 선택 스위치/점퍼 확인 필요 (일부 보드)

## ESP-IDF 개발 참고사항

### 라즈베리파이 5 환경 특이사항
- **빌드 속도**: 첫 빌드 약 5분, 재빌드 약 30초
- **ccache 활용**: 재빌드 속도 향상을 위해 ccache 활성화 권장
- **SSH 개발**: 원격 SSH로 개발 가능 (24시간 개발 서버)
- **USB 권한**: `dialout` 그룹에 사용자 추가 필요 (이미 설정됨)
- **시리얼 포트**: Windows COM 포트 대신 `/dev/ttyACM0` 사용

### FreeRTOS 사용
- `app_main()`에서 `xTaskCreate()`로 센서 읽기 태스크 생성
- `vTaskDelay(pdMS_TO_TICKS(ms))`로 태스크 지연 (틱 단위 변환)
- 센서 읽기는 별도 태스크에서 주기적으로 실행
- WiFi 연결은 메인 태스크에서 초기화

### 로깅 시스템
- `ESP_LOGI()`: 일반 정보 (센서 값, 초기화 상태, WiFi 연결)
- `ESP_LOGW()`: 경고 (WiFi 연결 해제, 높은 진동)
- `ESP_LOGE()`: 오류 (SPI 통신 실패, 센서 미인식, WiFi 연결 실패)
- `ESP_LOGD()`: 디버그 (레지스터 값, 상세 정보)

### SPI 통신 주의사항
- SPI Mode 3 사용 (CPOL=1, CPHA=1)
- 최대 클럭 속도 10MHz (안정성 위해 1MHz 기본)
- CS 핀은 하드웨어 제어 (ESP-IDF SPI 드라이버가 자동 관리)
- DMA 전송 사용 (연속 레지스터 읽기 시)

### WiFi 사용
- 2.4GHz WiFi만 지원 (5GHz 미지원)
- Station 모드로 동작
- 최대 5회 재연결 시도 (menuconfig에서 변경 가능)
- 연결 실패 시에도 센서/GPIO 테스트 기능은 계속 동작

### WiFi 연결 타이밍 (wifi_manager.c)
WiFi 연결 안정성을 위해 다음과 같은 대기 시간이 설정되어 있습니다:

**초기 연결:**
- WiFi 하드웨어 초기화 후 **3초 대기** 후 첫 연결 시도

**재시도 간격 (점진적 증가):**
| 재시도 | 대기 시간 |
|--------|----------|
| 1회차 | 7초 |
| 2회차 | 9초 |
| 3회차 | 11초 |
| 4회차 | 13초 |
| 5회차 | 15초 |

**총 연결 타임아웃:** 60초 (main.c에서 설정)

이 설정은 라우터가 바쁜 상황이나 신호가 약한 환경에서도 안정적인 연결을 보장합니다.

## 개발 팁

### 센서 트러블슈팅

**센서 인식 안 됨 (Device ID 읽기 실패)**
- SPI 배선 확인 (MOSI, MISO, SCLK, CS 연결)
- 전원 전압 확인 (VDD, VDDIO 모두 2.1~3.6V)
- SPI 모드 확인 (Mode 3 필수)
- CS 핀이 제대로 동작하는지 확인

**측정값이 0 또는 이상함**
- 센서 활성화 여부 확인 (CTRL1_XL.XL_EN 비트)
- BDU(Block Data Update) 설정 확인
- 데이터 준비 상태 확인 (STATUS_REG.XLDA 비트)

**고주파 노이즈가 심함**
- 저역통과 필터 대역폭 낮춤 (ODR/4 → ODR/100 등)
- 디커플링 커패시터 추가
- SPI 클럭 속도 낮춤

### 전력 최적화
- 미사용 시 `iis3dwb_enable(handle, false)`로 센서 끔
- 읽기 주기를 늘림 (100ms → 1초 이상)
- Deep Sleep 모드 사용 고려 (저전력 애플리케이션)

### 코드 수정 시 주의사항
- SPI 읽기 시 주소 MSB를 1로 설정 (0x80 | reg_addr)
- SPI 쓰기 시 주소 MSB를 0으로 설정 (0x00 | reg_addr)
- 풀스케일 비트 위치: CTRL1_XL[3:2]
- 대역폭 비트 위치: CTRL6_C[2:0]
- SPI 통신 실패 시 에러 처리 필수

## VSCode 통합

- `.vscode/settings.json`에 ESP-IDF 경로와 도구 설정 포함
- clangd를 사용한 IntelliSense 설정 구성
- ESP-IDF 확장 프로그램 필수 (`espressif.esp-idf-extension`)

## DevContainer 지원

- `.devcontainer/` 디렉토리에 ESP-IDF QEMU 환경 설정 포함
- Docker 기반 개발 환경 제공

## 참고 문서

- **IIS3DWB 데이터시트**: STMicroelectronics (dm00501492)
- **ESP32-S3 기술 레퍼런스**: Espressif 공식 문서
- **ESP-IDF SPI 드라이버 가이드**: https://docs.espressif.com/projects/esp-idf/en/latest/api-reference/peripherals/spi_master.html
