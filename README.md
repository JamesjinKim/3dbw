# ESP32-S3 GPIO 신호 전송 테스트 프로젝트

ESP32-S3 기반으로 4개의 커넥터(EX1~EX4) 간 GPIO 신호 전송을 테스트하는 프로젝트입니다. 랜케이블을 통해 EX1→EX2, EX3→EX4 간 신호 전송을 검증합니다.

## 지원 타겟

| ESP32-S3 |
| -------- |

권장: ESP32-S3-WROOM-1 모듈 기반 개발 보드

## 주요 기능

- **GPIO 출력/입력 테스트**: EX1,EX3는 출력, EX2,EX4는 입력 (인터럽트 사용)
- **신호 전송 검증**: HIGH/LOW 신호 전송 및 수신 확인
- **WiFi 연결**: 2.4GHz WiFi 연결 및 상태 모니터링
- **색상 표시**: MAC 주소, IP 주소, WiFi 상태를 터미널에 색상으로 강조
- **FreeRTOS 기반**: 비동기 GPIO 테스트 태스크
- **상세 로깅**: PASS/FAIL 상태, ISR 트리거 여부 출력

## 하드웨어 요구사항

### 부품
- ESP32-S3 개발 보드 (ESP32-S3-WROOM-1 권장)
- 랜케이블 2개 (EX1↔EX2, EX3↔EX4 연결용)
- 커넥터 보드 (EX1~EX4)

### GPIO 핀 연결

**EX1 (출력) → EX2 (입력) - 랜케이블로 연결:**
```
EX1 (OUTPUT)      EX2 (INPUT)
------------      -----------
GPIO4   -------   GPIO6    (PIN1)
GPIO11  -------   GPIO7    (PIN2)
GPIO12  -------   GPIO15   (PIN3)
GPIO13  -------   GPIO17   (PIN4)
GPIO14  -------   GPIO18   (PIN5)
```

**EX3 (출력) → EX4 (입력) - 랜케이블로 연결:**
```
EX3 (OUTPUT)      EX4 (INPUT)
------------      -----------
GPIO45  -------   GPIO1    (PIN1)
GPIO48  -------   GPIO2    (PIN2)
GPIO47  -------   GPIO42   (PIN3)
GPIO9   -------   GPIO41   (PIN4)
GPIO10  -------   GPIO40   (PIN5)
```

**참고**: 입력 핀(EX2, EX4)에는 내부 풀다운 저항이 활성화되어 있습니다.

## 사용 방법

### 1. 환경 설정

#### 라즈베리파이 5에서 ESP-IDF 설치

**VSCode Extension 사용 (권장):**
1. VSCode에서 ESP-IDF Extension 설치
2. F1 → `ESP-IDF: Configure ESP-IDF Extension` 실행
3. Express 모드 선택
4. ESP-IDF 버전: v5.4.3 선택
5. 설치 경로: `/home/사용자이름/esp/v5.4.3/esp-idf`

**수동 설치:**
```bash
# 필요한 패키지 설치
sudo apt-get update
sudo apt-get install git wget flex bison gperf python3 python3-pip \
    python3-venv cmake ninja-build ccache libffi-dev libssl-dev \
    dfu-util libusb-1.0-0

# ESP-IDF 클론
mkdir -p ~/esp
cd ~/esp
git clone --recursive https://github.com/espressif/esp-idf.git
cd esp-idf
./install.sh esp32s3

# 환경 변수 설정 (매번 새 터미널마다 실행 필요)
. ./export.sh
```

**타겟 설정:**
```bash
# ESP32-S3 사용 (권장)
idf.py set-target esp32s3

# 또는 ESP32 사용 시
# idf.py set-target esp32
```

### 2. 프로젝트 설정

센서 파라미터를 변경하려면 menuconfig를 사용하세요:

```bash
idf.py menuconfig
```

`TSL2591 Configuration` 메뉴에서 설정 가능:
- **I2C Port Number**: I²C 포트 (0 또는 1, 기본값: 1)
- **I2C SDA GPIO Number**: SDA 핀 번호 (기본값: 4)
- **I2C SCL GPIO Number**: SCL 핀 번호 (기본값: 11)
- **I2C Clock Frequency**: 클럭 속도 (기본값: 100000 Hz)
- **Integration Time**: 적분 시간 (100~600ms, 기본값: 100ms)
- **Gain Setting**: 게인 (LOW/MED/HIGH/MAX, 기본값: MED)
- **Sensor Read Interval**: 읽기 주기 (기본값: 1000ms)

