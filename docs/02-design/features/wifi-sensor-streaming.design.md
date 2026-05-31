# Design: WiFi 센서 데이터 무선 스트리밍

> **기능명**: wifi-sensor-streaming
> **작성일**: 2026-05-29
> **상태**: Design (Do 전)
> **기반**: [wifi-sensor-streaming.plan.md](../../01-plan/features/wifi-sensor-streaming.plan.md)

---

## 0. 확정된 결정 (Plan + 사용자)

| 항목 | 결정 |
|------|------|
| 받는 쪽 | 라즈베리파이 (Python UDP 수신) |
| 전송 프로토콜 | **UDP 1순위** (TCP 전환 가능하게 추상화) |
| 샘플레이트 | NVS 저장, **5단계** 선택 |
| 패킷 포맷 | 바이너리 (아래 3장 — 제안) |
| 센서 읽기 | 고속 시 FIFO 활용 (읽기 함수 신규 구현) |

---

## 1. 전체 구조

```
┌──────────────────── ESP32-S3 펌웨어 ────────────────────┐
│  [IIS3DWB] ─SPI→ sensor_task ─(ring buffer)→ udp_tx_task │
│                       ▲                          │ UDP    │
│                  설정(NVS): rate, server_ip, port│        │
└──────────────────────────────────────────────────┼───────┘
                                                    ▼
                          [라즈베리파이] udp_receiver.py
                            → 파싱 → 파일/표시 (CSV/바이너리 저장)
```

- **sensor_task**: 센서에서 샘플 읽어 링버퍼에 적재 (설정 rate에 따라 폴링 or FIFO)
- **udp_tx_task**: 링버퍼에서 모아 UDP 패킷으로 전송
- 두 태스크를 **분리**해 SPI 읽기와 WiFi 전송이 서로 안 막히게 (생산자-소비자)

---

## 2. 샘플레이트 5단계 (NVS 설정)

ODR은 26.667kHz 고정이므로, **데시메이션(N개당 1개 샘플링)**으로 유효 레이트를 조절한다.

| 단계 | 유효 레이트 | 대역폭(3축×2B) | 방식 | 용도 |
|------|-----------|---------------|------|------|
| 0 | **1 kHz** | ~6 KB/s | 폴링/데시메이션 | 일반 모니터링 (기본값) |
| 1 | **3.3 kHz** | ~20 KB/s | 폴링/데시메이션 | 저속 분석 |
| 2 | **6.6 kHz** | ~40 KB/s | FIFO | 중속 |
| 3 | **13.3 kHz** | ~80 KB/s | FIFO | 고속 |
| 4 | **26.6 kHz** | ~160 KB/s | FIFO (풀) | 정밀 진동분석 |

- 기본값: **단계 0 (1kHz)** — 안정 우선, 처음 검증에 적합
- NVS 키: `stream_rate` (uint8, 0~4)
- 단계 2 이상은 폴링으로 못 따라가므로 **FIFO 묶음 읽기** 사용

---

## 3. 바이너리 패킷 포맷 (제안)

UDP 데이터그램 1개 = 헤더(16B) + 샘플 배열. 손실/순서 추적을 위해 시퀀스 번호 포함.

```
┌─ 헤더 (16 bytes) ──────────────────────────────────────┐
│ offset  size  field         설명                        │
│ 0       4     magic         0x49495333 ("IIS3" )         │
│ 4       1     version       프로토콜 버전 = 1            │
│ 5       1     rate_step     샘플레이트 단계 (0~4)        │
│ 6       2     sample_count  이 패킷의 샘플 수 (N)        │
│ 8       4     seq           패킷 시퀀스 번호 (손실 감지) │
│ 12      4     timestamp_ms  디바이스 부팅 후 ms          │
├─ 페이로드 (N × 6 bytes) ───────────────────────────────┤
│ 각 샘플: int16 x, int16 y, int16 z  (little-endian)     │
└────────────────────────────────────────────────────────┘
```

- **샘플당 6바이트** (원시 int16 × 3축) — `iis3dwb_raw_data_t`와 동일
- **패킷당 샘플 수 N**: UDP MTU 안전선(~1400B) 고려 → **N = 200** (200×6=1200B + 16B 헤더 = 1216B)
- **seq**: 라즈베리파이가 손실/순서 판단 → 유실률 측정 (NFR-1 충족)
- mg 변환은 **서버에서** (감도 = 풀스케일 따라). 디바이스는 원시값만 보내 효율↑

> TCP 전환 시: 같은 패킷을 길이 프리픽스(4B) 붙여 스트림으로. 전송 계층만 교체.

---

## 4. 펌웨어 설계

### 4-1. 새 컴포넌트: `sensor_streamer`
```
components/sensor_streamer/
├── sensor_streamer.h   # 시작/정지 API
├── sensor_streamer.c   # sensor_task + udp_tx_task + 링버퍼
└── CMakeLists.txt
```

| 함수 | 역할 |
|------|------|
| `streamer_start(cfg)` | 두 태스크 생성, UDP 소켓 열기 |
| `streamer_stop()` | 태스크 정지, 소켓 닫기 |
| `streamer_get_stats(&s)` | 전송 패킷/바이트/드롭 카운트 |

