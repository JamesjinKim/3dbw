# Design: 하이브리드 고속 스트리밍 (FIFO 버스트 읽기)

- **Feature**: high-speed-fifo
- **Plan**: [[high-speed-fifo.plan]]
- **작성일**: 2026-06-01
- **방향**: 옵션 A 하이브리드 — `rate_step`으로 폴링/FIFO 자동 전환
- **CPU 부하**: 현장 요구로 "부하 감소" 필요. **INT 핀 미연결(SPI+UART만 사용)** →
  하드웨어 인터럽트 불가 → **Watermark 기반 효율 폴링**으로 부하 절감 (아래 8장)

## 1. 아키텍처

```
sensor_streamer_start(rate_step)
   ├ rate_step == 0 → xTaskCreate(sensor_task_polling)   [기존, 무변경]
   └ rate_step >= 1 → xTaskCreate(sensor_task_fifo)      [신규]
                          │
        iis3dwb_fifo_enable(bdr)  ← 부팅 시 1회
        loop: iis3dwb_fifo_count() → iis3dwb_read_fifo(buf, n)
              → (태그 제외) X/Y/Z 만 추출 → xRingbufferSend
                          │
   [링버퍼] → [tx_task] → UDP → 라즈베리파이   [전부 공통, 무변경]
```

핵심: **생산자 태스크만 분기**. 링버퍼/전송/패킷/소켓은 1단계 코드 그대로.

## 2. 드라이버 신규 API (iis3dwb.h / .c)

### 2.1 FIFO 활성화
```c
/** FIFO를 continuous 모드로 켜고 BDR(배치 데이터율) 설정 */
esp_err_t iis3dwb_fifo_enable(iis3dwb_handle_t *h, uint8_t bdr_code);
/** FIFO 끄기 (bypass) — 폴링 모드 복귀용 */
esp_err_t iis3dwb_fifo_disable(iis3dwb_handle_t *h);
```
- `FIFO_CTRL3`(0x09): 하위 니블 = 가속도 BDR 코드.
- `FIFO_CTRL4`(0x0A): 모드 = `IIS3DWB_FIFO_CONTINUOUS`(0x06).
  - continuous: FIFO가 가득 차면 오래된 샘플을 덮어씀 → 스트리밍에 적합.

**[데이터시트 확정 — Table 16] BDR_XL[3:0]은 두 값만 허용:**
- `0000` = 미배치, **`1010` = 26667 Hz (유일 동작값)**, `1011~1111` 금지.
- → **FIFO BDR은 26.6kHz 단일.** 중간 단계(3.3k/6.6k/13.3k)는 하드웨어에 없음.
  (IIS3DWB는 초광대역 진동센서로 ODR=26.6kHz 고정)

**결정: FIFO 26.6kHz + 소프트웨어 데시메이션**
FIFO는 항상 26.6kHz로 적재·읽되, 펌웨어가 **N개당 1개만 링버퍼에 적재**하여 5단계를 표현:

| rate_step | 목표 레이트 | 데시메이션(N) | FIFO BDR |
|-----------|------------|---------------|----------|
| 0 | 1 kHz | (폴링, FIFO 미사용) | - |
| 1 | 3.3 kHz | 8 (26667/8≈3333) | 1010 |
| 2 | 6.6 kHz | 4 (26667/4≈6667) | 1010 |
| 3 | 13.3 kHz | 2 (26667/2≈13333) | 1010 |
| 4 | 26.6 kHz | 1 (모두 전송) | 1010 |

> FIFO_CTRL3 = `0x0A`(BDR=1010), FIFO_CTRL4 FIFO_MODE = `110`(Continuous, 가득 차면 덮어씀).

### 2.2 FIFO 적재 개수
```c
/** FIFO에 쌓인 샘플(워드) 개수 읽기 (FIFO_STATUS1/2 의 DIFF_FIFO 9비트) */
esp_err_t iis3dwb_fifo_count(iis3dwb_handle_t *h, uint16_t *count);
```
- `FIFO_STATUS1`(0x3A): DIFF_FIFO[7:0]
- `FIFO_STATUS2`(0x3B): bit[0:1]=DIFF_FIFO[9:8] + 상태 플래그(OVR 등)
- count = ((status2 & 0x03) << 8) | status1