### 3. 빌드 및 플래시

#### 라즈베리파이 5에서 빌드

```bash
# ESP-IDF 환경 활성화 처음 시작 시
source ~/esp/v5.4.3/esp-idf/export.sh

# 빌드
idf.py build

# ESP32-S3 연결 확인
ls -l /dev/ttyACM*
# 일반적으로 /dev/ttyACM0으로 표시됨

# 플래시
idf.py -p /dev/ttyACM0 flash

# 플래시 및 시리얼 모니터 (한 번에)
idf.py -p /dev/ttyACM0 flash monitor
```

**VSCode에서 빌드 (권장):**
- F1 → `ESP-IDF: Build your project`
- F1 → `ESP-IDF: Flash your project`
- F1 → `ESP-IDF: Monitor your device`
- 또는 F1 → `ESP-IDF: Build, Flash and start a monitor on your device`

시리얼 모니터 종료: `Ctrl+]`

1) 빌드 (컴파일)
# source ~/esp/v5.4.3/esp-idf/export.sh

idf.py build
app + bootloader + partition table 모두 빌드
결과물은 build/ 폴더에 생성
첫 빌드: 수 분 / 재빌드: 보통 30초~1분
2) 플래시 (보드에 굽기)

# idf.py -p /dev/cu.usbmodem1433301 flash
3) 모니터 (시리얼 로그 보기)

# idf.py -p /dev/cu.usbmodem1433301 monitor

## 예상 출력

### MAC 주소 표시 (WiFi 연결 전)
```
  ┌─────────────────────────────────┐
  │ MAC : 30:ED:A0:21:3D:3C         │  (마젠타 색상)
  └─────────────────────────────────┘
```

### WiFi 연결 성공 시
```
  ★ WiFi Connected Successfully!      (녹색 배경)
  ┌─────────────────────────────────┐
  │ SSID: shinho2.4G                │  (시안 색상)
  │ IP  : 192.168.0.48              │  (노란색)
  │ MAC : 30:ED:A0:21:3D:3C         │  (마젠타)
  └─────────────────────────────────┘
```

### WiFi 연결 실패 시
```
  ✗ WiFi Connection Failed!           (빨간색 배경)
  Could not connect to: shinho2.4G
```

### GPIO 테스트 출력
```
I (6203) GPIO_TEST: ===== Test #1 =====
I (6203) GPIO_TEST: [Phase 1] Setting all outputs HIGH...
I (6253) GPIO_TEST:   EX1 -> EX2 (expected: HIGH)
I (6253) GPIO_TEST:     PIN1: GPIO4->GPIO6 = 1 (ISR:1,T:1) [PASS]
I (6253) GPIO_TEST:     PIN2: GPIO11->GPIO7 = 1 (ISR:1,T:1) [PASS]
I (6263) GPIO_TEST:     PIN3: GPIO12->GPIO15 = 1 (ISR:1,T:1) [PASS]
I (6263) GPIO_TEST:     PIN4: GPIO13->GPIO17 = 1 (ISR:1,T:1) [PASS]
I (6273) GPIO_TEST:     PIN5: GPIO14->GPIO18 = 1 (ISR:1,T:1) [PASS]
I (6273) GPIO_TEST:   EX3 -> EX4 (expected: HIGH)
I (6283) GPIO_TEST:     PIN1: GPIO45->GPIO1 = 1 (ISR:1,T:1) [PASS]
I (6283) GPIO_TEST:     PIN2: GPIO48->GPIO2 = 1 (ISR:1,T:1) [PASS]
I (6293) GPIO_TEST:     PIN3: GPIO47->GPIO42 = 1 (ISR:1,T:1) [PASS]
I (6293) GPIO_TEST:     PIN4: GPIO9->GPIO41 = 1 (ISR:1,T:1) [PASS]
I (6303) GPIO_TEST:     PIN5: GPIO10->GPIO40 = 1 (ISR:1,T:1) [PASS]
I (6803) GPIO_TEST: [Phase 2] Setting all outputs LOW...
...
I (7353) GPIO_TEST: >>> Test #1: ALL PASS <<<
I (7353) GPIO_TEST: Statistics: Total=1, Pass=1, Fail=0
```

**출력 포맷 설명:**
- `ISR:val` - GPIO 인터럽트로 감지된 값
- `T:triggered` - 인터럽트 발생 여부 (1=발생, 0=미발생)
- `[PASS/FAIL]` - 기대값과 일치 여부

## 트러블슈팅

