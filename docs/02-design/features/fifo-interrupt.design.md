# Design: FIFO Watermark 하드웨어 인터럽트 (INT1 → IO4)

- **Feature**: fifo-interrupt
- **Plan**: [[fifo-interrupt.plan]]
- **작성일**: 2026-06-08
- **결정**: INT1(IO4)만 사용. **INT2(IO5)는 예비(미사용).**

> ✅ **실기기 검증 성공 (2026-06-08):** 하드웨어 인터럽트 26.6kHz 무손실 달성.
> `패킷=1339 샘플=267800 드롭=0 에러=0 INT=4090` (10초) → **실효 26,780Hz, INT 초당 ~409회**.
>
> **해결한 두 가지 문제:**
> 1. **핀맵 오류** — INT1은 IO38이 아니라 **IO4** (HW 담당자 확정 핀맵: INT1→IO4, INT2→IO5).
>    IO38/IO37로는 신호가 잡히지 않았음.
> 2. **트리거 방식** — FIFO watermark INT는 **레벨 신호**(WTM 이상 채워진 동안 HIGH 유지)라
>    POSEDGE/ANYEDGE로는 최초 1회만 발동(`INT=1`에서 멈춤). → **GPIO_INTR_HIGH_LEVEL**로 바꾸고
>    **ISR에서 INT를 끈 뒤 → 태스크가 FIFO를 비우고 → INT를 재활성화**하는 패턴으로 해결.
>
> 사용자 선택(read_mode, NVS)으로 폴링/인터럽트 전환 가능하며, INT 미수신 시 효율폴링으로 자동 fallback.

## 1. 데이터시트 확정 사실

| 항목 | 값 | 근거 |
|------|-----|------|
| INT1_CTRL 레지스터 | 0x0D | Table 23 |
| **INT1_FIFO_TH 비트** | **bit 3** (`1<<3`) | Table 23 비트 배치 |
| INT1_FIFO_OVR 비트 | bit 4 (`1<<4`) | 오버런 알림(진단용 선택) |
| WTM 임계값 | FIFO_CTRL1=WTM[7:0], FIFO_CTRL2 bit0=WTM8 → **WTM[8:0]** | Table 11~14 |
| WTM 단위 | **1 LSB = 1샘플** (TAG+6B 워드 1개) | Table 12 "1 sensor(6B)+TAG(1B)" |
| 동작 | "쌓인 샘플 수 ≥ WTM" 이면 FIFO_WTM_IA 플래그 → INT1 | §4.3.3 |

> INT1_CTRL 비트맵(MSB→LSB): `0 · CNT_BDR · FIFO_FULL · FIFO_OVR · FIFO_TH · BOOT · 0 · DRDY_XL`

## 2. 아키텍처 — 세마포어 기반 ISR

```
[IIS3DWB] FIFO 26.6kHz 적재
   │ 쌓인 샘플 ≥ WTM(64)
   ▼ INT1 핀 HIGH (레벨 유지)
[ESP32 IO4] ─GPIO ISR(HIGH_LEVEL)→ ① INT 비활성화 ② xSemaphoreGiveFromISR(sem)
                              │
[sensor_task_fifo] xSemaphoreTake(sem) ─깨어남→ fifo_count→read_fifo→데시메이션→링버퍼
   │                           └─ FIFO 비운 뒤 ③ gpio_intr_enable() 재활성화
   │ (INT 안 오면 timeout → 효율폴링 fallback)
   ▼
[링버퍼] → [tx_task] → UDP → 라즈베리파이   (무변경)
```

핵심 ①: **ISR은 INT 비활성화 + 세마포어 give만** 한다(SPI 호출 금지). 실제 FIFO 읽기는 태스크에서.
핵심 ②: watermark INT는 **레벨 신호**이므로 ISR에서 INT를 꺼야 폭주를 막고, FIFO를 비운 뒤
태스크가 다시 켜야 다음 채움에 재발동한다. (POSEDGE로는 최초 1회만 발동 — 검증 중 발견한 핵심 함정.)

## 3. 드라이버 API 추가 (iis3dwb.h/.c)

### 3.1 WTM 설정
```c
/** FIFO watermark 임계값(샘플 수) 설정. wtm: 1~511 */
esp_err_t iis3dwb_fifo_set_watermark(iis3dwb_handle_t *h, uint16_t wtm);
```
- FIFO_CTRL1 = wtm & 0xFF
- FIFO_CTRL2 = (FIFO_CTRL2 & ~0x01) | ((wtm >> 8) & 0x01)  // WTM8 보존적 쓰기

### 3.2 INT1 라우팅
```c
/** FIFO watermark 인터럽트를 INT1 핀으로 라우팅 (INT1_FIFO_TH=1) */
esp_err_t iis3dwb_fifo_route_int1(iis3dwb_handle_t *h, bool enable);
```
- INT1_CTRL bit3 set/clear. (선택) bit4(OVR)도 함께 켜 오버런 가시화 가능.

### 3.3 상수 (헤더)
```c
#define IIS3DWB_REG_INT1_CTRL   0x0D   // 이미 존재
#define IIS3DWB_INT1_FIFO_TH    (1<<3)
#define IIS3DWB_INT1_FIFO_OVR   (1<<4)
```

## 4. 스트리머 변경 (sensor_streamer.c)

### 4.1 Kconfig
```
config IIS3DWB_INT1_GPIO
    int "IIS3DWB INT1 GPIO (FIFO watermark 인터럽트)"
    default 4            # HW 핀맵 확정: INT1→IO4 (실기기 검증 완료)
    range -1 48          # -1 = INT 미사용(효율폴링)
```

