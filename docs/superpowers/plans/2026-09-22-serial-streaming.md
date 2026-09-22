# USB 시리얼 직결 전송 + 공장 초기화 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** WiFi 대신 USB 케이블로 PC에 진동 데이터를 직접 전송하는 경로를 추가하고, GUI에 측정 범위 선택과 공장 초기화 기능을 넣는다.

**Architecture:** `sensor_streamer` 의 패킷 조립부는 그대로 두고 전송 수단만 분기시킨다(`tx_send()`). 패킷 헤더를 v2(18B)로 확장해 풀스케일 정보를 담아, 수신 측이 스케일을 가정하지 않아도 되게 한다. 풀스케일은 빌드타임 상수에서 NVS 설정으로 옮겨 GUI에서 선택한다. 전송 방식은 WiFi/USB 배타 선택이며, 시리얼 모드에서는 WiFi 초기화를 건너뛴다.

**Tech Stack:** ESP-IDF v5.4.3 (C), Tauri 2 (Rust), Vanilla JS

**Spec:** `docs/2026-09-22-serial-streaming-design.md`

## Global Constraints

- **NVS 네임스페이스**: `devcfg` — 펌웨어 `config_manager.c` 와 정확히 일치해야 함
- **NVS 파티션**: 오프셋 `0x9000`, 크기 `0x6000` (24K)
- **패킷 magic**: `0x49495333` ("IIS3")
- **패킷 프로토콜 버전**: v2 (`STREAM_PROTO_VER = 2`), 헤더 18바이트
- **패킷당 샘플 수**: 200 (`STREAM_SAMPLES_PER_PACKET`), 총 패킷 1,218 B
- **`full_scale_g` 저장 형식**: 사람이 읽는 값 `2/4/8/16` (레지스터 코드값 아님)
- **풀스케일 레지스터 코드**: ±2g=`0x00`, ±4g=`0x08`, ±8g=`0x0C`, ±16g=`0x04` (순서가 직관과 어긋남 — 주의)
- **감도(mg/LSB)**: ±2g=0.061, ±4g=0.122, ±8g=0.244, ±16g=0.488
- **rate_step → Hz**: `{0:1000, 1:3333, 2:6667, 3:13333, 4:26667}`
- **기본값**: 풀스케일 ±4g, rate_step 1(3.3kHz), read_mode 1(인터럽트), 포트 9000
- **USB TX 버퍼**: 8192 B (ESP-IDF 기본 256 B는 패킷보다 작아 블로킹 발생 — 반드시 변경)
- **빌드 환경**: 펌웨어(Task 6~9)는 이 macOS에서 빌드 불가 → 라즈베리파이에서 검증. GUI(Task 1~5)는 macOS에서 빌드·테스트 가능

---

## 파일 구조

| 파일 | 책임 | 작업 |
|------|------|------|
| `config-tool/src-tauri/src/nvs.rs` | NVS 바이너리 생성 | 수정 — `full_scale_g` 키 추가, 시리얼 모드 IP 검증 완화 |
| `config-tool/src-tauri/src/lib.rs` | Tauri 명령 | 수정 — `transport`/`full_scale_g` 파라미터, `factory_reset`, `verify_serial_stream` |
| `config-tool/ui/index.html` | 화면 구조 | 수정 — 전송 방식·측정 범위 선택, 공장 초기화 버튼 |
| `config-tool/ui/main.js` | 화면 로직 | 수정 — 조건부 입력란, 초기화 확인, prefs 확장 |
| `components/sensor_streamer/sensor_streamer.h` | 스트리머 공개 API | 수정 — `STREAM_TRANSPORT_SERIAL`, v2 헤더, `full_scale_g` |
| `components/sensor_streamer/sensor_streamer.c` | 스트리밍 구현 | 수정 — `tx_send()` 분기, USB 드라이버, 로그 차단 |
| `components/config_manager/config_manager.h` | 설정 구조체 | 수정 — `full_scale_g` 필드 |
| `components/config_manager/config_manager.c` | NVS 저장/로드 | 수정 — `full_scale_g` 키, 폴백 |
| `main/main.c` | 부팅 흐름 | 수정 — 전송 방식 분기, 3초 창, NVS 풀스케일 적용 |

**작업 순서**: GUI(Task 1~5) → 펌웨어(Task 6~9) → 통합 검증(Task 10)

---

## Task 1: NVS 생성기에 full_scale_g 추가

**Files:**
- Modify: `config-tool/src-tauri/src/nvs.rs:264-306` (`generate_full_nvs`)
- Test: `config-tool/src-tauri/src/nvs.rs` 내 `mod tests`

**Interfaces:**
- Consumes: 기존 `NvsPage::add_u8(key, val)`, `add_string`, `add_u16`, `add_namespace`, `serialize()`
- Produces: `generate_full_nvs(namespace, ssid, password, srv_ip, srv_port, stream_rate, transport, read_mode, full_scale_g, partition_size) -> Result<Vec<u8>, String>` — 인자 1개 추가(`full_scale_g: u8`, `read_mode` 뒤)

**배경:** 기존 테스트 `matches_full_reference_bin` 은 ESP-IDF `nvs_partition_gen.py` 가 만든 `tests_ref_full_nvs.bin` 과 바이트 단위로 비교한다. 키를 추가하면 이 기준 파일이 더 이상 맞지 않는다. 기준 bin 재생성은 ESP-IDF가 필요해 **Task 10(라즈베리파이)에서 수행**한다. 그때까지 기존 테스트는 구형 시그니처를 검증하도록 유지하고, 신규 키는 구조 검증 테스트로 커버한다.

- [ ] **Step 1: 실패하는 테스트 작성**

`config-tool/src-tauri/src/nvs.rs` 의 `mod tests` 안에 추가:

