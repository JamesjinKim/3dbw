# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 프로젝트 개요

ESP32-S3 + IIS3DWB 초광대역 진동센서로 진동 데이터를 고속 수집해
**WiFi(UDP) 또는 USB 시리얼 직결**로 라즈베리파이에 스트리밍하는 프로젝트입니다.
설정(WiFi·서버IP·샘플레이트·풀스케일)은 NVS에 저장되며 별도 GUI 툴로 주입합니다.

**주요 기능:**
- IIS3DWB FIFO 고속 수집 (최대 26.6 kHz) — 폴링/인터럽트 선택
- 전송 경로 배타 선택: `transport=0` WiFi UDP / `transport=2` USB 시리얼 직결
- NVS 기반 런타임 설정 (`config_manager`) + 시리얼 명령 프로토콜 (`serial_protocol`)
- WiFi 연결 및 상태 모니터링 (색상 강조 표시), 공장 초기화
- 공정 수집기 (`rpi-collector/`) — 포토센서 트리거로 2대 동시 수집, CSV/바이너리 저장

**현재 개발 환경 (2026-09-22 확인):**
- **플랫폼:** 라즈베리파이 4 Model B Rev 1.5 (Raspberry Pi OS, Linux 6.18.39+rpt-rpi-v8, ARM64)
- **ESP-IDF:** v5.4.3 (경로: `~/esp/v5.4.3/esp-idf`) — 설치: `./setup-rpi.sh`
- **툴체인:** `~/.espressif`
- **Python:** 3.13.5 (시스템) / ESP-IDF 는 자체 venv 사용
- **CMake:** 3.31.6 · **Ninja:** 1.12.1 · **빌드 시스템:** Ninja
- **타겟:** ESP32-S3 (QFN56, 8MB PSRAM)
- **시리얼 포트:** `/dev/ttyACM0` (ESP32-S3 내장 USB) 또는 `/dev/ttyUSB0` (UART 브리지 보드)
- **IDE:** VSCode + ESP-IDF Extension

> 이 프로젝트는 macOS 에서 이관됐고, **2026-09-23 에 맥 시절 잔재를 모두 정리했다.**
> 지운 것: `iis_config_tool/`(Tauri, 맥에 원본 보관) · `rpi-receiver/`(수집기가 대체) ·
> `components/tsl2591/`(빌드 안 됨) · `flash.sh`(run.sh 와 중복) · 다른 칩용
> `sdkconfig.defaults.*` 8개 · 맥 경로가 박힌 `docs/html`·`docs/01-plan`·`docs/superpowers` ·
> `PROJECT.md`(TSL2591 시절 변경내역). 모두 git 이력에 남아 있어 복구 가능하다.

## 프로젝트 구조

