# IIS3DWB 진동센서 시스템

ESP32-S3 + IIS3DWB 초광대역 진동센서로 최대 **26.6 kHz** 진동을 수집하는 시스템입니다.
센서는 USB 직결(시리얼) 또는 WiFi(UDP)로 라즈베리파이에 데이터를 보냅니다.

이 저장소는 **라즈베리파이 4 (Raspberry Pi OS, arm64)** 에서 개발·운영합니다.

---

## 하는 일은 셋입니다

```
[준비 공정 RPi]                                   [현장 RPi]
 ① boardcheck      보드 진단 (PASS/FAIL)
 ② sensor-setup-py 펌웨어 굽기 + 설정 주입
          │                                        ③ rpi-collector
          └──── 설정 끝난 센서를 현장으로 ───────────→    데이터 수집
```

### ① 보드 진단 — `boardcheck/`

새 보드를 받았거나 하드웨어를 바꿨을 때, **소프트웨어 작업에 들어가기 전에** 먼저 돌립니다.

```bash
cd boardcheck
./run.sh              # 꽂고 한 줄. 20초 안에 6개 항목 판정
./run.sh --loop       # 보드를 갈아 끼우며 연속 검사
```

MCU · SPI · 가속도 출력 · FIFO/ODR · **INT1 전기상태** · INT 발생률을 검사하고
PASS/FAIL 과 조치 안내를 냅니다. 종료코드 `0`=PASS `1`=WARN `2`=FAIL `3`=실행오류.

ESP-IDF 없이 돌아갑니다 (`dist/` 에 구울 펌웨어가 들어 있습니다).
핀맵을 바꿨다면 `./build.sh` 로 다시 빌드하세요.

### ② 펌웨어 굽기 + 설정 — `sensor-setup-py/` · **SHT 진동센서 설정**

```bash
python3 sensor-setup-py/set_sensor_gui.py      # 개발 트리에서 바로 실행
```

한 창에서 **① 펌웨어 굽기 → ② 설정 주입**까지 끝납니다.
WiFi(UDP)와 USB 직결 중 전송 방식을 고르고, 측정 속도·범위·읽기 방식을 정합니다.
주입 후 부팅 로그에서 **펌웨어가 되울린 설정을 입력값과 대조**해 반영을 확인합니다.

배포 패키지는 이렇게 만듭니다. 결과는 `dist/` — 이 폴더를 zip 으로 묶어 배포하고,
사용자는 풀어서 `bash run.sh` 하나로 실행합니다.

```bash
./tools/build-deploy.sh                          # 배포 빌드 (NVS 우선)
./tools/make-deploy-package.sh --version 1.0.0   # → dist/iis3dwb-setup-1.0.0/
```

> 개발 중 빠르게 굽고 로그를 보려면 `./dev-flash.sh` 를 씁니다.
> 다만 이것은 **개발 빌드**라 NVS 의 WiFi 설정을 무시하고 시리얼 속도도 다릅니다 —
> 출하에는 쓰지 마세요.

### ③ 데이터 수집 — `rpi-collector/` · **SHT 진동센서 수집**

포토센서에 불이 들어오면 짝지어진 진동센서만 정해진 시간만큼 기록하고 저장합니다.
**진동센서 1대마다 포토센서 1개**가 짝을 이루며 각 짝은 독립 동작합니다.

```bash
python3 rpi-collector/collector_gui.py              # 수집기 화면 (개발 트리)
python3 rpi-collector/collect_cli.py --auto         # 화면 없는 RPi 용 글자 화면
./tools/make-collector-package.sh --version 1.0.0   # → dist-collector/
```

사용법과 **데이터 분석 시 주의사항**은 도움말에 있습니다.

```bash
python3 rpi-collector/helpdoc.py     # 브라우저로 열림 (인터넷 불필요)
```

---

## 처음 설치 (개발 RPi)

```bash
./setup-rpi.sh        # ESP-IDF v5.4.3 + 의존 패키지 + 포트 권한
```

현장 RPi 에는 아무것도 설치하지 않습니다. 배포 zip 을 풀어 `bash run.sh` 하면
패키지 안의 라이브러리(`vendor/`)로 돌고, 필요한 USB 권한은 처음 실행 때 스스로 설정합니다.

---

## 꼭 알아둘 것

**측정 단위는 이미 환산돼 있습니다.** CSV 의 `_mg` 열을 그대로 쓰면 됩니다.
측정 범위(±2/4/8/16 g)는 패킷 헤더에 실려 오므로 수집기가 자동으로 맞춥니다.

**주파수 분석(FFT)이 목적이라면 26.6 kHz 로 수집하세요.** 낮은 설정은 펌웨어가
저역통과 필터 없이 샘플을 솎아내는 방식이라, 높은 주파수 성분이 **낮은 주파수 자리로
접혀 들어옵니다**(에일리어싱). 실측 근거는 도움말 5장에 있습니다.
RMS·피크 같은 전체 진동량만 본다면 낮은 레이트도 정확합니다.

**센서 구분은 USB 구멍으로 합니다.** 브리지칩에 고유 일련번호가 없어 다른 방법이
없습니다. `slots.py --make-udev` 로 만든 `/dev/iis3dwb1`, `/dev/iis3dwb2` 는
재부팅해도 바뀌지 않습니다. 케이블을 다른 구멍에 옮기면 번호가 바뀝니다.

---

## 폴더 구성

| 폴더 | 내용 |
|---|---|
| `main/` `components/` | 펌웨어 소스 (ESP-IDF) |
| `boardcheck/` | ① 보드 진단 — 독립 ESP-IDF 프로젝트 |
| `sensor-setup-py/` | ② SHT 진동센서 설정 — 펌웨어 굽기 + 설정 주입 (tkinter) |
| `rpi-collector/` | ③ SHT 진동센서 수집 — 수집기 화면 (tkinter) + 글자 화면 + UDP 수신기 |
| `tools/` | 배포 빌드·패키징·USB 복구 스크립트 |
| `deploy/` | 두 배포 패키지 공용 `run.sh`·`install.sh`·udev 규칙 |
| `vendor/` | 폐쇄망 배포용 파이썬 라이브러리 원본 (해시 고정) |
| `dev-flash.sh` | **개발용** 빌드·굽기·모니터 — 출하에 쓰지 말 것 |
| `docs/` | 패킷 규격 · 시리얼 전송 설계 · FIFO 인터럽트 · 데이터시트 |

센서 핀맵은 **`components/iis3dwb/Kconfig` 한 곳**에만 있습니다.
펌웨어와 `boardcheck` 가 같은 정의를 공유하므로, 하드웨어가 바뀌면 여기만 고칩니다.

---

## 사양

| 항목 | 값 |
|---|---|
| 센서 | IIS3DWB 3축, SPI Mode 3 |
| 출력 속도(ODR) | 26.667 kHz **고정** (낮은 설정은 솎아내기로 구현) |
| 측정 범위 | ±2 / ±4 / ±8 / ±16 g |
| 전송 | USB 시리얼 2,000,000 bps · WiFi UDP · 프로토콜 v2 |
| 실측 처리량 | 26,733 Hz · 유실 0.00% |
| 타겟 | ESP32-S3 (QFN56, 8MB PSRAM) · ESP-IDF v5.4.3 |
