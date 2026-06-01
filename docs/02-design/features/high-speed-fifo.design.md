# Design: 하이브리드 고속 스트리밍 (FIFO 버스트 읽기)

- **Feature**: high-speed-fifo
- **Plan**: [[high-speed-fifo.plan]]
- **작성일**: 2026-06-01
- **방향**: 옵션 A 하이브리드 — `rate_step`으로 폴링/FIFO 자동 전환

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

**BDR 코드 (FIFO_CTRL3[3:0], 데이터시트 기준 — 구현 시 재확인 필수)**
| rate_step | 목표 | BDR 코드(예상) | 비고 |
|-----------|------|----------------|------|
| 1 | 3.3 kHz | 0b0111 (?) | 데이터시트 표로 확정 |
| 2 | 6.6 kHz | 0b1000 (?) | 〃 |
| 3 | 13.3 kHz | 0b1001 (?) | 〃 |
| 4 | 26.6 kHz | 0b1010 (=full ODR) | 최대 |
> ⚠️ Do 단계 첫 작업: 데이터시트 FIFO_CTRL3 BDR 표에서 정확한 코드 확정 후 매핑 함수 작성.

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

### 3.1 rate_step → BDR 매핑
```c
static uint8_t rate_to_bdr(uint8_t rate_step); // 위 표 기반, Do에서 확정
```

### 3.2 sensor_task_fifo (신규 생산자)
```c
static void sensor_task_fifo(void *arg) {
    iis3dwb_fifo_enable(s.cfg.sensor, rate_to_bdr(s.cfg.rate_step));
    iis3dwb_raw_data_t burst[FIFO_BURST_MAX];
    while (s.running) {
        uint16_t avail = 0;
        iis3dwb_fifo_count(s.cfg.sensor, &avail);
        if (avail == 0) { vTaskDelay(1); continue; }   // 1틱 양보
        uint16_t want = avail > FIFO_BURST_MAX ? FIFO_BURST_MAX : avail;
        uint16_t got = 0;
        if (iis3dwb_read_fifo(s.cfg.sensor, burst, want, &got) == ESP_OK) {
            for (uint16_t i = 0; i < got; i++) {
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