### 4-2. 전송 계층 추상화 (UDP↔TCP 교체 용이)
```c
typedef struct {
    esp_err_t (*open)(const char *ip, uint16_t port);
    int       (*send)(const uint8_t *buf, size_t len);
    void      (*close)(void);
} stream_transport_t;
// udp_transport / tcp_transport 두 구현. cfg로 선택.
```

### 4-3. 생산자-소비자 (링버퍼)
- `sensor_task` (우선순위 높음): 센서 읽기 → 링버퍼 push. SPI에 집중.
- `udp_tx_task`: 링버퍼에서 N샘플 pop → 패킷 조립 → transport->send()
- 링버퍼 가득 차면(WiFi가 못 따라감) **가장 오래된 데이터 드롭 + 드롭 카운트++** (오버런 방지)
- FreeRTOS `ringbuf` (esp_ringbuf) 사용

### 4-4. 센서 읽기 — 단계별
- **저속(0,1)**: 기존 `iis3dwb_read_raw_data()` 폴링 + 데시메이션
- **고속(2~4)**: **신규 `iis3dwb_read_fifo()`** — FIFO 워터마크 인터럽트 또는 폴링으로 묶음 읽기
  - 신규 구현 필요 (현재 FIFO 레지스터 정의만 있고 읽기 함수 없음)
  - CTRL/FIFO_CTRL 레지스터로 FIFO 모드·워터마크 설정

### 4-5. 설정 (NVS) — config_manager 확장
기존 `config_manager`에 스트리밍 설정 추가:
```c
typedef struct {
    char server_ip[16];   // 라즈베리파이 IP
    uint16_t server_port; // 예: 9000
    uint8_t rate_step;    // 0~4
    uint8_t transport;    // 0=UDP, 1=TCP
} stream_config_t;
config_save_stream(&cfg); / config_load_stream(&cfg);
```
- NVS 네임스페이스 `devcfg` 재사용 (키: `srv_ip`, `srv_port`, `stream_rate`, `transport`)
- 설정 툴(GUI)에서 server_ip/port/rate를 NVS에 주입 (WiFi와 동일 방식)

### 4-6. main.c 통합
```
부팅 → WiFi 연결(기존) → 연결 성공 시 streamer_start(cfg)
  설정 없으면 스트리밍 비활성(기존 USB 로그만)
```

---

## 5. 라즈베리파이 수신 프로그램 (제안)

```
rpi-receiver/
├── udp_receiver.py     # UDP 수신 + 패킷 파싱 + 저장
└── README.md
```

핵심 로직:
```python
import socket, struct
HEADER = struct.Struct("<IBBHII")  # magic,ver,rate,count,seq,ts (16B)
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(("0.0.0.0", 9000))
last_seq = None; lost = 0
while True:
    data, _ = sock.recvfrom(2048)
    magic, ver, rate, count, seq, ts = HEADER.unpack_from(data, 0)
    if magic != 0x49495333: continue
    # 손실 감지
    if last_seq is not None and seq != last_seq + 1:
        lost += seq - last_seq - 1
    last_seq = seq
    # 샘플 파싱 (count × int16 x3)
    samples = struct.unpack_from("<%dh" % (count*3), data, 16)
    # → CSV/바이너리 파일에 저장 (또는 화면 표시)
```
- **저장**: CSV(분석 쉬움) 또는 바이너리(고속 시 용량 효율). 설정으로 선택.
- **유실률 출력**: seq 기반으로 실시간 손실률 표시 (NFR-1 검증)
- 후처리(FFT 등)는 저장된 데이터로 별도 (범위 밖)

---

## 6. 단계별 구현 순서 (Do에서 따라갈 순서)

1. **NVS 스트리밍 설정** — config_manager에 server_ip/port/rate 추가
2. **sensor_streamer 골격** — UDP transport + 링버퍼 + 두 태스크
3. **저속(1kHz) 폴링 전송** — 1단계 검증 (데이터 도달 확인)
4. **라즈베리파이 udp_receiver.py** — 수신·파싱·저장·유실률
5. **FIFO 읽기 함수** — `iis3dwb_read_fifo()` 신규 구현
6. **중·고속 단계(2~4)** — FIFO 묶음 전송, 처리량 측정
7. **안정화** — 드롭/재연결/장시간 테스트, 26.6kHz 한계 확인
8. (선택) TCP transport 추가

---

## 7. 검증 기준

| 항목 | 기준 |
|------|------|
| 1단계 | 라즈베리파이에서 1kHz 데이터 수신·저장 확인 |
| 유실률 | seq 기반 측정, 저속에서 ~0% |
| 고속 | 26.6kHz 시도 → 실제 도달 레이트/유실률 기록 |
| 안정성 | 30분+ 연속 전송 드롭률 측정 |
| 설정 | NVS rate_step 변경 → 실제 레이트 반영 |

---

## 8. 미결/리스크

- **26.6kHz 실제 도달 여부는 실측 전 미확정** — 안 되면 단계 3(13.3kHz)이 현실적 상한일 수 있음. 5단계 설정이라 문제 없이 낮춰 사용 가능.
- FIFO 읽기 함수가 신규 구현이라 센서 데이터시트(레지스터) 정밀 확인 필요.
- UDP 손실은 정상 (설계상 감수). 손실 많으면 TCP 전환 또는 레이트 하향.

---

## 9. 다음 단계
```
/pdca do wifi-sensor-streaming
```
구현은 위 6장 순서대로. **저속 1단계부터** 검증하며 진행.