### 4.2 GPIO ISR 설정 (start 시)
```c
// ISR: INT 비활성화 + 세마포어 give만. (watermark INT는 레벨 신호 → 폭주 방지 위해 끈다)
static void IRAM_ATTR fifo_isr(void *arg) {
    gpio_intr_disable((gpio_num_t)(intptr_t)arg);
    s.int_count++;
    BaseType_t hpw = pdFALSE;
    xSemaphoreGiveFromISR(s.fifo_sem, &hpw);
    if (hpw) portYIELD_FROM_ISR();
}

// start()에서 (INT1_GPIO>=0 && read_mode==1 일 때):
s.fifo_sem = xSemaphoreCreateBinary();
gpio_config_t io = { .pin_bit_mask=1ULL<<INT1_GPIO, .mode=GPIO_MODE_INPUT,
                     .intr_type=GPIO_INTR_HIGH_LEVEL,   // ★ 레벨 트리거 (POSEDGE 아님!)
                     .pull_down_en=GPIO_PULLDOWN_ENABLE };
gpio_config(&io);
gpio_install_isr_service(0);              // 이미 설치돼 있으면 무시
gpio_isr_handler_add(INT1_GPIO, fifo_isr, (void*)(intptr_t)INT1_GPIO);
iis3dwb_fifo_set_watermark(sensor, STREAM_INT_WTM);  // 64
iis3dwb_fifo_route_int1(sensor, true);
```

### 4.3 sensor_task_fifo 대기 방식 교체
```c
while (s.running) {
    // INT 대기 (최대 timeout). INT 오면 즉시, 안 오면 fallback 폴링.
    bool got_int = (s.int_enabled &&
        xSemaphoreTake(s.fifo_sem, pdMS_TO_TICKS(FIFO_INT_TIMEOUT_MS)) == pdTRUE);
    if (!s.int_enabled) vTaskDelay(wait);   // INT 미사용 → 기존 효율폴링

    uint16_t avail = 0;
    iis3dwb_fifo_count(...);
    ... (기존 버스트 읽기 + 데시메이션, 변경 없음) ...

    // ★ FIFO를 비운 뒤 INT 재활성화 — 레벨 신호이므로 다음 채움에 다시 발동시킨다.
    if (s.int_enabled) gpio_intr_enable((gpio_num_t)INT1_GPIO);

    // fallback 감지: INT 모드인데 timeout이 연속 N회 → 폴링으로 강등
    if (s.int_enabled && !got_int && avail == 0) {
        if (++s.int_miss > FIFO_INT_MISS_MAX) { s.int_enabled = false;
            ESP_LOGW(TAG, "INT 미발생 → 효율폴링 fallback"); }
    } else { s.int_miss = 0; }
}
```
- `FIFO_INT_TIMEOUT_MS = 50`, `FIFO_INT_MISS_MAX = 20` (≈1초 미발생 시 fallback).
- 읽기/데시메이션/링버퍼 코드는 **재사용 (변경 없음)**.

## 5. 데이터 정합성 / 무변경 범위

- 패킷·전송·수신기·데시메이션 **무변경**.
- 폴링 경로(read_mode=0) **무변경**.
- INT2(IO5): 설정·배선 안 함. 향후 예비.

## 6. 검증 기준 (Check) — ✅ 전 항목 통과

| 항목 | 기준 | 실측 결과 |
|------|------|-----------|
| INT 발생 | INT 카운터로 IO4 ISR 호출 확인 (초당 ~400회 @64WTM/26.6kHz) | ✅ INT=4090/10s → **초당 ~409회** |
| 26.6kHz 무손실 | rate_step=4, 드롭0/에러0 | ✅ 샘플 267800/10s → **26,780Hz, 드롭0/에러0** |
| CPU 개선 | fifo_count 폴링 제거 (INT 올 때만 깨어남) | ✅ INT 대기 방식으로 동작 |
| fallback | INT 미수신 시 폴링으로 계속 송신 | ✅ read_mode=0 또는 GPIO -1 시 효율폴링 |
| 회귀 | read_mode=0 폴링 정상 | ✅ 정상 |

## 7. 리스크 & 대응

| 리스크 | 대응 | 실제 |
|--------|------|------|
| INT 핀맵이 다름 | INT 카운터=0 이면 fallback 자동 동작 + 경고 로그 | **IO38→IO4 정정으로 해결** |
| watermark INT가 레벨 신호 | POSEDGE는 1회만 발동 → **HIGH_LEVEL + ISR 끄기/태스크 재켜기** | **검증 중 발견, 해결** |
| WTM 너무 작아 INT 폭주 | 64샘플(≈2.4ms→초당 ~409 INT) + ISR에서 INT 비활성화 | ✅ 폭주 없음 |
| ISR 우선순위/IRAM | IRAM_ATTR + give/disable만 수행, SPI는 태스크 | ✅ |
| gpio_install_isr_service 중복 | 반환값 ESP_ERR_INVALID_STATE 무시 | ✅ |

## 8. 구현 순서 (Do) — ✅ 완료

1. ✅ 드라이버: `fifo_set_watermark` + `fifo_route_int1` + 상수
2. ✅ Kconfig: `IIS3DWB_INT1_GPIO`(기본 4)
3. ✅ 스트리머: 세마포어+ISR(HIGH_LEVEL)+GPIO 설정, 대기 방식 교체, fallback
4. ✅ INT 카운터 진단 로그로 IO4 배선·발생 검증 (가장 중요) → 성공
5. ✅ 실기기: 26.6kHz INT 무손실 (267800샘플/10s, 드롭0/에러0), fallback 테스트
6. ⏳ 진단 로그(int_count)는 stats에 보존 (유용), 문서 갱신 완료
