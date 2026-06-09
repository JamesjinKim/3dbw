# IIS3DWB 센서 설정 툴 (Python · 무설치)

이 폴더(`set_sensor_gui.py` + `nvs_gen.py`)만 복사해 가면 IIS3DWB 센서의
WiFi·서버·측정속도·읽기방식을 설정하고 디바이스에 영구 저장(NVS)할 수 있습니다.

**NVS 생성은 외부 도구(`nvs_partition_gen.py`)가 필요 없습니다** — `nvs_gen.py`가
순수 파이썬으로 NVS 바이너리를 직접 만듭니다(검증된 Rust 설정툴과 byte-exact 동일).
따라서 **전체 ESP-IDF 설치 없이도** 동작하며, 디바이스에 굽는 단계(`esptool`)만 있으면 됩니다.

> 📖 **그림과 함께 보는 사용 설명서:** `사용설명서.html` 을 브라우저로 열어보세요.
> (화면 미리보기, 단계별 사용법, 문제 해결을 한 페이지로 정리)

---

## 준비물 (받는 PC 기준)

- Python 3.7+ (표준 라이브러리 `tkinter` 사용 — 대부분 기본 포함)
- **esptool** — 둘 중 하나면 됨:
  - ESP-IDF 환경을 활성화했다면 이미 PATH 에 있음 (`source .../export.sh`), **또는**
  - `pip install esptool` 한 번 (ESP-IDF 전체 설치 불필요)
- (선택) `pyserial` — USB 포트 자동 인식·라벨에 사용. 없으면 `/dev` 스캔으로 폴백.
  `pip install pyserial`
- USB 케이블로 연결된 IIS3DWB 디바이스 (펌웨어가 이미 플래시된 상태)

> 펌웨어가 안 들어간 새 보드라면 먼저 펌웨어를 플래시해야 합니다.
> (펌웨어 저장소: https://github.com/JamesjinKim/3dbw — `idf.py flash`)

> 📁 `old/` 폴더는 **구버전**입니다(외부 `nvs_partition_gen.py` 의존). **무시하세요.**
> 최신은 최상위 `set_sensor_gui.py` + `nvs_gen.py` 입니다.

---

## 실행 방법

```bash
# esptool 이 PATH 에 없다면 둘 중 하나:
source ~/esp/v5.4.3/esp-idf/export.sh      # ESP-IDF 활성화 (경로는 환경에 맞게)
#   또는:  pip install esptool

# 설정 툴 실행
cd sensor-setup-py
python3 set_sensor_gui.py
```

창이 뜨면:
1. **USB 포트** 선택 (안 보이면 "포트 새로고침")
2. **WiFi 이름/비밀번호** 입력 (2.4GHz 전용)
3. **라즈베리파이 IP / 포트** 입력 — 수신기 화면에 표시된 IP (포트 기본 9000)
4. **측정 속도** 선택 (기본 3.3 kHz · 권장)
5. **데이터 읽기 방식** 선택 (기본 인터럽트 · 저부하)
6. **"설정 저장 & 디바이스에 주입"** 클릭

주입이 끝나면 디바이스를 재부팅(전원 재인가)하면 WiFi에 자동 연결되고
라즈베리파이로 데이터를 전송합니다.

---

## 여러 센서 연속 설정

- 입력값은 자동으로 기억됩니다 (`~/.iis3dwb_sensor_setup.json`).
- 다음 센서는 **포트만 바꿔** 바로 "주입" 누르면 됩니다.
- 같은 WiFi·같은 라즈베리파이로 여러 대를 빠르게 등록할 수 있습니다.

---

## 동작 확인 (선택)

주입 후 디바이스 시리얼 로그로 연결·전송을 확인할 수 있습니다:

```bash
idf.py -p /dev/ttyACM0 monitor      # 포트는 환경에 맞게 (종료: Ctrl+])
```

로그에서 다음을 확인:
- `WiFi Connected` + `IP : 192.168.0.xxx`
- `📡 센서 데이터 스트리밍 중! → (라즈베리파이IP):9000`
- `[스트리밍] 패킷=... 드롭=0 에러=0`

---

## 자주 묻는 질문 / 문제 해결

| 증상 | 해결 |
|------|------|
| `esptool` 관련 오류 / 못 찾음 | `pip install esptool`, 또는 ESP-IDF 활성화(`source .../export.sh`). NVS 생성엔 ESP-IDF 가 필요 없지만 **굽기(esptool)** 는 필요합니다. |
| 포트 목록이 비어 있음 | USB 케이블/포트 확인 후 "포트 새로고침". Linux 는 `dialout` 그룹 권한 필요. 포트 인식이 약하면 `pip install pyserial`. |
| `No serial data received` | esptool 이 디바이스와 통신하지 못함. ① 다른 프로그램(`idf.py monitor` 등)이 같은 포트를 점유 중인지 확인 후 닫기 ② **데이터 통신용 USB 케이블**인지 확인(충전 전용 케이블은 안 됨) ③ 케이블 다시 꽂고 "포트 새로고침". |
| Flash 주입 실패 (포트 사용 중) | `idf.py monitor` 등 시리얼을 점유한 다른 프로그램을 닫고 다시 시도. |
| 주입은 됐는데 라즈베리파이에 수신 안 됨 | **라즈베리파이 IP 불일치**가 가장 흔한 원인. `hostname -I` 로 현재 IP 확인 후 그 IP로 다시 주입. (고정 IP 권장) |

---

## 펌웨어와 일치하는 NVS 규격 (참고 · 수정 금지)

| 항목 | 값 |
|------|-----|
| NVS namespace | `devcfg` |
| NVS 파티션 offset / size | `0x9000` / `0x6000` (24576 B) |
| 키 순서 | `wifi_ssid`, `wifi_pass`, `srv_ip`, `srv_port`(u16), `stream_rate`(u8), `transport`(u8), `read_mode`(u8) |

> 이 규격은 펌웨어 및 기존 Tauri 설정툴(`config-tool/src-tauri/src/nvs.rs`)과 동일합니다.
> `nvs_gen.py` 는 그 Rust 구현을 파이썬으로 1:1 포팅한 것으로, 검증된 기준 바이너리
> (`config-tool/src-tauri/tests_ref_full_nvs.bin`)와 **byte-exact 일치**가 확인됐습니다.
> ESP-IDF `nvs_partition_gen.py` 결과와도 동일 구조라, 외부 도구 없이 바로 굽기만 하면 됩니다.

---

## ⚠️ 보안 주의

- 입력값 기억 파일(`~/.iis3dwb_sensor_setup.json`)에 **WiFi 비밀번호가 평문**으로 저장됩니다.
  여러 센서 연속 설정 편의를 위한 것이며, **이 PC 안에만** 저장됩니다.
- 공용 PC에서 작업했다면 작업 후 이 파일을 삭제하세요:
  ```bash
  rm ~/.iis3dwb_sensor_setup.json
  ```
