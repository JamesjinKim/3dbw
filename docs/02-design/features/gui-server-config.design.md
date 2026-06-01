# Design: GUI 서버 설정 + 송수신 확인

- **Feature**: gui-server-config
- **Plan**: [[gui-server-config.plan]]
- **작성일**: 2026-06-01
- **결정사항**: 송수신 확인 = **디바이스 시리얼 로그 확인** 방식

## 1. 아키텍처 개요

```
[UI 위저드]                  [Rust 백엔드(lib.rs)]            [디바이스]
 ① 디바이스 연결  ──detect_device──────────────────────────▶ chip 확인
 ② WiFi+서버 입력 ──write_config(ssid,pw,ip,port,rate)──────▶ NVS 기록(0x9000)
                     └ nvs::generate_full_nvs()             └ 재부팅→스트리밍
 ③ 송수신 확인    ──verify_streaming(port)──시리얼 read──────▶ "스트리밍 시작" 로그
 ④ 완료/종료      (UI만)
```

## 2. NVS 생성기 확장 (nvs.rs) — FR-1

### 2.1 추가할 엔트리 타입
현재: `add_namespace`(TYPE_U8=0x01), `add_string`(TYPE_STR=0x21)
추가:
- `TYPE_U8 = 0x01` (이미 상수 존재) → `add_u8(key, val)`
- `TYPE_U16 = 0x02` → `add_u16(key, val)`

### 2.2 NVS 엔트리 포맷 (32바이트, primitive 타입)
```
[0]      NsIndex (= NS_INDEX = 1)
[1]      Type (0x01=u8, 0x02=u16)
[2]      Span (= 1, primitive는 항상 1엔트리)
[3]      ChunkIndex (= 0xFF)
[4..8]   CRC32 (entry[8..32] 대상, nvs_crc)
[8..24]  Key (널 종료 문자열, 최대 15자 + \0)
[24..32] Data (8바이트, little-endian 값 + 나머지 0)
```
> ⚠️ primitive 값은 [24..32] 8바이트에 LE로 저장. u8은 1바이트+나머지 0, u16은 2바이트+나머지 0.
> string과 달리 별도 데이터 엔트리 없음(span=1).

### 2.3 엔트리 작성 순서 (nvs_partition_gen.py 와 일치)
```
namespace(devcfg)
wifi_ssid (string)
wifi_pass (string)
srv_ip    (string)
srv_port  (u16)
stream_rate (u8)
transport (u8)
```
CSV 입력 순서대로 엔트리가 직렬화됨 → 동일 순서 유지.

### 2.4 신규 함수
```rust
pub fn generate_full_nvs(
    namespace: &str,
    ssid: &str, password: &str,
    srv_ip: &str, srv_port: u16,
    stream_rate: u8, transport: u8,
    partition_size: usize,
) -> Result<Vec<u8>, String>
```
- 기존 `generate_wifi_nvs`는 유지(하위호환·기존 테스트).
- 내부적으로 page에 7개 엔트리 추가 후 serialize.

### 2.5 검증 테스트
- 기존 `matches_reference_bin` (WiFi 전용) 유지.
- 신규 `matches_full_reference_bin`: `generate_full_nvs(...)` 결과를 ESP-IDF `nvs_partition_gen.py`로 만든 `tests_ref_full_nvs.bin`과 byte-exact 비교.
- reference bin은 더미값(example2.4G/example1234/192.168.0.37/9000/0/0)으로 생성.

## 3. write_config 커맨드 (lib.rs) — FR-2

```rust
#[tauri::command]
fn write_config(port: String, ssid: String, password: String,
                server_ip: String, server_port: u16,
                rate_step: u8) -> Result<WriteResult, String>
```
- `nvs::generate_full_nvs(NS, ssid, pw, server_ip, server_port, rate_step, 0, NVS_SIZE)`
- espflash로 0x9000에 기록 (기존 write_wifi의 flash 로직 재사용).
- transport=0(UDP) 고정.
- 기존 `write_wifi`는 제거하거나 내부적으로 write_config 호출로 대체.

## 4. verify_streaming 커맨드 (lib.rs) — FR-4

```rust
#[tauri::command]
fn verify_streaming(port: String, timeout_secs: u64) -> Result<StreamCheck, String>
```
- 동작: 시리얼 포트를 열고(DTR/RTS 조작으로 리셋 유발 가능) 최대 timeout 동안 라인 읽기.
- **성공 판정**: 로그에 다음 중 하나 등장 시
  - `"스트리밍 시작"` 또는 `"STREAMER: 스트리밍 시작"`
  - `"[스트리밍] 패킷="` 뒤 숫자 > 0
- **실패 판정**: timeout 동안 위 로그 없음 + `"스트리밍 비활성"` 등장 → 설정 누락.
- 반환:
```rust
struct StreamCheck { streaming: bool, rate_hz: u32, server: String, reason: String }
```
- 포트 경합: 확인 시에만 open, 즉시 close. (GUI write_config 직후 호출)

## 5. UI 변경 — FR-3, FR-5

### 5.1 ②단계: WiFi 입력 화면에 서버 필드 추가
```
WiFi 이름 (SSID)        [____________]
비밀번호                [____________]
─────────────────────────────────
라즈베리파이 IP          [192.168.0.__]   ← 신규
포트                    [9000]            ← 신규(기본값)
샘플레이트              [0 — 1 kHz ▼]     ← 신규(드롭다운 5단계)
[ 저장 & 연결 확인 ]
```

### 5.2 ③단계: 송수신 확인 (신규 단계)
- "저장 & 연결 확인" → write_config 성공 후 자동으로 verify_streaming 호출.
- 화면: 스피너 + "데이터 송수신 확인 중… (최대 40초)"
- 결과:
  - 성공 → "✓ 데이터 송수신 확인됨 (192.168.0.37:9000, 1 kHz)" → ④로
  - 실패 → "⚠ 전송이 시작되지 않았습니다" + 재시도 버튼

### 5.3 ④단계: 완료/종료
- 기존 완료 화면 + 서버 정보 행 추가(서버 IP:포트, 샘플레이트).
- 버튼: "다른 디바이스 설정"(처음으로) — 종료는 사용자가 창 닫기.
- 완료 문구: "설정이 끝났습니다. USB를 분리해도 전원만 넣으면 자동으로 데이터를 전송합니다."

## 6. 단계별 상태 머신 (main.js)
```
STEP_CONNECT(①) → STEP_INPUT(②) → STEP_VERIFY(③) → STEP_DONE(④)
                                      │ 실패
                                      └→ STEP_INPUT (재시도)
```

## 7. 구현 순서 (Do)
1. nvs.rs: add_u8 / add_u16 + generate_full_nvs + 단위테스트
2. ESP-IDF로 reference full bin 생성 → 테스트 통과 확인
3. lib.rs: write_config + verify_streaming 커맨드
4. UI: ② 서버 필드, ③ 확인 단계, ④ 완료 문구
5. 실기기 검증: GUI만으로 전체 흐름 → 라즈베리파이 수신 확인

## 8. 검증 기준 (Check)
- nvs 단위테스트 2종 통과 (wifi-only + full)
- GUI 저장 후 디바이스 NVS에 6키 기록 (덤프 확인)
- verify_streaming 이 "스트리밍 시작" 감지
- 라즈베리파이 수신 (수동 주입 0회)
