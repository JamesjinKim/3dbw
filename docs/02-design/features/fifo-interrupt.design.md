# Design: FIFO Watermark 하드웨어 인터럽트 (INT1 → IO38)

- **Feature**: fifo-interrupt
- **Plan**: [[fifo-interrupt.plan]]
- **작성일**: 2026-06-08
- **결정**: INT1(IO38)만 사용. **INT2(IO37)는 예비(미사용).**

> ⚠️ **실기기 검증 결과 (2026-06-08):** 코드는 완성·동작하나 **하드웨어 INT 신호 미수신**.
> 센서 INT 설정 정상(INT1_CTRL=0x08, WTM=64, active-high)이고 FIFO 가득(avail=512)인데도
> **IO38·IO37 둘 다 LOW 고정**, ESP32 INT 발생 0회. → 배선 미연결/오결선 추정.
> **조치:** Kconfig 기본값을 `-1`(효율 폴링)로 설정. 효율 폴링 26.6kHz 무손실 재확인(에러0).
> INT 코드는 보존 — **HW 담당자 배선 확인 후 `IIS3DWB_INT1_GPIO=38`로 바꾸면 즉시 인터럽트 동작.**
>
> **HW 확인 요청:** ① 센서 INT1 핀이 실제로 ESP32 IO38에 연결됐는지 ② 단선/오결선 여부
> (FIFO 가득 상태에서 센서 INT1 핀 전압이 HIGH로 뜨는지 멀티미터 측정).

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
   ▼ INT1 핀 HIGH
[ESP32 IO38] ─GPIO ISR→ xSemaphoreGiveFromISR(sem)
                              │
[sensor_task_fifo] xSemaphoreTake(sem) ─깨어남→ fifo_count→read_fifo→데시메이션→링버퍼
   │ (INT 안 오면 timeout → 효율폴링 fallback)
   ▼
[링버퍼] → [tx_task] → UDP → 라즈베리파이   (무변경)
```

핵심: **ISR은 세마포어 give만** 한다(SPI 호출 금지). 실제 FIFO 읽기는 태스크에서.

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
    default 38           # HW 핀맵: INT1→IO38
    range -1 48          # -1 = INT 미사용(효율폴링)
```

### 4.2 GPIO ISR 설정 (start 시)
```c
static SemaphoreHandle_t s_fifo_sem;

static void IRAM_ATTR fifo_isr(void *arg) {
    BaseType_t hpw = pdFALSE;
    xSemaphoreGiveFromISR(s_fifo_sem, &hpw);
    if (hpw) portYIELD_FROM_ISR();
}

// start()에서 (rate_step>=1 && INT1_GPIO>=0 일 때):
s_fifo_sem = xSemaphoreCreateBinary();
gpio_config_t io = { .pin_bit_mask=1ULL<<INT1_GPIO, .mode=GPIO_MODE_INPUT,
                     .intr_type=GPIO_INTR_POSEDGE, .pull_down_en=1 };
gpio_config(&io);
gpio_install_isr_service(0);              // 이미 설치돼 있으면 무시
gpio_isr_handler_add(INT1_GPIO, fifo_isr, NULL);
iis3dwb_fifo_set_watermark(sensor, STREAM_WTM_SAMPLES);  // 64
iis3dwb_fifo_route_int1(sensor, true);
```

### 4.3 sensor_task_fifo 대기 방식 교체
```c
while (s.running) {
    // INT 대기 (최대 timeout). INT 오면 즉시, 안 오면 fallback 폴링.
    bool got_int = (s.int_enabled &&
        xSemaphoreTake(s_fifo_sem, pdMS_TO_TICKS(FIFO_INT_TIMEOUT_MS)) == pdTRUE);
    if (!s.int_enabled) vTaskDelay(wait);   // INT 미사용 → 기존 효율폴링

    uint16_t avail = 0;
    iis3dwb_fifo_count(...);
    ... (기존 버스트 읽기 + 데시메이션, 변경 없음) ...

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
- 폴링 경로(rate_step=0) **무변경**.
- INT2(IO37): 설정·배선 안 함. 향후 예비.

## 6. 검증 기준 (Check)

| 항목 | 기준 |
|------|------|
| INT 발생 | INT 카운터 로그로 IO38 ISR 호출 확인 (초당 ~400회 @64WTM/26.6kHz) |
| 26.6kHz 무손실 | rate_step=4, 드롭0/에러0 |
| CPU 개선 | idle 태스크 비율이 효율폴링 대비 동등↑ (fifo_count 폴링 제거 효과) |
| fallback | INT 핀 분리 시뮬레이션(GPIO -1) → 폴링으로 계속 송신 |
| 회귀 | rate_step=0 폴링 1kHz 정상 |

## 7. 리스크 & 대응

| 리스크 | 대응 |
|--------|------|
| IO38 배선이 핀맵과 다름 | INT 카운터=0 이면 fallback 자동 동작 + 경고 로그 |
| WTM 너무 작아 INT 폭주 | 64샘플(≈2.4ms→초당 ~416 INT)부터, 필요시 128로 |
| ISR 우선순위/IRAM | IRAM_ATTR + give만 수행, SPI는 태스크 |
| gpio_install_isr_service 중복 | 반환값 ESP_ERR_INVALID_STATE 무시 |

## 8. 구현 순서 (Do)

1. 드라이버: `fifo_set_watermark` + `fifo_route_int1` + 상수
2. Kconfig: `IIS3DWB_INT1_GPIO`(기본 38)
3. 스트리머: 세마포어+ISR+GPIO 설정, 대기 방식 교체, fallback
4. INT 카운터 진단 로그로 IO38 배선·발생 검증 (가장 중요)
5. 실기기: 26.6kHz INT 무손실 + CPU 사용률 비교, fallback 테스트
6. 진단 로그 제거, 문서 갱신