### 2.3 FIFO 버스트 읽기
```c
/** FIFO에서 최대 max_samples 개를 읽어 raw[]에 채움. 실제 읽은 수 *out_n 반환 */
esp_err_t iis3dwb_read_fifo(iis3dwb_handle_t *h,
                            iis3dwb_raw_data_t *raw, uint16_t max_samples,
                            uint16_t *out_n);
```
- FIFO 워드 1개 = `FIFO_DATA_OUT_TAG`(0x78) 1B + X/Y/Z 6B = **7바이트**.
- `iis3dwb_read_registers(h, 0x78, buf, n*7)` 로 연속 버스트 (이미 존재하는 함수 재사용).
- 파싱: 각 7B 블록에서 tag(buf[k*7+0])는 가속도 태그(0x02 류)인지 확인, X=buf[k*7+1..2] LE …
  - read_raw_data와 동일한 LE 조합: `x = (int16)(d[2]<<8 | d[1])` (TAG가 [0]이라 +1 오프셋).
- 버스트 1회 최대 샘플 수 상한 `FIFO_BURST_MAX = 64` (64×7=448B, SPI/DMA 안전).

## 3. 스트리머 변경 (sensor_streamer.c)

### 3.1 rate_step → 데시메이션 매핑
```c
static uint8_t rate_to_decim(uint8_t rate_step) {
    switch (rate_step) { case 1: return 8; case 2: return 4;
                         case 3: return 2; default: return 1; } // 4=1(모두)
}
```

### 3.2 sensor_task_fifo (신규 생산자, 데시메이션 포함)
```c
static void sensor_task_fifo(void *arg) {
    uint8_t decim = rate_to_decim(s.cfg.rate_step);  // N개당 1개 전송
    uint32_t phase = 0;
    iis3dwb_fifo_enable(s.cfg.sensor, IIS3DWB_BDR_26667);  // 항상 26.6kHz
    iis3dwb_raw_data_t burst[FIFO_BURST_MAX];
    while (s.running) {
        uint16_t avail = 0;
        iis3dwb_fifo_count(s.cfg.sensor, &avail);
        if (avail == 0) { vTaskDelay(1); continue; }   // 1틱 양보
        uint16_t want = avail > FIFO_BURST_MAX ? FIFO_BURST_MAX : avail;
        uint16_t got = 0;
        if (iis3dwb_read_fifo(s.cfg.sensor, burst, want, &got) == ESP_OK) {
            for (uint16_t i = 0; i < got; i++) {
                if (phase++ % decim != 0) continue;     // 데시메이션
                uint8_t sample[6];
                memcpy(sample,   &burst[i].x, 2);
                memcpy(sample+2, &burst[i].y, 2);
                memcpy(sample+4, &burst[i].z, 2);
                if (xRingbufferSend(s.ringbuf, sample, 6, 0) != pdTRUE)
                    s.stats.dropped++;
            }
        }
    }
    iis3dwb_fifo_disable(s.cfg.sensor);
    vTaskDelete(NULL);
}
```
> 데시메이션은 단순 추출(every-Nth). 안티앨리어싱 필터는 미적용 — 진동 모니터링 1차 목적엔 충분.
> 정밀 분석이 필요하면 후속 단계에서 평균/저역통과 데시메이션 고려.
- 폴링 한계 회피: SPI 단발 대신 묶음 읽기 → 26.6kHz도 한 번에 수십 샘플 처리.
- FIFO가 비면 1틱 양보(타 태스크/WiFi에 CPU 양보), 차면 버스트.

### 3.3 start() 분기
```c
if (s.cfg.rate_step == 0)
    xTaskCreate(sensor_task, "strm_sensor", 4096, NULL, 6, &s.sensor_task_h);
else
    xTaskCreate(sensor_task_fifo, "strm_fifo", 4096, NULL, 6, &s.sensor_task_h);
```
- 기존 `sensor_task` 함수명 유지(폴링). 패킷/전송/통계 로깅 무변경.

## 3.5 CPU 부하 절감 — Watermark 효율 폴링 (INT 핀 미사용)

**제약:** 현재 보드는 SPI + UART만 사용. IIS3DWB INT1 핀이 ESP32 GPIO에 미연결 →
하드웨어 인터럽트(핀 ISR) 불가. 대신 소프트웨어로 부하를 줄인다.

**문제(개선 전):** `sensor_task_fifo`가 매 루프 `fifo_count`를 SPI로 읽고, 비면 `vTaskDelay(1)`.
→ FIFO가 빈 동안에도 1틱(10ms)마다 SPI 트랜잭션 발생 = 불필요한 부하.