- **components/iis3dwb/**: IIS3DWB 진동 센서 드라이버 컴포넌트
  - `iis3dwb.h`: 드라이버 헤더 파일 (레지스터 정의, API 선언)
  - `iis3dwb.c`: 드라이버 구현 (SPI 통신, 센서 제어, 가속도 계산)
  - `CMakeLists.txt`: 컴포넌트 빌드 설정
- **components/wifi_manager/**: WiFi 관리 컴포넌트
  - `wifi_manager.h`: WiFi 관리 API 선언
  - `wifi_manager.c`: WiFi 연결, 재연결, 상태 모니터링 구현
  - `CMakeLists.txt`: 컴포넌트 빌드 설정
- **components/config_manager/**: NVS 기반 런타임 설정 (WiFi·서버IP·레이트·풀스케일)
- **components/sensor_streamer/**: 패킷 조립 + 전송 (UDP / USB 시리얼) — 패킷 계약 v2
- **components/serial_protocol/**: 시리얼 명령 프로토콜 (설정 주입·공장 초기화)
- **main/**: 메인 애플리케이션 코드
  - `main.c`: app_main() 진입점, 전송 모드 판정, WiFi 초기화, 스트리머 기동
  - `Kconfig.projbuild`: 프로젝트 설정 메뉴 정의 (WiFi, SPI 핀, 센서 설정)
  - `CMakeLists.txt`: 메인 컴포넌트 빌드 설정
- **sdkconfig.defaults**: 기본 설정 · **sdkconfig.defaults.deploy**: 배포 빌드 오버레이
- **tools/**: 배포 빌드·패키징·USB 복구 스크립트 · **deploy/**: 두 패키지 공용 `run.sh`(진입점) ·
  `install.sh`(오프라인 설치) · `70-iis3dwb.rules`(udev)
- **vendor/**: 폐쇄망 배포용 파이썬 라이브러리 원본 (esptool sdist · pyserial · intelhex, `SHA256SUMS`)
- **docs/**: 패킷 규격 · 시리얼 전송 설계 · FIFO 인터럽트 · 데이터시트
- **rpi-collector/**: 공정 수집기 (배포물 ③)
  - `iis3dwb_packet.py`: 패킷 파서 (v1 16B / v2 18B 헤더) — **정본은 여기 하나**
  - `session.py`: 채널 상태머신. 진동센서 1대 ↔ 포토센서 1개가 독립 동작
  - `sensor_link.py` / `writer.py`: 읽기·쓰기 스레드 분리 (조용한 유실 방지)
  - `trigger.py`: 포토센서 엣지 인터럽트 (DIN1=GPIO5 / DIN2=GPIO17)
  - `gpio_cdev.py`: 커널 GPIO v2 uAPI 를 순수 파이썬 ioctl 로 — trigger 의 기본 백엔드.
    lgpio 는 커널이 v2 를 모를 때(5.10 미만)만 쓰는 예비
  - `slots.py`: USB 슬롯 ↔ 센서 이름 + **udev 고정 장치 이름**(`/dev/iis3dwb1`)
  - **공식 명칭: "SHT 진동센서 수집"** (창 제목·바탕화면 아이콘·도움말)
  - `collector_gui.py`: **수집기 화면(GUI)** — `bash run.sh` 기본. 센서 칸마다 자기 포토센서
    실시간 상태·DIN 선택·수동 시작/중지·진행률. 공통 설정(수집 시간 등)을 화면에서 바꾼다
  - `settings.py`: 공통 설정 저장(`settings.json`) — **GUI 와 글자 화면이 같은 파일**을 쓴다
    (Lite 에서 글자 화면으로 돌아도 GUI 에서 정한 수집 시간이 적용되게)
  - `collect_cli.py`: 글자 화면 — 화면이 없는 RPi(Lite·SSH)에서 run.sh 가 대신 띄운다
  - `udp_receiver.py`: WiFi(UDP) 수신 → CSV
  - `bin_to_csv.py`: 바이너리 → CSV · `help.html`: 오프라인 사용설명서
  - `vendor_path.py`: 배포 패키지의 `vendor/`(pyserial)를 import 경로 앞에 넣는다
  - `selftest.py`: 센서·GPIO 없이 도는 자체 검증 (실제 USB 상태와 무관해야 한다)
- **boardcheck/**: 보드 수입검사 전용 펌웨어 + 실행기 (**독립 ESP-IDF 프로젝트**)
  - 하드웨어를 변경했거나 새 보드를 받았을 때 소프트웨어 작업 전에 먼저 돌리는 관문
  - `./run.sh` 한 줄로 굽고 검사해 PASS/FAIL 판정 (종료코드 0/1/2/3)
  - 검사 6종: MCU · SPI/WHO_AM_I · 가속도 출력 · FIFO/ODR · **INT1 전기상태** · INT 발생률
  - INT1 단선/극성반전을 핀 레벨에서 가른다 (실제로 단선 보드를 잡아낸 항목)
  - 상위 프로젝트와 `components/iis3dwb` 만 공유하고 나머지는 빌드하지 않는다
    (`set(COMPONENTS main)`) — 스트리밍 코드가 바뀌어도 검사 결과가 흔들리지 않는다
- **sensor-setup-py/**: 센서 설정 주입 GUI (tkinter, 크로스플랫폼 — **라즈베리파이 권장**)
  - **공식 명칭: "SHT 진동센서 설정"** (창 제목·화면 머리글·바탕화면 아이콘·도움말. 예전 이름
    "IIS3DWB 센서 설정 툴" 은 쓰지 않는다). 화면 용어: **진동센서 목록**(보드 목록 그룹),
    **MAC 주소 확인**(MAC 읽기 버튼). 설정툴 화면에는 수집기 명령을 넣지 않는다.
- **setup-rpi.sh**: 라즈베리파이 환경 일괄 구성 (ESP-IDF + 의존 패키지 + 권한)

## 주요 명령어

### 개발 환경 설정 (라즈베리파이)
```bash
# ESP-IDF 환경 활성화 (매번 터미널 시작 시 필요)
source ~/esp/v5.4.3/esp-idf/export.sh

# 또는 .bashrc에 alias 추가
echo "alias get_idf='. ~/esp/v5.4.3/esp-idf/export.sh'" >> ~/.bashrc

# 타겟 칩은 sdkconfig.defaults 의 CONFIG_IDF_TARGET="esp32s3" 로 고정되어 있어
# 별도 set-target 없이 `idf.py build` 만으로 esp32s3 로 빌드된다.
# (다른 칩으로 시험할 때만 명시적으로 바꾼다)
# idf.py set-target esp32s3
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

**IIS3DWB 설정** — 위치는 `components/iis3dwb/Kconfig` (menuconfig 에서는
`Component config → IIS3DWB Vibration Sensor Configuration`).
`boardcheck/` 가 같은 핀맵으로 검사해야 하므로 **핀 정의는 이 파일 한 곳에만**
둔다. 두 프로젝트가 각자 핀 번호를 들고 있으면 하드웨어 변경 시 한쪽만 고쳐져
"검사는 통과인데 실물은 다른 핀" 이 된다:
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

### 라즈베리파이 환경 특이사항
- **빌드 속도**: 라즈베리파이 4 기준 첫 빌드 약 10~15분, 재빌드 약 1~2분
  (라즈베리파이 5 는 대략 절반. `setup-rpi.sh` 가 ccache 를 함께 설치한다)
- **ccache 활용**: 재빌드 속도 향상을 위해 ccache 활성화 권장
- **SSH 개발**: 원격 SSH로 개발 가능 (24시간 개발 서버)
- **USB 권한**: `dialout` 그룹에 사용자 추가 필요 (설정됨 — `id` 로 확인)
- **시리얼 포트**: Windows COM 포트나 macOS `/dev/cu.*` 대신 `/dev/ttyACM0` 사용
- **내장 UART 혼동 주의**: `/dev/ttyAMA*`, `/dev/serial0` 은 라즈베리파이 자체 UART로
  IIS3DWB 와 무관하다. 스크립트·수신기 모두 이들을 후보에서 제외한다.
- **메모리**: 8GB 모델 기준 여유. 2GB 모델에서 빌드 시 `-j2` 로 병렬도를 낮출 것.

### 라즈베리파이 USB 시리얼 트러블슈팅 (실제 겪은 문제)

**증상: 읽기는 되는데 쓰기만 영구 블로킹**
- 부팅 로그는 정상 수신되지만 `esptool` 이 `Write timeout` 으로 실패
- `write()` 가 반환하지 않고 `TIOCOUTQ`(pyserial `out_waiting`) 가 줄지 않음
- 드라이버 unbind/rebind 로는 **복구되지 않는다** (엔드포인트 상태가 남는다)

**원인**: USB-UART 브리지의 bulk OUT 엔드포인트 스톨. ModemManager 가 새로
나타난 `ttyACM*` 을 모뎀으로 의심해 AT 명령을 쏘는 과정에서 유발되는 경우가 많다.

**복구**: USB 장치 레벨 리셋 (USBDEVFS_RESET)
```bash
./tools/usb-recover.sh                 # 브리지가 1대일 때 — 찾아서 리셋
./tools/usb-recover.sh /dev/iis3dwb2   # 여러 대일 때 — 멈춘 포트를 지정
```
같은 기종 브리지가 2대면 VID:PID 로 구분할 수 없어, 포트를 지정하지 않으면 거부한다
(예전에는 첫 번째를 말없이 리셋해 멀쩡한 센서가 리셋됐다). 설정툴 GUI 는
`sensor-setup-py/usb_reset.py` 로 포트 기준 리셋을 한다.

**재발 방지**: `/etc/udev/rules.d/99-iis3dwb-no-modemmanager.rules` 로 ModemManager 가
이 포트를 무시하게 한다 (`ID_MM_DEVICE_IGNORE=1`). 새 라즈베리파이에서는
`setup-rpi.sh` 가 이 규칙을 설치한다.

> ⚠️ 진단 시 주의: `python3` 출력을 파이프로 받으면 블록 버퍼링 때문에 `timeout` 으로
> 죽일 때 출력이 유실된다. 시리얼 디버깅은 **반드시 `python3 -u`** 로 실행할 것.

### 검증된 하드웨어 경로 (혼동 주의)

**esptool(플래시·NVS 주입)과 앱의 시리얼 기능은 서로 다른 채널을 쓴다.**

| 기능 | 채널 | 이 보드에서 |
|------|------|------------|
| `esptool` 플래시 / NVS 주입 | ROM 부트로더 **UART0** | ✅ Cypress USB-UART 브리지로 동작 |
| 콘솔 로그 (`idf.py monitor`) | **UART0** (`CONFIG_ESP_CONSOLE_UART_NUM=0`) | ✅ 같은 브리지 |
| 시리얼 스트리밍 (`transport=2`) | **UART0** 2 Mbps (`CONFIG_STREAM_SERIAL_CHANNEL_UART0`) | ✅ 26,733 Hz · 유실 0% 실측 |
| 앱 설정 명령 (`serial_protocol`) | 네이티브 USB (`usb_serial_jtag_read_bytes`) | ❌ 이 보드는 네이티브 USB 미배선 |

**이 보드의 USB 커넥터는 ESP32-S3 내장 USB 가 아니라 Cypress 브리지로 이어진다.**
그래서 스트리밍을 UART0 로 내보내도록 `CONFIG_STREAM_SERIAL_CHANNEL` 을 두었다.
내장 USB 로 직결된 보드라면 `USB_JTAG` 로 바꾼다 — 틀리면 설정은 주입되지만
데이터가 어디에도 도달하지 않아 원인 찾기 어려운 고장으로 보인다.

UART0 는 실제 UART 라 보드레이트가 속도를 직접 제한한다. 26.6 kHz 는
**1.62 Mbps** 를 요구하므로(26,667 × 6B × 10/8 + 헤더) 2,000,000 bps 가 최소값이다.

### 배포물 세 가지 (섞지 말 것)

| 배포물 | 어디서 쓰나 | ESP-IDF 필요 |
|---|---|---|
| `boardcheck/` | 보드 진단 — 새 보드·하드웨어 변경 시 **가장 먼저** | ✗ (dist/ 커밋됨) |
| `sensor-setup-py/` | 펌웨어 굽기 + 설정 주입 | ✗ (패키지의 firmware/ 사용) |
| `rpi-collector/` | 현장 RPi 에서 데이터 수집 | ✗ (`tools/make-collector-package.sh`) |

배포 폴더도 나눈다 — 설정툴 `dist/`, 수집기 `dist-collector/` (둘 다 git 무시). 각 폴더를
통째로 zip 해 사용자에게 주고(`SHT진동센서설정-<버전>.zip` / `SHT진동센서수집-<버전>.zip`,
zip 도 git 무시), 폴더에는 README.txt + 프로그램 폴더만 둔다. **이름에 `test` 를 넣지 않는다.**

### 폐쇄망 배포 (현장은 인터넷이 안 된다)

현장 RPi 는 폐쇄망이고 OS 버전·32/64비트·Desktop/Lite 여부를 모른다. 그래서 두 패키지는
**apt / pip 를 전혀 쓰지 않는다.**
- 순수 파이썬 라이브러리는 패키지의 `vendor/` 에 소스째 넣는다. 원본은 저장소의
  `vendor/`(해시 고정)이고 `tools/vendor_extract.py` 가 꺼낸다. 설정툴은 esptool·
  pyserial·intelhex, 수집기는 pyserial. esptool 은 PyPI 가 sdist 만 배포해 sdist 에서
  꺼낸다(piwheels 휠은 해시가 달라 쓰지 않는다). 패키지의 esptool 이 시스템 것보다 우선.
- **작업자가 아는 명령은 `bash run.sh` 하나다** (두 패키지 모두, 폴더는 분리).
  `deploy/run.sh` 한 벌을 두 패키지가 같이 쓰고, 들어 있는 파일로 어느 쪽인지 가른다.
  매번 `install.sh --check` → 빠진 것만 설치(sudo) → 실행. 수집기는 인자 없이 `--auto`,
  나머지 인자는 `collect_cli.py` 로, `slots`·`udp`·`help` 는 하위 명령. 설정툴은
  바탕화면 아이콘을 만든다(마지막 실행 폴더를 가리킴). `./run.sh` 대신 `bash run.sh`
  로 안내한다 — FAT USB 메모리로 옮기면 실행권한이 사라진다.
- `install.sh --check` 종료코드: 0 정상 / 1 설치로 못 고침(tkinter·python·손상) /
  3 설치하면 고쳐짐. run.sh 가 이것으로 갈린다.
- udev 규칙은 **`70-iis3dwb.rules`** 이고 `TAG+="uaccess"` 로 데스크톱 사용자에게 즉시
  권한을 준다 → 그룹 추가 후 재로그인이 필요 없다 (SSH 는 여전히 그룹·재접속).
  **번호가 73(seat-late) 보다 작아야** uaccess 가 먹는다. 0.0.6 까지 쓰던
  `99-iis3dwb.rules` 에서는 무시됐는데, 개발 RPi 는 apt 의 esptool·openocd 규칙이 같은
  ACL 을 붙여 줘서 드러나지 않았다 — 현장 RPi 에는 그 패키지가 없다. install.sh 가
  예전 99 파일을 지운다.
- 패키징 스크립트가 **`python3 -S`(시스템 site-packages 차단)로 import·selftest 를
  돌려** 시스템 패키지 없이 동작함을 단정한다. 이 RPi 에는 esptool·lgpio 가 설치돼 있어
  일반 실행으로는 폐쇄망 상황이 재현되지 않는다 — 현장 재현 시험도 `-S` 로 한다.
- 두 도구 모두 **Python 3.7+** 에서 동작한다 (vermin 정적 검사).
- 포토센서 GPIO 는 lgpio(C 확장) 대신 `gpio_cdev.py` 로 커널 인터페이스를 직접 쓴다.
  의존성 0, 커널 5.10+ (bullseye 이후) 면 OS·32/64비트·Python 버전 무관. lgpio 휠은
  64비트 bookworm 에서만 단독 동작해 패키지에 넣을 수 없었다 (2026-09-26 조사).
- 넣어 갈 수 없는 것: **tkinter**(Lite 이미지엔 없음 → GUI 불가, 수집기 CLI 는 가능).
  없을 때 이유를 화면에 남기고 죽지 않는다.

**개발용 `./dev-flash.sh`(예전 이름 `run.sh`)로 구운 펌웨어를 출하하지 말 것.** 개발 빌드는
`CONFIG_WIFI_PREFER_NVS=n` 이라 설정툴이 주입한 WiFi 를 무시한다. 배포에는
`tools/build-deploy.sh` → `tools/make-deploy-package.sh` 를 쓴다
(`esp_flash.flash_firmware()` 가 manifest 의 `wifi_prefer_nvs` 를 단정해 막는다).

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