```rust
    /// full_scale_g 키가 NVS에 기록되는지 구조 검증
    /// (바이트 단위 기준 bin 비교는 Task 10에서 ESP-IDF로 재생성 후 추가)
    #[test]
    fn writes_full_scale_key() {
        let bin = generate_full_nvs(
            "devcfg", "testssid", "testpass", "192.168.0.37",
            9000, 1, 0, 1, 4, 0x6000,
        )
        .expect("생성 실패");

        // 키 문자열이 엔트리 영역에 존재하는지 확인
        let key_bytes = b"full_scale_g\0";
        let found = bin
            .windows(key_bytes.len())
            .any(|w| w == key_bytes);
        assert!(found, "full_scale_g 키를 찾을 수 없음");
    }

    /// 시리얼 모드(transport=2)는 서버 IP가 비어 있어도 생성되어야 한다
    #[test]
    fn serial_mode_allows_empty_ip() {
        let result = generate_full_nvs(
            "devcfg", "testssid", "testpass", "",
            9000, 1, 2, 1, 4, 0x6000,
        );
        assert!(result.is_ok(), "시리얼 모드에서 빈 IP가 거부됨: {:?}", result.err());
    }

    /// WiFi 모드(transport=0)는 서버 IP가 필수
    #[test]
    fn wifi_mode_requires_ip() {
        let result = generate_full_nvs(
            "devcfg", "testssid", "testpass", "",
            9000, 1, 0, 1, 4, 0x6000,
        );
        assert!(result.is_err(), "WiFi 모드에서 빈 IP가 허용됨");
    }
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `cd config-tool && cargo test --manifest-path src-tauri/Cargo.toml --lib`
Expected: FAIL — `generate_full_nvs` 의 인자 개수 불일치로 컴파일 에러

- [ ] **Step 3: 구현**

`nvs.rs:264` 의 시그니처에 인자를 추가하고, IP 검증을 transport에 따라 분기한다:

```rust
pub fn generate_full_nvs(
    namespace: &str,
    ssid: &str,
    password: &str,
    srv_ip: &str,
    srv_port: u16,
    stream_rate: u8,
    transport: u8,
    read_mode: u8,
    full_scale_g: u8,
    partition_size: usize,
) -> Result<Vec<u8>, String> {
```

IP 검증 블록(`nvs.rs:287-289`)을 아래로 교체:

```rust
    // 시리얼 모드(transport=2)는 서버 IP를 쓰지 않으므로 빈 값을 허용한다.
    // 길이 상한은 모드와 무관하게 적용한다(NVS 필드 크기 제약).
    if srv_ip.len() > 15 {
        return Err("서버 IP 형식이 올바르지 않습니다".into());
    }
    if transport != 2 && srv_ip.is_empty() {
        return Err("서버 IP 형식이 올바르지 않습니다".into());
    }
```

풀스케일 값 검증을 SSID 검증 뒤에 추가:

```rust
    if ![2u8, 4, 8, 16].contains(&full_scale_g) {
        return Err("측정 범위는 2/4/8/16 중 하나여야 합니다".into());
    }
```

엔트리 작성부(`page.add_u8("read_mode", read_mode);` 다음 줄)에 추가:

```rust
    page.add_u8("full_scale_g", full_scale_g);
```

- [ ] **Step 4: 기존 호출부 수정**

`lib.rs` 의 `write_config` 내 `generate_full_nvs` 호출에 인자를 추가한다(Task 2에서 파라미터로 받도록 바꾸므로, 여기서는 일단 `4` 를 상수로 전달):

`config-tool/src-tauri/src/lib.rs:366-376` 의 호출을 다음으로 교체:

```rust
    let nvs_bin = nvs::generate_full_nvs(
        NVS_NAMESPACE,
        &ssid,
        &password,
        &server_ip,
        server_port,
        rate_step,
        0,
        read_mode,
        4,
        NVS_SIZE,
    )?;
```

기존 테스트 `matches_full_reference_bin` 의 호출에도 `4,` 를 `read_mode` 인자 뒤에 추가한다. **이 테스트는 이번 변경으로 실패하게 되므로 `#[ignore]` 를 붙이고 사유를 남긴다:**

```rust
    /// WiFi + 서버설정 전체 NVS — ESP-IDF 기준 bin과 byte-exact 비교
    ///
    /// full_scale_g 키 추가로 기준 bin이 낡았다. Task 10에서 라즈베리파이의
    /// nvs_partition_gen.py 로 재생성한 뒤 ignore를 제거한다.
    #[ignore = "기준 bin 재생성 대기 (Task 10)"]
    #[test]
    fn matches_full_reference_bin() {
```

- [ ] **Step 5: 테스트 통과 확인**

Run: `cd config-tool && cargo test --manifest-path src-tauri/Cargo.toml --lib`
Expected: PASS — `writes_full_scale_key`, `serial_mode_allows_empty_ip`, `wifi_mode_requires_ip`, `matches_reference_bin` 통과. `matches_full_reference_bin` 은 ignored 1건으로 표시

- [ ] **Step 6: 커밋**

```bash
cd /Users/kimkookjin/Projects/ESP-IDF/IIS3DWB
git add config-tool/src-tauri/src/nvs.rs config-tool/src-tauri/src/lib.rs
git commit -m "$(cat <<'EOF'
NVS 생성기: full_scale_g 키 추가 + 시리얼 모드 IP 검증 완화

시리얼 전송(transport=2)은 서버 IP를 쓰지 않으므로 빈 값을 허용한다.
기준 bin 비교 테스트는 재생성 전까지 ignore 처리.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: write_config 에 transport / full_scale_g 파라미터 추가

**Files:**
- Modify: `config-tool/src-tauri/src/lib.rs:355-440` (`write_config`)

**Interfaces:**
- Consumes: Task 1의 `generate_full_nvs(..., full_scale_g, partition_size)`
- Produces: Tauri 명령 `write_config(port, ssid, password, serverIp, serverPort, rateStep, readMode, transport, fullScaleG) -> Result<WriteResult, String>` — JS에서 camelCase로 호출

- [ ] **Step 1: 시그니처 수정**

`lib.rs:356-364` 의 함수 시그니처를 교체:

```rust
#[tauri::command]
fn write_config(
    port: String,
    ssid: String,
    password: String,
    server_ip: String,
    server_port: u16,
    rate_step: u8,
    read_mode: u8,
    transport: u8,
    full_scale_g: u8,
) -> Result<WriteResult, String> {
```

- [ ] **Step 2: NVS 생성 호출 수정**

Task 1 Step 4에서 상수로 넣었던 `0`(transport)과 `4`(full_scale_g)를 파라미터로 교체:

```rust
    let nvs_bin = nvs::generate_full_nvs(
        NVS_NAMESPACE,
        &ssid,
        &password,
        &server_ip,
        server_port,
        rate_step,
        transport,
        read_mode,
        full_scale_g,
        NVS_SIZE,
    )?;
```

- [ ] **Step 3: 컴파일 확인**

Run: `cd config-tool && cargo build --manifest-path src-tauri/Cargo.toml`
Expected: 성공

- [ ] **Step 4: 커밋**

```bash
cd /Users/kimkookjin/Projects/ESP-IDF/IIS3DWB
git add config-tool/src-tauri/src/lib.rs
git commit -m "$(cat <<'EOF'
write_config: transport / full_scale_g 파라미터 추가

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: 공장 초기화 명령 (factory_reset)

**Files:**
- Modify: `config-tool/src-tauri/src/lib.rs` (신규 명령 추가 + `invoke_handler` 등록)
- Modify: `config-tool/src-tauri/src/nvs.rs` (빈 NVS 생성 함수)

**Interfaces:**
- Consumes: 기존 `connect_flasher(port) -> Result<Flasher, String>`, `NVS_OFFSET`, `NVS_SIZE`
- Produces: `nvs::generate_empty_nvs(partition_size) -> Result<Vec<u8>, String>`, Tauri 명령 `factory_reset(port) -> Result<String, String>`

**설계 근거:** 펌웨어의 `factory_reset` JSON 명령이 아니라 빈 NVS 주입 방식을 쓴다. 이미 검증된 경로를 재사용하고, 디바이스가 시리얼 스트리밍으로 포트를 점유한 상태에서도 확실히 동작하기 때문이다.

- [ ] **Step 1: 실패하는 테스트 작성**

`nvs.rs` 의 `mod tests` 에 추가:

```rust
    /// 빈 NVS는 전체가 0xFF (지워진 플래시 상태)여야 한다
    #[test]
    fn empty_nvs_is_all_erased() {
        let bin = generate_empty_nvs(0x6000).expect("생성 실패");
        assert_eq!(bin.len(), 0x6000, "크기 불일치");
        assert!(bin.iter().all(|&b| b == 0xFF), "0xFF가 아닌 바이트가 있음");
    }

    /// 파티션 크기 검증은 유지되어야 한다
    #[test]
    fn empty_nvs_rejects_bad_size() {
        assert!(generate_empty_nvs(100).is_err(), "잘못된 크기가 허용됨");
    }
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `cd config-tool && cargo test --manifest-path src-tauri/Cargo.toml --lib`
Expected: FAIL — `generate_empty_nvs` 미정의로 컴파일 에러

- [ ] **Step 3: generate_empty_nvs 구현**

`nvs.rs` 의 `generate_full_nvs` 함수 바로 뒤에 추가:

```rust
/// 공장 초기화용 빈 NVS 파티션 생성.
///
/// 전체를 0xFF(지워진 플래시)로 채운다. 디바이스는 이를 "설정 없음"으로
/// 인식해 첫 부팅 상태로 돌아간다. 펌웨어는 건드리지 않는다.
pub fn generate_empty_nvs(partition_size: usize) -> Result<Vec<u8>, String> {
    if partition_size < PAGE_SIZE * 2 {
        return Err("NVS 파티션 크기가 너무 작습니다 (최소 8KB)".into());
    }
    if partition_size % PAGE_SIZE != 0 {
        return Err("NVS 파티션 크기는 4096의 배수여야 합니다".into());
    }
    Ok(vec![0xFFu8; partition_size])
}
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `cd config-tool && cargo test --manifest-path src-tauri/Cargo.toml --lib`
Expected: PASS — `empty_nvs_is_all_erased`, `empty_nvs_rejects_bad_size` 통과

- [ ] **Step 5: Tauri 명령 추가**

`lib.rs` 의 `flash_firmware` 함수 앞에 추가:

```rust
/// 공장 초기화 — NVS 파티션을 지워 설정을 모두 삭제한다.
/// 펌웨어(앱/부트로더)는 건드리지 않으므로 바로 재설정할 수 있다.
#[tauri::command]
fn factory_reset(port: String) -> Result<String, String> {
    let empty = nvs::generate_empty_nvs(NVS_SIZE)?;

    let mut flasher = connect_flasher(&port)?;
    flasher
        .write_bin_to_flash(NVS_OFFSET, &empty, None)
        .map_err(|e| format!("NVS 초기화 실패: {}", e))?;

    Ok("설정이 초기화되었습니다.".to_string())
}
```

> `write_bin_to_flash` 의 인자 형태는 기존 `write_config` 내 호출부(`lib.rs:378` 부근)를 그대로 따른다. 실제 시그니처가 다르면 그쪽 코드를 복사해 맞춘다.

- [ ] **Step 6: invoke_handler 등록**

`lib.rs:473-481` 의 `generate_handler!` 목록에 `factory_reset` 추가:

```rust
        .invoke_handler(tauri::generate_handler![
            product_info,
            list_ports,
            detect_device,
            flash_firmware,
            write_wifi,
            write_config,
            factory_reset
        ])
```

- [ ] **Step 7: 빌드 확인**

Run: `cd config-tool && cargo build --manifest-path src-tauri/Cargo.toml`
Expected: 성공

- [ ] **Step 8: 커밋**

```bash
cd /Users/kimkookjin/Projects/ESP-IDF/IIS3DWB
git add config-tool/src-tauri/src/nvs.rs config-tool/src-tauri/src/lib.rs
git commit -m "$(cat <<'EOF'
공장 초기화: 빈 NVS 주입 방식으로 설정만 삭제

펌웨어 JSON 명령 대신 검증된 NVS 주입 경로를 재사용한다.
시리얼 스트리밍으로 포트가 점유된 상태에서도 동작한다.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: GUI — 전송 방식 · 측정 범위 선택 UI

**Files:**
- Modify: `config-tool/ui/index.html:40-95` (2단계 화면)
- Modify: `config-tool/ui/main.js:16-70` (prefs), `main.js:236-252` (저장 호출)

**Interfaces:**
- Consumes: Task 2의 `write_config(..., transport, fullScaleG)`
- Produces: DOM 요소 id — `transport-wifi`, `transport-usb`(라디오), `wifi-fields`(WiFi 전용 영역), `full-scale`(select), `usb-note`(안내)

- [ ] **Step 1: HTML — 전송 방식 선택 추가**

`ui/index.html` 의 2단계 `<h1>WiFi 정보 입력</h1>` 아래 `<p class="sub">` 다음에 삽입:

```html
      <div class="field">
        <label>데이터 전송 방식</label>
        <label class="radio-row">
          <input type="radio" name="transport" id="transport-wifi" value="0" checked />
          <span><b>WiFi 무선 전송</b><br>
            <small>센서가 WiFi에 접속해 서버로 전송합니다.</small></span>
        </label>
        <label class="radio-row">
          <input type="radio" name="transport" id="transport-usb" value="2" />
          <span><b>USB 직결 전송</b><br>
            <small>케이블로 PC에 직접 전송합니다 (WiFi 불필요).</small></span>
        </label>
      </div>

      <div id="usb-note" class="hint-box hidden">
        💡 USB 케이블로 연결된 PC가 데이터를 받습니다. 최대 26.6 kHz까지 사용 가능합니다.<br>
        ⚠️ 전송 중에는 디바이스 로그가 표시되지 않습니다.
      </div>
```

- [ ] **Step 2: HTML — WiFi 전용 영역 감싸기**

WiFi SSID `<div class="field">` 부터 포트 `<div class="field">` 까지(즉 `<label>WiFi 이름 (SSID)</label>` 이 있는 field부터 `<input type="number" id="srv-port" ...>` 가 있는 field까지)를 다음으로 감싼다:

```html
      <div id="wifi-fields">
        <!-- 기존 WiFi SSID / 비밀번호 / 구분선 / 라즈베리파이 IP / 포트 field 들을 여기 그대로 둔다 -->
      </div>
```

- [ ] **Step 3: HTML — 측정 범위 선택 추가**

`<label>데이터 읽기 방식</label>` 이 있는 field **뒤에** 삽입:

```html
      <div class="field">
        <label>측정 범위</label>
        <select id="full-scale">
          <option value="2">±2g · 정밀 측정 (미세 진동)</option>
          <option value="4" selected>±4g · 일반 모니터링 (기본·권장)</option>
          <option value="8">±8g · 강한 진동</option>
          <option value="16">±16g · 충격·낙하 측정</option>
        </select>
        <span class="hint">측정 범위를 넘는 진동은 잘려서 기록됩니다(클리핑). 범위가 클수록 큰 진동을 측정할 수 있지만 미세한 변화의 분해능은 낮아집니다.</span>
      </div>
```

- [ ] **Step 4: CSS 추가**

`ui/style.css` 끝에 추가:

```css
/* 전송 방식 라디오 */
.radio-row {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 10px 12px;
  margin-bottom: 6px;
  border: 1px solid var(--border, #d0d0d0);
  border-radius: 8px;
  cursor: pointer;
}
.radio-row input { margin-top: 3px; }
.radio-row small { color: var(--muted, #888); }

/* USB 안내 박스 */
.hint-box {
  padding: 10px 12px;
  margin-bottom: 14px;
  background: rgba(100, 150, 255, 0.08);
  border-left: 3px solid var(--accent, #4a90d9);
  border-radius: 4px;
  font-size: 13px;
  line-height: 1.6;
}
```

- [ ] **Step 5: JS — 전송 방식 전환 로직**

`ui/main.js` 의 `loadPrefs()` 함수 **앞에** 추가:

```js
// ===== 전송 방식 전환 =====
// WiFi 선택 → SSID/비번/서버IP/포트 입력란 표시
// USB 선택  → 위 입력란 숨김 + 안내 표시 (WiFi 없이 케이블로 직접 전송)
function currentTransport() {
  return $("transport-usb").checked ? 2 : 0;
}

function applyTransportUI() {
  const isUsb = currentTransport() === 2;
  $("wifi-fields").style.display = isUsb ? "none" : "";
  $("usb-note").classList.toggle("hidden", !isUsb);
}

$("transport-wifi").addEventListener("change", applyTransportUI);
$("transport-usb").addEventListener("change", applyTransportUI);
```

- [ ] **Step 6: JS — prefs 확장**

`savePrefs()` 의 `prefs` 객체에 두 줄 추가:

```js
      readMode: $("read-mode").value,
      transport: currentTransport(),
      fullScale: $("full-scale").value,
```

`loadPrefs()` 의 `if (p.readMode != null)` 줄 **뒤에** 추가:

```js
    if (p.fullScale != null) $("full-scale").value = p.fullScale;
    if (p.transport === 2) {
      $("transport-usb").checked = true;
    } else {
      $("transport-wifi").checked = true;
    }
    applyTransportUI();   // 복원한 선택에 맞춰 입력란 표시 상태를 맞춘다
```

`clearPrefs()` 의 `$("read-mode").value = "1";` 줄 **뒤에** 추가:

```js
  $("full-scale").value = "4";      // 기본 ±4g
  $("transport-wifi").checked = true;
  applyTransportUI();
```

- [ ] **Step 7: JS — 저장 호출에 파라미터 추가**

`main.js:244-252` 의 `invoke("write_config", {...})` 를 교체:

```js
    const result = await invoke("write_config", {
      port: selectedPort,
      ssid,
      password: pw,
      serverIp: srvIp,
      serverPort: srvPort,
      rateStep: rateStep,
      readMode: readMode,
      transport: currentTransport(),
      fullScaleG: parseInt($("full-scale").value, 10),
    });
```

- [ ] **Step 8: JS — USB 모드에서 WiFi 입력 검증 건너뛰기**

`$("btn-save")` 핸들러 초반의 입력 검증부에서, SSID/서버IP 필수 검사를 USB 모드일 때 건너뛰도록 감싼다. 검증 블록(`if (!ssid ...)` 또는 유사한 early return 구간) 앞에 추가:

```js
  // USB 직결 모드는 WiFi/서버 입력을 쓰지 않으므로 검증을 건너뛴다.
  const isUsbMode = currentTransport() === 2;
```

그리고 기존 SSID·서버IP 검증 조건에 `!isUsbMode &&` 를 앞에 붙인다. USB 모드에서는 `ssid`/`srvIp` 가 빈 문자열이어도 진행되어야 한다.

- [ ] **Step 9: 앱 실행 확인**

Run: `cd config-tool && npm run tauri dev`
Expected: 앱이 뜨고, 2단계에서 "USB 직결 전송" 선택 시 WiFi/서버 입력란이 사라지며 안내 박스가 나타난다. 다시 "WiFi 무선 전송" 선택 시 복귀한다.

- [ ] **Step 10: 커밋**

```bash
cd /Users/kimkookjin/Projects/ESP-IDF/IIS3DWB
git add config-tool/ui/index.html config-tool/ui/main.js config-tool/ui/style.css
git commit -m "$(cat <<'EOF'
GUI: 전송 방식(WiFi/USB) 선택 + 측정 범위 선택 추가

USB 직결 선택 시 WiFi·서버 입력란을 숨기고 검증도 건너뛴다.
측정 범위는 전송 방식과 무관하게 항상 표시한다.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: GUI — 공장 초기화 버튼

**Files:**
- Modify: `config-tool/ui/index.html:34` 부근 (1단계 고급 링크 옆)
- Modify: `config-tool/ui/main.js` (핸들러 추가)

**Interfaces:**
- Consumes: Task 3의 Tauri 명령 `factory_reset(port)`
- Produces: DOM id `btn-factory-reset`

- [ ] **Step 1: HTML — 버튼 추가**

`ui/index.html` 의 1단계, 기존 고급 링크 다음 줄에 삽입:

```html
      <a class="adv-link hidden" id="btn-factory-reset" title="저장된 WiFi·서버 설정을 모두 지웁니다. 펌웨어는 유지되므로 바로 다시 설정할 수 있습니다.">고급: 공장 초기화 (설정 삭제)</a>
```

- [ ] **Step 2: JS — 표시 조건 맞추기**

기존 `btn-flash-fw` 가 `hidden` 클래스를 제거하는 지점을 찾아(디바이스 감지 성공 시), 같은 자리에 추가:

```js
  $("btn-factory-reset").classList.remove("hidden");
```

- [ ] **Step 3: JS — 핸들러 추가**

`main.js` 의 `$("btn-back-1")` 핸들러 근처에 추가:

```js
// 공장 초기화 — 설정(NVS)만 지우고 펌웨어는 유지한다.
// 되돌릴 수 없으므로 확인을 받는다.
$("btn-factory-reset").addEventListener("click", async (e) => {
  e.preventDefault();
  if (!selectedPort) {
    alert("먼저 디바이스를 연결하세요.");
    return;
  }
  const ok = confirm(
    "저장된 설정이 모두 지워집니다.\n" +
    "펌웨어는 유지되므로 바로 다시 설정할 수 있습니다.\n\n" +
    "계속할까요?"
  );
  if (!ok) return;

  showOverlay("설정을 초기화하는 중...");
  try {
    const msg = await invoke("factory_reset", { port: selectedPort });
    hideOverlay();
    alert(msg + "\n다시 설정할 수 있습니다.");
  } catch (err) {
    hideOverlay();
    alert("초기화 실패: " + err);
  }
});
```

> `selectedPort` 는 기존 코드에서 쓰는 변수명이다. 실제 변수명이 다르면 그쪽에 맞춘다.

- [ ] **Step 4: 실행 확인**

Run: `cd config-tool && npm run tauri dev`
Expected: 디바이스 연결 후 1단계에 "고급: 공장 초기화" 링크가 보이고, 클릭 시 확인 대화상자가 뜬다. 취소하면 아무 일도 일어나지 않는다.

- [ ] **Step 5: 커밋**

```bash
cd /Users/kimkookjin/Projects/ESP-IDF/IIS3DWB
git add config-tool/ui/index.html config-tool/ui/main.js
git commit -m "$(cat <<'EOF'
GUI: 공장 초기화 버튼 추가 (확인 후 설정만 삭제)

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: config_manager — full_scale_g 필드

**Files:**
- Modify: `components/config_manager/config_manager.h:78-86` (`config_stream_t`)
- Modify: `components/config_manager/config_manager.c:16-23` (키 정의), `:143-177` (save), `:179-215` (load)

**Interfaces:**
- Produces: `config_stream_t.full_scale_g` (uint8_t, 값 2/4/8/16)

**주의:** 이 Task부터 펌웨어다. macOS에서 빌드할 수 없으므로 코드 작성만 하고 빌드 검증은 Task 10(라즈베리파이)에서 일괄 수행한다.

- [ ] **Step 1: 구조체에 필드 추가**

`config_manager.h` 의 `config_stream_t` 를 교체:

```c
typedef struct {
    char     server_ip[CONFIG_MGR_IP_MAX_LEN + 1]; /**< 라즈베리파이 IP */
    uint16_t server_port;                          /**< 수신 포트 (예: 9000) */
    uint8_t  rate_step;                            /**< 샘플레이트 단계 0~4 */
    uint8_t  transport;                            /**< 0=UDP, 1=TCP, 2=USB시리얼 */
    uint8_t  read_mode;                            /**< 0=폴링(자동), 1=인터럽트 */
    uint8_t  full_scale_g;                         /**< 측정 범위 2/4/8/16 (g) */
} config_stream_t;
```

- [ ] **Step 2: NVS 키 상수 추가**

`config_manager.c:23` 의 `NVS_KEY_READMODE` 정의 다음 줄에 추가:

```c
#define NVS_KEY_FULLSCALE "full_scale_g"
```

- [ ] **Step 3: 저장 로직 추가**

`config_manager.c:165` 의 `read_mode` 저장 줄 다음에 추가:

```c
    if (ret == ESP_OK) ret = nvs_set_u8(h, NVS_KEY_FULLSCALE, cfg->full_scale_g);
```

- [ ] **Step 4: 로드 폴백 추가**

`config_manager.c:207-209` 의 `read_mode` 폴백 블록 다음에 추가:

```c
    /* 구형 디바이스 NVS에는 이 키가 없다. 없으면 기존 빌드타임 기본값과
     * 같은 ±4g로 채워 동작이 바뀌지 않게 한다. 0으로 두면 감도 계산이 깨진다. */
    if (nvs_get_u8(h, NVS_KEY_FULLSCALE, &cfg->full_scale_g) != ESP_OK) {
        cfg->full_scale_g = 4;
    }
```

- [ ] **Step 5: 로그에 값 노출**

`config_manager.c` 의 load 함수 끝 `ESP_LOGI` 를 교체(진단 시 설정 확인용):

```c
    ESP_LOGI(TAG, "스트리밍 설정 로드 (서버 %s:%u, rate=%u, transport=%u, fs=±%ug)",
             cfg->server_ip, cfg->server_port, cfg->rate_step,
             cfg->transport, cfg->full_scale_g);
```

- [ ] **Step 6: 커밋**

```bash
cd /Users/kimkookjin/Projects/ESP-IDF/IIS3DWB
git add components/config_manager/
git commit -m "$(cat <<'EOF'
config_manager: full_scale_g 필드 추가

구형 NVS에 키가 없으면 기본값 4(±4g)로 폴백해 기존 동작을 유지한다.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: sensor_streamer — v2 헤더 + 시리얼 전송

**Files:**
- Modify: `components/sensor_streamer/sensor_streamer.h:25-46`
- Modify: `components/sensor_streamer/sensor_streamer.c:15-21`(include), `:52-60`(헤더), `:290-345`(tx_task), `:350-434`(start/stop)
- Modify: `components/sensor_streamer/CMakeLists.txt` (드라이버 의존성)

**Interfaces:**
- Consumes: Task 6의 `config_stream_t.full_scale_g`
- Produces: `STREAM_TRANSPORT_SERIAL = 2`, `sensor_streamer_config_t.full_scale_g`, v2 `stream_header_t`(18B)

- [ ] **Step 1: 헤더 — 전송 타입과 설정 필드**

`sensor_streamer.h` 에서 `STREAM_PROTO_VER` 를 2로 올리고:

```c
#define STREAM_PROTO_VER    2
```

`stream_transport_type_t` 에 값 추가:

```c
typedef enum {
    STREAM_TRANSPORT_UDP    = 0,
    STREAM_TRANSPORT_TCP    = 1,
    STREAM_TRANSPORT_SERIAL = 2,   /* USB 직결 — 로그를 끄고 패킷만 전송 */
} stream_transport_type_t;
```

`sensor_streamer_config_t` 에 필드 추가(`read_mode` 다음):

```c
    uint8_t full_scale_g;       /**< 측정 범위 2/4/8/16 (g) — 패킷 헤더에 실어 보냄 */
```

- [ ] **Step 2: 패킷 헤더 v2 확장**

`sensor_streamer.c:52-60` 의 `stream_header_t` 를 교체:

```c
/* 패킷 헤더 (18바이트, v2) — 수신 프로그램과 바이트 단위로 일치해야 함.
 * v2에서 full_scale_g 추가: 수신 측이 감도를 가정하지 않고 환산할 수 있다. */
typedef struct __attribute__((packed)) {
    uint32_t magic;         /* STREAM_MAGIC */
    uint8_t  version;       /* STREAM_PROTO_VER (=2) */
    uint8_t  rate_step;     /* 0~4 */
    uint16_t sample_count;  /* 이 패킷의 샘플 수 */
    uint32_t seq;           /* 패킷 시퀀스 번호 */
    uint32_t timestamp_ms;  /* 부팅 후 ms */
    uint8_t  full_scale_g;  /* 측정 범위 2/4/8/16 (g) */
    uint8_t  reserved;      /* 0 — 정렬 및 향후 확장 */
} stream_header_t;
```

- [ ] **Step 3: include 및 상태 변수 추가**

`sensor_streamer.c` 의 include 목록에 추가:

```c
#include "driver/usb_serial_jtag.h"
#include "esp_log.h"
```

모듈 상태 구조체(`static struct { ... } s;`)에 필드 추가:

```c
    bool usb_installed;          /* USB 드라이버 설치 여부 (stop에서 정리 판단) */
    esp_log_level_t saved_log;   /* 스트리밍 전 로그 레벨 (복원용) */
```

- [ ] **Step 4: tx_send 분기 함수 추가**

`tx_task` 함수 **앞에** 추가:

```c
/* 조립된 패킷을 설정된 전송 수단으로 내보낸다.
 * 패킷 포맷은 전송 수단과 무관하게 동일하므로, 여기서만 갈라진다.
 * 반환: 전송 바이트 수(>=0) 또는 실패(<0) */
static int tx_send(const uint8_t *packet, size_t len)
{
    if (s.cfg.transport == STREAM_TRANSPORT_SERIAL) {
        return usb_serial_jtag_write_bytes(packet, len, pdMS_TO_TICKS(100));
    }

    /* UDP: ENOMEM(lwip TX 버퍼 일시 부족) 시 양보하며 재시도.
     * INT 모드는 버스트로 몰려 순간 버퍼 고갈이 잦음 → 최대 8회 재시도. */
    int sent = -1;
    for (int attempt = 0; attempt < 8; attempt++) {
        sent = sendto(s.sock, packet, len, 0,
                      (struct sockaddr *)&s.dest, sizeof(s.dest));
        if (sent >= 0) break;
        if (errno != ENOMEM) break;
        vTaskDelay(1);
    }
    return sent;
}
```

- [ ] **Step 5: tx_task 에서 헤더 채우기 + 전송 교체**

`tx_task` 의 패킷 전송 블록에서, 헤더 채우기에 한 줄 추가:

```c
            h->full_scale_g = s.cfg.full_scale_g;
            h->reserved = 0;
```

그리고 기존 `sendto` 재시도 루프 전체(`int sent = -1; for (...) {...}`)를 한 줄로 교체:

```c
            int sent = tx_send(packet, len);
```

- [ ] **Step 6: start — 시리얼 모드 분기**

`sensor_streamer_start()` 의 인자 검증을 교체(시리얼은 `server_ip` 불필요):

```c
    if (!cfg || !cfg->sensor) {
        return ESP_ERR_INVALID_ARG;
    }
    if (cfg->transport != STREAM_TRANSPORT_SERIAL && !cfg->server_ip) {
        return ESP_ERR_INVALID_ARG;
    }
```

`memset(&s, 0, sizeof(s)); s.cfg = *cfg; s.sock = -1;` 다음에, 기존 "현재는 UDP만 구현" 블록과 소켓 생성 블록 전체를 다음으로 감싼다:

```c
    if (cfg->transport == STREAM_TRANSPORT_SERIAL) {
        /* USB 직결: 소켓 대신 USB Serial/JTAG 드라이버를 쓴다.
         * 기본 TX 버퍼(256B)는 패킷(1218B)보다 작아 매 전송이 블로킹되므로
         * 반드시 키운다. 8KB ≈ 26.6kHz에서 약 50ms 분량. */
        usb_serial_jtag_driver_config_t ucfg = USB_SERIAL_JTAG_DRIVER_CONFIG_DEFAULT();
        ucfg.tx_buffer_size = 8192;
        esp_err_t uret = usb_serial_jtag_driver_install(&ucfg);
        if (uret != ESP_OK) {
            ESP_LOGE(TAG, "USB 드라이버 설치 실패: %s", esp_err_to_name(uret));
            return uret;
        }
        s.usb_installed = true;

        /* 로그와 데이터가 같은 USB 포트를 쓰므로, 로그가 섞이면 패킷이 깨진다.
         * 스트리밍 동안 로그를 끄고 stop에서 복원한다. */
        s.saved_log = esp_log_level_get("*");
        esp_log_level_set("*", ESP_LOG_NONE);
    } else {
        /* 현재는 UDP만 구현 (TCP는 후속) */
        if (cfg->transport != STREAM_TRANSPORT_UDP) {
            ESP_LOGW(TAG, "TCP는 아직 미구현 — UDP로 진행");
            s.cfg.transport = STREAM_TRANSPORT_UDP;
        }

        /* --- 기존 소켓 생성 블록을 그대로 여기에 둔다 --- */
        /* socket(), memset(&s.dest...), inet_aton() 검증 포함 */
    }
```

> 기존 소켓 생성 코드는 그대로 `else` 안으로 옮긴다. 실패 시 `return` 하는 경로도 유지한다.

- [ ] **Step 7: start — 링버퍼 실패 시 정리 경로 보강**

링버퍼 생성 실패 블록에서 소켓만 닫던 것을 USB도 정리하도록 교체:

```c
    s.ringbuf = xRingbufferCreate(RINGBUF_SIZE, RINGBUF_TYPE_NOSPLIT);
    if (!s.ringbuf) {
        ESP_LOGE(TAG, "링버퍼 생성 실패");
        if (s.sock >= 0) { close(s.sock); s.sock = -1; }
        if (s.usb_installed) {
            esp_log_level_set("*", s.saved_log);
            usb_serial_jtag_driver_uninstall();
            s.usb_installed = false;
        }
        return ESP_ERR_NO_MEM;
    }
```

- [ ] **Step 8: stop — USB 정리 및 로그 복원**

`sensor_streamer_stop()` 의 소켓 정리 블록 다음에 추가:

```c
    if (s.usb_installed) {
        /* 링버퍼에 남은 패킷이 호스트로 나갈 시간을 준 뒤 정리한다. */
        usb_serial_jtag_wait_tx_done(pdMS_TO_TICKS(200));
        esp_log_level_set("*", s.saved_log);   /* 로그 복원 — 이후 진단 가능 */
        usb_serial_jtag_driver_uninstall();
        s.usb_installed = false;
    }
```

- [ ] **Step 9: CMakeLists 의존성 추가**

`components/sensor_streamer/CMakeLists.txt` 의 `REQUIRES` 에 `esp_driver_usb_serial_jtag` 를 추가한다. 예:

```cmake
idf_component_register(
    SRCS "sensor_streamer.c"
    INCLUDE_DIRS "."
    REQUIRES iis3dwb esp_driver_usb_serial_jtag
)
```

> 기존 `REQUIRES` 목록을 유지한 채 `esp_driver_usb_serial_jtag` 만 덧붙인다.

- [ ] **Step 10: 커밋**

```bash
cd /Users/kimkookjin/Projects/ESP-IDF/IIS3DWB
git add components/sensor_streamer/
git commit -m "$(cat <<'EOF'
sensor_streamer: USB 시리얼 전송 + v2 헤더(full_scale_g)

패킷 조립부는 그대로 두고 tx_send()에서 전송 수단만 분기한다.
시리얼 모드는 로그를 끄고 TX 버퍼를 8KB로 키워 블로킹을 막는다.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: main.c — 부팅 분기 + 3초 창 + 풀스케일 적용

**Files:**
- Modify: `main/main.c:288-296` (센서 init), `:501-620` (app_main 흐름)

**Interfaces:**
- Consumes: Task 6의 `config_stream_t.full_scale_g`, Task 7의 `STREAM_TRANSPORT_SERIAL` 및 `sensor_streamer_config_t.full_scale_g`

- [ ] **Step 1: 풀스케일 코드값 변환 헬퍼 추가**

`main.c` 의 `get_fullscale_string()` 함수 근처에 추가:

```c
/* 사람이 읽는 g 값(2/4/8/16) → IIS3DWB 레지스터 코드값.
 * 코드값은 순서가 직관과 어긋나므로(±16g가 0x04) 변환은 이 한 곳에만 둔다. */
static iis3dwb_fs_xl_t fs_g_to_code(uint8_t g)
{
    switch (g) {
        case 2:  return IIS3DWB_FS_2G;
        case 4:  return IIS3DWB_FS_4G;
        case 8:  return IIS3DWB_FS_8G;
        case 16: return IIS3DWB_FS_16G;
        default: return IIS3DWB_FS_4G;   /* 알 수 없는 값은 기본 ±4g */
    }
}
```

- [ ] **Step 2: 센서 초기화에 NVS 풀스케일 반영**

센서 init 함수가 풀스케일을 인자로 받도록 바꾼다. `main.c:294` 의

```c
        .full_scale = (iis3dwb_fs_xl_t)CONFIG_IIS3DWB_FULL_SCALE,
```

를 다음으로 교체하고, 이 설정을 만드는 함수에 `uint8_t full_scale_g` 파라미터를 추가한다:

```c
        .full_scale = fs_g_to_code(full_scale_g),
```

호출부에서는 NVS에서 읽은 값을 넘긴다. NVS 설정이 없는 경우(첫 부팅)에는 `4` 를 넘긴다.

- [ ] **Step 3: app_main — 설정을 먼저 읽어 전송 방식 판단**

`app_main` 에서 `config_manager_init()` 직후, WiFi 초기화 **전에** 스트리밍 설정을 읽는다:

```c
    /* 전송 방식을 먼저 확인한다. USB 직결이면 WiFi를 아예 띄우지 않아
     * 부팅이 빨라지고(최대 60초 연결 대기 제거) 소비전력도 준다. */
    config_stream_t scfg = {0};
    bool has_stream = config_manager_has_stream();
    if (has_stream) {
        if (config_manager_load_stream(&scfg) != ESP_OK) {
            has_stream = false;
        }
    }
    bool serial_mode = (has_stream && scfg.transport == 2);
```

- [ ] **Step 4: WiFi 초기화를 조건부로**

기존 WiFi 초기화 블록(`wifi_manager_init` ~ `wifi_manager_wait_for_connection` 구간)을 `if (!serial_mode) { ... }` 로 감싼다. 시리얼 모드에서는 이 블록 전체를 건너뛴다.

- [ ] **Step 5: 3초 명령 대기 창 추가**

스트리밍 시작 **직전**에 삽입:

```c
    if (serial_mode) {
        /* 스트리밍이 시작되면 USB 포트가 데이터로 가득 차고 로그도 꺼져
         * Config Tool이 접근할 수 없다. 이 3초가 설정을 되돌릴 유일한 창이다.
         * 절대 제거하지 말 것. */
        ESP_LOGI(TAG, "USB 직결 모드 — 3초 후 스트리밍을 시작합니다.");
        ESP_LOGI(TAG, "설정을 바꾸려면 지금 Config Tool을 연결하세요.");
        vTaskDelay(pdMS_TO_TICKS(3000));
    }
```

- [ ] **Step 6: 스트리밍 시작 조건 수정**

기존 조건 `iis3dwb_initialized && wifi_manager_is_connected() && config_manager_has_stream()` 을 전송 방식에 맞게 교체:

```c
    /* 시리얼 모드는 WiFi 연결이 필요 없다. */
    bool can_stream = iis3dwb_initialized && has_stream &&
                      (serial_mode || wifi_manager_is_connected());

    if (can_stream) {
        sensor_streamer_config_t st = {
            .sensor       = &sensor_handle,
            .server_ip    = scfg.server_ip,
            .server_port  = scfg.server_port,
            .rate_step    = scfg.rate_step,
            .transport    = (stream_transport_type_t)scfg.transport,
            .read_mode    = scfg.read_mode,
            .full_scale_g = scfg.full_scale_g,
        };
        esp_err_t sret = sensor_streamer_start(&st);
        /* --- 기존 결과 로깅 코드를 그대로 유지 --- */
    }
```

> `sensor_handle` 등 기존 변수명은 현재 `main.c` 코드에 맞춘다. 위 구조만 따르고 이름은 기존 것을 쓴다.

- [ ] **Step 7: 주기 로그도 조건부로**

시리얼 모드에서는 로그가 꺼져 있으므로 주기적 통계 출력 루프가 무의미하다. 해당 루프의 `ESP_LOGI` 호출은 그대로 두어도 무해하지만(로그 레벨이 NONE이라 출력되지 않음), 루프 자체의 `vTaskDelay` 주기는 유지한다. **수정 불필요** — 이 단계는 확인만 한다.

- [ ] **Step 8: 커밋**

```bash
cd /Users/kimkookjin/Projects/ESP-IDF/IIS3DWB
git add main/main.c
git commit -m "$(cat <<'EOF'
main: 전송 방식 분기 + 3초 명령 대기 창 + NVS 풀스케일 적용

시리얼 모드는 WiFi를 건너뛴다. 스트리밍 시작 전 3초 창은
설정을 되돌릴 유일한 경로이므로 반드시 유지해야 한다.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: 펌웨어 빌드 검증 (라즈베리파이)

**Files:** 없음 — 빌드·수정만

**환경:** 라즈베리파이 5, ESP-IDF v5.4.3

- [ ] **Step 1: 환경 준비 및 빌드**

```bash
source ~/esp/v5.4.3/esp-idf/export.sh
cd ~/<프로젝트경로>/IIS3DWB
git pull
idf.py build
```

Expected: 빌드 성공. 실패 시 컴파일 에러를 하나씩 수정한다. 흔한 원인:
- `esp_driver_usb_serial_jtag` 의존성 누락 → Task 7 Step 9 확인
- `esp_log_level_get` 미선언 → `esp_log.h` include 확인
- `sensor_handle` 등 변수명 불일치 → Task 8의 실제 이름 확인

- [ ] **Step 2: 헤더 크기 확인**

빌드 후 v2 헤더가 18바이트인지 확인한다. `main.c` 의 app_main 초반에 임시로 추가:

```c
    ESP_LOGI(TAG, "stream_header_t 크기: %d (18이어야 함)", (int)sizeof(stream_header_t));
```

플래시 후 로그로 `18` 을 확인하고, 확인되면 이 줄을 제거한다.

> `stream_header_t` 는 `.c` 파일에 있으므로, 확인용으로 `sensor_streamer.c` 의 start 함수 안에 넣는 편이 간단하다.

- [ ] **Step 3: 빌드 수정 커밋**

```bash
git add -A
git commit -m "$(cat <<'EOF'
펌웨어 빌드 수정 (라즈베리파이 검증)

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
git push
```

---

## Task 10: 통합 검증 + 기준 bin 재생성 (라즈베리파이 + 실기기)

**Files:**
- Create: `config-tool/src-tauri/tests_ref_full_nvs.bin` (재생성으로 교체)
- Modify: `config-tool/src-tauri/src/nvs.rs` (`#[ignore]` 제거)

- [ ] **Step 1: NVS 기준 bin 재생성**

라즈베리파이에서 실행:

```bash
source ~/esp/v5.4.3/esp-idf/export.sh
cd /tmp
cat > ref_full.csv <<'EOF'
key,type,encoding,value
devcfg,namespace,,
wifi_ssid,data,string,example2.4G
wifi_pass,data,string,example1234
srv_ip,data,string,192.168.0.37
srv_port,data,u16,9000
stream_rate,data,u8,0
transport,data,u8,0
read_mode,data,u8,0
full_scale_g,data,u8,4
EOF

python $IDF_PATH/components/nvs_flash/nvs_partition_generator/nvs_partition_gen.py \
       generate ref_full.csv tests_ref_full_nvs.bin 0x6000
```

생성된 `tests_ref_full_nvs.bin` 을 Mac의 `config-tool/src-tauri/` 로 복사한다.

- [ ] **Step 2: 테스트의 ignore 제거 및 인자 확인**

`nvs.rs` 의 `matches_full_reference_bin` 에서 `#[ignore = ...]` 줄을 삭제하고, 호출 인자가 위 CSV와 정확히 일치하는지 확인한다:

```rust
        let generated = generate_full_nvs(
            "devcfg",
            "example2.4G",
            "example1234",
            "192.168.0.37",
            9000,
            0,   // stream_rate
            0,   // transport
            0,   // read_mode
            4,   // full_scale_g
            0x6000,
        )
```

- [ ] **Step 3: 바이트 단위 비교 통과 확인**

Run: `cd config-tool && cargo test --manifest-path src-tauri/Cargo.toml --lib`
Expected: PASS — 5개 모두 통과, ignored 0

실패하면 CSV의 키 순서가 `generate_full_nvs` 의 `add_*` 호출 순서와 같은지 확인한다. NVS 엔트리는 기록 순서대로 배치되므로 순서가 다르면 바이트가 어긋난다.

- [ ] **Step 4: 실기기 — WiFi 모드 회귀 확인**

기존 동작이 깨지지 않았는지 먼저 확인한다.

1. GUI에서 WiFi 모드로 설정 저장
2. `idf.py -p /dev/ttyACM0 monitor` 로 부팅 로그 확인
3. 기대: WiFi 연결 성공, 스트리밍 시작, `udp_receiver.py` 로 데이터 수신

**기준:** 기존과 동일하게 동작할 것. 단 수신기가 v1 헤더(16B)를 가정하므로 이 단계에서는 패킷 파싱이 깨진다 — 이는 예상된 결과이며 Step 6에서 확인한다.

- [ ] **Step 4b: 실기기 — 구형 NVS 호환성 확인**

`full_scale_g` 키가 없는 구형 NVS에서 기본값 폴백이 동작하는지 확인한다.
이 폴백이 깨지면 구형 디바이스의 감도 계산이 틀어진다(Task 6 Step 4).

라즈베리파이에서 구형 형식 NVS를 만들어 주입한다(`full_scale_g` 줄 없음):

```bash
source ~/esp/v5.4.3/esp-idf/export.sh
cd /tmp
cat > old_fmt.csv <<'EOF'
key,type,encoding,value
devcfg,namespace,,
wifi_ssid,data,string,<실제SSID>
wifi_pass,data,string,<실제비밀번호>
srv_ip,data,string,<라즈베리파이IP>
srv_port,data,u16,9000
stream_rate,data,u8,1
transport,data,u8,0
read_mode,data,u8,1
EOF

python $IDF_PATH/components/nvs_flash/nvs_partition_generator/nvs_partition_gen.py \
       generate old_fmt.csv old_fmt.bin 0x6000

esptool.py --chip esp32s3 -p /dev/ttyACM0 -b 460800 write_flash 0x9000 old_fmt.bin
idf.py -p /dev/ttyACM0 monitor
```

**기준:** 부팅 로그의 설정 로드 줄에 `fs=±4g` 가 표시되어야 한다(키가 없어 기본값 4로 폴백).
`fs=±0g` 가 나오면 Task 6 Step 4의 폴백이 누락된 것이다.

- [ ] **Step 5: 실기기 — USB 시리얼 모드 확인**

1. GUI에서 "USB 직결 전송" 선택, 측정 범위 ±4g, 속도 3.3kHz로 저장
2. 디바이스 재부팅
3. 부팅 로그에서 "USB 직결 모드 — 3초 후..." 확인
4. 3초 후 로그가 멈추고 바이너리 데이터가 흐르는지 확인

**기준:** WiFi 연결 시도 로그가 없어야 한다(건너뛰었으므로). 3초 뒤 로그 출력이 멈춰야 한다.

- [ ] **Step 6: 패킷 수신 및 환산 확인**

임시 확인 스크립트를 라즈베리파이에서 실행:

```python
# /tmp/check_serial.py
import serial, struct

MAGIC = 0x49495333
HEADER_V2 = struct.Struct("<IBBHIIBB")
SENSITIVITY = {2: 0.061, 4: 0.122, 8: 0.244, 16: 0.488}

ser = serial.Serial("/dev/ttyACM0", 115200, timeout=2)
ser.reset_input_buffer()

buf = b""
count = 0
while count < 5:
    buf += ser.read(4096)
    # magic 스캔으로 패킷 경계 찾기
    idx = buf.find(struct.pack("<I", MAGIC))
    if idx < 0 or len(buf) - idx < HEADER_V2.size:
        continue
    magic, ver, rate, n, seq, ts, fs_g, _ = HEADER_V2.unpack_from(buf, idx)
    need = HEADER_V2.size + n * 6
    if len(buf) - idx < need:
        continue
    samples = struct.unpack_from("<%dh" % (n * 3), buf, idx + HEADER_V2.size)
    sens = SENSITIVITY.get(fs_g, 0.122)
    x, y, z = samples[0] * sens, samples[1] * sens, samples[2] * sens
    mag = (x*x + y*y + z*z) ** 0.5
    print(f"ver={ver} rate_step={rate} fs=±{fs_g}g n={n} seq={seq} "
          f"| 첫샘플 {x:.0f},{y:.0f},{z:.0f} mg | 합성 {mag:.0f} mg")
    buf = buf[idx + need:]
    count += 1
ser.close()
```

Run: `python3 /tmp/check_serial.py`

**기준:**
- `ver=2` — v2 헤더가 나와야 함
- `fs=±4g` — GUI에서 설정한 값이 헤더에 실려 와야 함
- **센서를 수평에 놓고 정지시킨 상태에서 합성값이 약 1000 mg** — 환산 정확성 검증

합성값이 500이나 2000 근처면 풀스케일 설정과 헤더 값이 어긋난 것이다.

- [ ] **Step 7: 고속 유실 측정**

GUI에서 속도를 단계별로 올리며 각각 확인한다: 3.3kHz → 6.6kHz → 13.3kHz → 26.6kHz

각 단계에서 `sensor_streamer_get_stats()` 의 `dropped` 를 확인해야 하는데, 시리얼 모드는 로그가 꺼져 있다. **확인 방법**: Step 6 스크립트에서 `seq` 를 연속으로 관찰해 건너뛴 번호가 있는지 본다.

```python
    # seq 연속성 확인용 — 이전 seq와 비교
    # 건너뛴 값이 있으면 패킷 유실
```

**기준:** seq가 연속이면 유실 없음. 유실이 발생한 최저 속도가 실제 상한이다.

**유실 발견 시:** `sensor_streamer.c` 의 `ucfg.tx_buffer_size` 를 16384로 올려 재측정한다. 그래도 유실되면 해당 속도를 GUI 옵션에서 제외하거나 경고를 표시하도록 스펙을 갱신한다.

- [ ] **Step 8: 공장 초기화 확인**

1. GUI 1단계에서 "고급: 공장 초기화" 클릭 → 확인
2. 디바이스 재부팅
3. 기대: 설정 없음 상태로 부팅(WiFi 연결 시도 없음, 스트리밍 없음)
4. GUI로 다시 설정 → 정상 동작 확인

**기준:** 펌웨어는 유지되어야 한다. 재플래시 없이 바로 재설정이 되어야 한다.

- [ ] **Step 9: 복구 경로 확인**

USB 시리얼 모드로 설정된 디바이스를 GUI로 되돌릴 수 있는지 확인한다.

1. USB 모드로 스트리밍 중인 디바이스에 GUI 연결
2. WiFi 모드로 변경해 저장
3. 기대: 정상 저장, 재부팅 후 WiFi 모드로 동작

**기준:** 공장 초기화는 NVS 직접 주입이라 포트 점유와 무관하게 동작해야 한다. 만약 실패하면 3초 창 타이밍에 맞춰 재시도한다.

- [ ] **Step 10: 결과 기록 및 커밋**

측정한 실제 상한을 스펙 10장(미해결 사항)에 반영한다:

```bash
cd /Users/kimkookjin/Projects/ESP-IDF/IIS3DWB
# docs/2026-09-22-serial-streaming-design.md 의 10장 1번 항목에
# 실측 결과를 기록 (예: "26.6kHz까지 유실 없음 확인" 또는 "13.3kHz가 상한")
git add config-tool/src-tauri/tests_ref_full_nvs.bin \
        config-tool/src-tauri/src/nvs.rs \
        docs/2026-09-22-serial-streaming-design.md
git commit -m "$(cat <<'EOF'
통합 검증: 기준 bin 재생성 + 실기기 확인

NVS 기준 bin을 ESP-IDF로 재생성해 바이트 단위 검증을 복구했다.
실측한 고속 전송 상한을 스펙에 기록.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## 완료 기준

| 항목 | 확인 방법 |
|------|----------|
| NVS 생성 정확성 | `cargo test` 5개 통과, ignored 0 |
| WiFi 모드 회귀 없음 | 기존 설정 디바이스가 그대로 동작 (Task 10 Step 4) |
| 구형 NVS 호환성 | `full_scale_g` 키 없는 NVS에서 `fs=±4g` 폴백 (Task 10 Step 4b) |
| USB 전송 동작 | `ver=2`, `fs=±4g` 패킷 수신 (Task 10 Step 6) |
| 환산 정확성 | 정지 시 3축 합성 ≈ 1000 mg (Task 10 Step 6) |
| 고속 상한 확정 | seq 연속성으로 유실 측정, 스펙에 기록 (Task 10 Step 7) |
| 공장 초기화 | 설정 삭제 후 재설정 가능, 펌웨어 유지 (Task 10 Step 8) |
| 복구 경로 | USB 모드 → WiFi 모드 전환 가능 (Task 10 Step 9) |

## 범위 밖

**수신 프로그램** — 라즈베리파이(EDU Kit WDAQ)에서 별도 개발. GPIO 포토센서 게이팅 포함. 스펙 7장이 입력 사양이다.

**기존 `udp_receiver.py` 갱신** — v2 헤더를 읽도록 수정이 필요하나 WDAQ 작업에 속한다. Task 10 Step 4에서 파싱이 깨지는 것은 예상된 결과다.