**개선: Watermark 만큼 쌓일 시간을 계산해 CPU 양보**
- 목표 묶음 크기 `WTM`(예: 64샘플)을 정한다.
- 26.6kHz에서 64샘플이 쌓이는 시간 ≈ `64 / 26667 ≈ 2.4 ms`.
- 그 시간만큼 `vTaskDelay`로 자고, 깨어나 한 번에 버스트 읽기.
- → SPI 호출 빈도가 "샘플당"이 아니라 "묶음당"으로 감소. CPU가 대부분 잠들어 있음.

```c
#define STREAM_WTM_SAMPLES 64
// 1회 대기 = WTM개 쌓이는 시간 (틱 단위, 최소 1틱)
uint32_t batch_ms = (STREAM_WTM_SAMPLES * 1000) / 26667;   // ≈2ms
TickType_t wait = pdMS_TO_TICKS(batch_ms);
if (wait < 1) wait = 1;

while (s.running) {
    vTaskDelay(wait);                       // WTM 쌓일 동안 CPU 양보
    uint16_t avail = 0;
    iis3dwb_fifo_count(s.cfg.sensor, &avail);   // 깨어나서 1번만 확인
    while (avail > 0 && s.running) {
        uint16_t want = avail > IIS3DWB_FIFO_BURST_MAX ? IIS3DWB_FIFO_BURST_MAX : avail;
        ... read_fifo + 데시메이션 + 링버퍼 ...
        avail -= got;
    }
}
```
- 효과: 빈 FIFO를 헛되이 폴링하지 않음. `fifo_count`/`read_fifo` 호출이 묶음당 1~수회로 감소.
- (선택) FIFO_CTRL1/2에 하드웨어 WTM 임계도 설정 가능하나, INT 없이 읽으므로 시간기반 대기로 충분.
- **향후 INT 핀이 배선되면**: 이 `vTaskDelay`를 `xSemaphoreTake`(ISR이 give)로 교체하면 진짜 인터럽트 방식으로 무손실 전환 가능 (구조 동일).

## 4. 데이터 정합성

- 패킷 포맷 변경 없음: 여전히 `{int16 x,y,z}` × N. rate_step 헤더 필드로 수신측이 레이트 인지.
- 라즈베리파이 수신기 변경 불필요 (이미 rate_step별 RATE_HZ 매핑 보유).
- FIFO 태그는 디바이스 내부에서만 사용, 링버퍼엔 X/Y/Z만 적재 → 네트워크 포맷 동일.

## 5. 검증 기준 (Check)

| 항목 | 기준 |
|------|------|
| 회귀 | rate_step=0 → 기존 폴링 1kHz/드롭0 그대로 |
| 6.6kHz | rate_step=2 → 라즈베리파이 실효 ≈ 6.6kHz (±10%) |
| 26.6kHz | rate_step=4 → 1kHz보다 명백히 높음, 한계 측정·기록 |
| 오버런 | FIFO OVR/링버퍼 풀 → dropped 통계 증가로 가시화 |
| 안정성 | 수 분 연속 전송 크래시 없음 |

## 6. 구현 순서 (Do) — 위험도 순

1. **데이터시트 BDR 코드 확정** (FIFO_CTRL3 표) → `rate_to_bdr` 작성
2. 드라이버 `iis3dwb_fifo_enable/disable/count/read_fifo` 구현
3. **소량 덤프 검증**: FIFO 켜고 10샘플 읽어 시리얼 출력 → 태그/XYZ 정렬·중력값 확인 (가장 중요)
4. `sensor_task_fifo` + start 분기
5. 빌드 → 실기기: rate_step 2 → 4 순 측정 (라즈베리파이 실효 레이트)
6. 한계 레이트·드롭률 기록 (3단계 안정화 입력)

## 7. 리스크 & 대응

| 리스크 | 대응 |
|--------|------|
| BDR 코드 오확정 → 레이트 안 맞음 | 3번 소량 덤프에서 실측 레이트로 교차검증 |
| 태그 오프셋 오류 → XYZ 어긋남 | 3번에서 정지 시 한 축 ≈1g(중력) 확인으로 검증 |
| 26.6kHz UDP/WiFi 처리량 한계 | 정직하게 한계 측정·문서화, 필요 시 BDR 낮춰 권장값 제시 |
| 버스트 SPI 길이 한계 | FIFO_BURST_MAX=64로 상한 (448B) |
