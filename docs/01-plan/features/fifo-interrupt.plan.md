# Plan: FIFO Watermark 하드웨어 인터럽트 (INT1 → IO38)

- **Feature**: fifo-interrupt
- **작성일**: 2026-06-08
- **상태**: Plan
- **관련**: [[high-speed-fifo]] (FIFO 효율폴링, 검증완료), [[wifi-sensor-streaming]]
- **계기**: 하드웨어 담당자 핀맵 제공 — **INT1=IO38, INT2=IO37** (이전엔 미연결로 알고 있었음)

## 1. 배경

- 기존 고속 스트리밍은 **FIFO + Watermark 효율 폴링**(INT 핀 미연결 가정)으로 26.6kHz 달성.
- 이번에 HW 담당자가 INT 핀맵 제공: **INT1→IO38, INT2→IO37 실제 연결됨.**
- 가용성 검토 완료: IO38/IO37 모두 충돌 없음, strapping 아님, **PSRAM 미사용**이라 GPIO33~37도 자유.
  → **INT1(IO38)로 하드웨어 인터럽트 구현 가능.**

## 2. 목표

FIFO watermark 도달 시 **센서가 INT1(IO38)으로 신호 → ESP32 ISR이 깨움** 방식으로 전환하여
CPU 부하를 최소화한다 (현장 요구: 폴링보다 저부하).

- `fifo_count` 주기 폴링 제거 → INT가 올 때만 깨어나 묶음 읽기.
- 26.6kHz 무손실 유지 (기존 성능 보존 또는 향상).

## 3. 범위 (In Scope)

| # | 요구사항 | 영역 |
|---|----------|------|
| FR-1 | INT1_CTRL(0x0D) INT1_FIFO_TH=1 설정 (watermark→INT1 라우팅) | iis3dwb 드라이버 |
| FR-2 | FIFO_CTRL1/2 WTM 임계값 설정 (예: 64샘플) | iis3dwb 드라이버 |
| FR-3 | IO38 GPIO 입력+ISR 등록, ISR이 세마포어 give | sensor_streamer |
| FR-4 | sensor_task_fifo: vTaskDelay → xSemaphoreTake 대기로 교체 | sensor_streamer |
| FR-5 | INT 핀 GPIO 번호 Kconfig 설정 (기본 38) | Kconfig |
| FR-6 | INT 실패/미연결 시 효율폴링으로 자동 fallback | sensor_streamer |

## 4. 범위 외

- 폴링 경로(rate_step=0, 1kHz) — 무변경
- 데시메이션 로직 — 기존 유지
- INT2(IO37) — 1차는 INT1만, INT2는 예비
- 패킷/전송/수신기 — 무변경

## 5. 핵심 설계 포인트

- **세마포어 기반**: 기존 설계에 "향후 vTaskDelay→세마포어 교체로 인터럽트 전환 가능"으로 이미 준비됨.
- **WTM 임계**: 너무 작으면 INT 폭주(부하↑), 너무 크면 지연↑/오버런. 64샘플(≈2.4ms)부터 튜닝.
- **fallback 안전장치**: INT가 일정 시간 안 오면(미배선/오설정) 효율폴링으로 자동 전환 → 회귀 방지.
- **gpio_install_isr_service** + `gpio_isr_handler_add(IO38, ...)`, IRAM ISR.

## 6. 성공 기준

- [ ] rate_step=4: INT 방식으로 26.6kHz 무손실 (드롭0)
- [ ] `fifo_count` 주기 폴링 제거 확인 (INT 이벤트로만 깨어남)
- [ ] CPU 사용률이 효율폴링 대비 동등 이상으로 개선 (idle 태스크 비율로 측정)
- [ ] INT 미배선 환경에서 fallback 동작 (폴링으로 계속 송신)
- [ ] 폴링 경로(rate_step=0) 회귀 없음

## 7. 리스크

| 리스크 | 대응 |
|--------|------|
| IO38 실제 배선이 핀맵과 다름 | 첫 검증: INT 카운터 로그로 INT 발생 확인. 안 오면 fallback |
| WTM/INT 설정 오류로 INT 안 옴 | 레지스터 되읽기 + INT 카운터 진단 |
| ISR 내 SPI 호출 금지 | ISR은 세마포어 give만, SPI 읽기는 태스크에서 |
| 향후 PSRAM 켜면 IO37 점유 | INT1(IO38) 사용으로 회피 (33~37 범위 밖) |

## 8. 구현 순서 (Do)

1. 드라이버: WTM 설정 + INT1_FIFO_TH 라우팅 함수
2. IO38 GPIO ISR + 세마포어, sensor_task_fifo 대기 방식 교체
3. INT 발생 카운터 진단 로그로 배선·동작 검증
4. 효율폴링 fallback 추가
5. 실기기: 26.6kHz INT 방식 무손실 확인 + CPU 사용률 비교