### 모든 GPIO 테스트 FAIL
```
E (XXX) GPIO_TEST:     PIN1: GPIO4->GPIO6 = 0 (ISR:0,T:0) [FAIL]
```

**해결 방법**:
- 랜케이블 연결 확인 (EX1↔EX2, EX3↔EX4)
- 랜케이블 단선 여부 확인
- 커넥터 접촉 상태 확인

### 특정 핀만 FAIL
```
I (XXX) GPIO_TEST:     PIN1: GPIO4->GPIO6 = 1 (ISR:1,T:1) [PASS]
E (XXX) GPIO_TEST:     PIN5: GPIO14->GPIO18 = 0 (ISR:0,T:0) [FAIL]
```

**해결 방법**:
- 해당 라인의 랜케이블 접촉 불량
- 해당 GPIO 핀의 하드웨어 문제 확인
- 다른 랜케이블로 교체 테스트

### WiFi 연결 실패 (4-way handshake timeout)
```
I (XXX) wifi:state: run -> init (0xfc0)
E (XXX) WIFI_MGR: Failed to connect to AP after 5 attempts
```

**에러 코드 의미:**
- `0xfc0`: 4-way handshake timeout (WPA/WPA2 인증 실패)
- `0x200`: Auth timeout
- `0x400`: Assoc timeout

**해결 방법**:
1. NVS 초기화: `idf.py erase-flash` 후 재플래시
2. 라우터에서 MAC 주소 차단 여부 확인
3. 비밀번호 확인 (menuconfig → WiFi Configuration)
4. 재시도 횟수 증가 (menuconfig → Maximum retry)

## 프로젝트 구조

```
IIS3DWB/
├── components/
│   ├── wifi_manager/         # WiFi 관리 컴포넌트
│   │   ├── wifi_manager.h    # WiFi API 선언
│   │   ├── wifi_manager.c    # WiFi 연결, 재연결 로직
│   │   └── CMakeLists.txt
│   ├── iis3dwb/              # IIS3DWB 센서 드라이버 (미사용)
│   └── tsl2591/              # TSL2591 센서 드라이버 (미사용)
├── main/
│   ├── main.c                # GPIO 테스트 메인 애플리케이션
│   ├── Kconfig.projbuild     # WiFi, GPIO 설정 메뉴
│   └── CMakeLists.txt
├── flash.sh                  # macOS용 자동 플래시 스크립트
├── CMakeLists.txt            # 프로젝트 최상위 빌드 설정
├── sdkconfig.defaults        # 기본 설정
├── CLAUDE.md                 # 개발자 가이드
└── README.md                 # 본 문서
```

## WiFi 연결 설정

### WiFi 타이밍 설정

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

**총 연결 타임아웃:** 60초

### WiFi 설정 변경

```bash
idf.py menuconfig
# → WiFi Configuration
#   - SSID: WiFi 네트워크 이름
#   - Password: WiFi 비밀번호
#   - Maximum retry: 재시도 횟수 (기본값: 5)
```

## 빌드 문제 해결

### 라즈베리파이에서 빌드 캐시 문제

다른 환경(Windows 등)에서 빌드한 프로젝트를 라즈베리파이로 가져온 경우:

```bash
# 프로젝트 디렉토리로 이동
cd ~/shinho/TSL2591

# build 디렉토리 삭제
rm -rf build

# ESP-IDF 환경 활성화
source ~/esp/v5.4.3/esp-idf/export.sh

# 타겟 재설정
idf.py set-target esp32s3

# 빌드
idf.py build

# 플래시
idf.py -p /dev/ttyACM0 flash monitor
```

### USB 권한 문제

```bash
# 사용자를 dialout 그룹에 추가 (이미 되어 있어야 함)
sudo usermod -a -G dialout $USER

# 로그아웃 후 다시 로그인 필요
```


## 참고 자료

- **ESP32-S3 기술 레퍼런스**: [Espressif Documentation](https://docs.espressif.com/projects/esp-idf/en/latest/esp32s3/)
- **ESP-IDF GPIO 드라이버**: [GPIO Driver Documentation](https://docs.espressif.com/projects/esp-idf/en/latest/api-reference/peripherals/gpio.html)
- **ESP-IDF WiFi 드라이버**: [WiFi Driver Documentation](https://docs.espressif.com/projects/esp-idf/en/latest/api-reference/network/esp_wifi.html)
- **ESP-IDF 프로그래밍 가이드**: [ESP-IDF Programming Guide](https://docs.espressif.com/projects/esp-idf/en/latest/)
