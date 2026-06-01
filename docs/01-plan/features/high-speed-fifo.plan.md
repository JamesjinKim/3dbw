# Plan: 하이브리드 고속 스트리밍 (FIFO 버스트 읽기)

- **Feature**: high-speed-fifo
- **작성일**: 2026-06-01
- **상태**: Plan
- **관련**: [[wifi-sensor-streaming]] (1단계 폴링, 검증 완료), [[gui-server-config]] (GUI 설정)
- **기획 근거**: `docs/html/wifi-streaming-plan.html` 9장 (옵션 A = 하이브리드 확정)

## 1. 배경 / 문제

- GUI는 1k~26.6kHz 5단계를 노출하지만, 펌웨어가 **폴링 + vTaskDelay** 방식이라
  `CONFIG_FREERTOS_HZ=100`(1틱=10ms) 한계로 **실제로는 ~1kHz까지만** 동작.
- 3.3kHz 이상 선택 시 SPI 단발 읽기가 센서 ODR(26.6kHz 고정)을 못 따라가 언더샘플링.
- 메뉴와 실제 능력이 불일치하는 상태.

## 2. 목표 — 하이브리드 자동 전환

`rate_step` 값으로 센서 읽기 방식을 **자동 선택**한다. 사용자는 속도만 고르면 됨.

| rate_step | 속도 | 방식 |
|-----------|------|------|
| 0 | 1 kHz | 폴링 (현재, 그대로 유지) |
| 1~4 | 3.3k~26.6kHz | **FIFO 버스트 읽기 (신규)** |

핵심 원칙: **검증된 폴링 경로는 건드리지 않고, FIFO 경로만 추가**한다.

## 3. 범위 (In Scope)

| # | 요구사항 | 영역 |
|---|----------|------|
| FR-1 | `iis3dwb_fifo_enable(handle, bdr)` — FIFO 활성화 (CTRL3=BDR, CTRL4=continuous) | iis3dwb 드라이버 |
| FR-2 | `iis3dwb_fifo_count(handle, *n)` — FIFO_STATUS로 적재 샘플 수 읽기 | iis3dwb 드라이버 |
| FR-3 | `iis3dwb_read_fifo(handle, samples[], max, *read)` — DATA_OUT 버스트로 N샘플 읽기 | iis3dwb 드라이버 |
| FR-4 | `sensor_task_fifo()` — watermark/주기마다 FIFO 묶음 읽어 링버퍼 적재 | sensor_streamer |
| FR-5 | `sensor_streamer_start()`에서 rate_step으로 폴링/FIFO 분기 | sensor_streamer |
| FR-6 | rate_step → BDR 매핑 (3.3k/6.6k/13.3k/26.6k) | sensor_streamer |

## 4. 범위 외 (Out of Scope)

- 폴링 경로(rate_step=0) 변경 — 그대로 유지
- 링버퍼 / 전송 태스크 / UDP / 패킷 포맷 변경 — 그대로 재사용
- GUI 변경 — 이미 5단계 노출 중 (불일치가 이번 작업으로 해소됨)
- TCP transport, watermark 하드웨어 인터럽트(우선 폴링형 FIFO 체크로 시작, 필요 시 INT 추가)

## 5. 기술 설계 요점

### 5.1 FIFO 동작 (IIS3DWB)
- 센서는 ODR 26.6kHz로 내부 **3KB FIFO(최대 ~512샘플)** 에 자동 적재.
- `FIFO_CTRL3`: 가속도 BDR(batch data rate) 설정 → 단계별 다운샘플 가능.
- `FIFO_CTRL4`: 모드 = `IIS3DWB_FIFO_CONTINUOUS_WTM`(0x03, 이미 정의됨).
- `FIFO_STATUS1/2`: 현재 적재된 샘플 수(DIFF_FIFO) 읽기.
- `FIFO_DATA_OUT_TAG`(0x78~0x7E): 태그(1B) + X/Y/Z(6B) = **샘플당 7B**.

### 5.2 읽기 전략 (1차)
- `iis3dwb_read_registers()`(이미 존재)로 **여러 샘플을 한 번의 SPI 버스트**로 읽음.
- `sensor_task_fifo()`: 짧은 주기로 FIFO_STATUS 확인 → 쌓인 만큼 버스트 읽기 → 태그 제외하고 X/Y/Z만 링버퍼에 적재.
- 폴링형 FIFO 체크로 시작(INT 핀 불필요). 26.6kHz에서 부족하면 watermark INT로 2차 개선.

### 5.3 분기 (sensor_streamer_start)
```c
if (rate_step == 0) xTaskCreate(sensor_task_polling, ...);   // 기존
else                xTaskCreate(sensor_task_fifo, ...);      // 신규
```

## 6. 성공 기준 (Check)

- [ ] rate_step=0 → 기존 폴링 동작 그대로 (회귀 없음, 1kHz/드롭0)
- [ ] rate_step=2(6.6kHz) → 라즈베리파이 실효 레이트 ≈ 6.6kHz (±10%)
- [ ] rate_step=4(26.6kHz) → 실효 레이트가 1kHz보다 명백히 높음 (한계 측정·기록)
- [ ] FIFO 오버런(적재 초과) 시 드롭 통계로 가시화
- [ ] 장시간(수 분) 전송 시 크래시 없음

## 7. 리스크

| 리스크 | 대응 |
|--------|------|
| 26.6kHz × 7B = ~186KB/s, WiFi/UDP 처리량 한계 | 단계별 측정. 한계 레이트를 정직하게 문서화(3단계 안정화) |
| FIFO 태그 파싱 오류 → XYZ 어긋남 | 데이터시트 태그 포맷 확인 + 초기 소량 덤프 검증 |
| 버스트 SPI 길이/DMA 한계 | 한 번에 읽는 샘플 수 상한 설정 (예: 64~128샘플) |
| 폴링 경로 회귀 | 분기로 격리, rate_step=0 경로 코드 미변경 |

## 8. 구현 순서 (Do)

1. 드라이버: `iis3dwb_fifo_enable` / `iis3dwb_fifo_count` / `iis3dwb_read_fifo`
2. 소량 덤프로 태그·XYZ 정렬 검증 (시리얼 로그)
3. `sensor_task_fifo` + start() 분기 + BDR 매핑
4. 빌드 → 실기기: rate_step 2 → 4 순으로 라즈베리파이 실효 레이트 측정
5. 한계 레이트 측정·기록 (3단계 안정화 입력)
