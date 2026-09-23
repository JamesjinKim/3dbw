#!/usr/bin/env python3
"""포토센서 트리거 — WDAQ EDU 보드 DIN1(GPIO5) 엣지 감시.

    ./trigger.py --watch            현재 레벨과 엣지를 실시간으로 본다 (배선 확인용)
    ./trigger.py --probe            활성 레벨을 판정해 준다

## 핀

WDAQ EDU Board 의 디지털 입력은 J5 커넥터의 DIN1~4 이고 BCM GPIO 로는
**5 / 17 / 27 / 22** 다. "포토센서 1번" 은 그중 첫 번째인 **DIN1 = GPIO5** 다.

## 활성 레벨

**무입력 상태에서 DIN1~4 는 모두 HIGH 로 읽힌다** (실측). 산업용 포토센서는
보통 NPN 오픈컬렉터라 감지 시 신호를 GND 로 끌어내린다. 따라서 기본값은
**LOW 활성(FALLING_EDGE)** 이다.

다만 PNP 출력이나 반전 버퍼가 끼면 반대가 된다. 그래서 활성 레벨은 설정으로
두고, GUI 는 **현재 레벨을 실시간으로 표시**한다 — 배선이 반대면 "대기 중인데
활성으로 표시됨" 으로 즉시 드러난다. `--probe` 로도 판정할 수 있다.

## 폴링이 아니라 엣지 인터럽트

EDU 참조 구현은 20 ms 폴링을 쓰지만, 그 방식은 **20 ms 보다 짧은 펄스를 놓친다.**
라인이 빠르면 제품이 지나가는 시간이 그보다 짧을 수 있다. lgpio 의
`gpio_claim_alert` 는 커널 레벨에서 엣지를 잡아 주고 `gpio_set_debounce_micros`
로 채터링도 함께 처리한다.
"""

import threading
import time

DIN_PINS = {1: 5, 2: 17, 3: 27, 4: 22}      # DIN 번호 → BCM GPIO
DEFAULT_DIN = 1                              # "포토센서 1번"
DEFAULT_DEBOUNCE_MS = 50

try:
    import lgpio
    _LGPIO_ERR = None
except ImportError as e:                     # pragma: no cover
    lgpio = None
    _LGPIO_ERR = str(e)


class PhotoTrigger:
    """포토센서 입력 감시.

    `on_trigger` 는 **lgpio 의 콜백 스레드에서 호출된다.** GUI 는 여기서 위젯을
    만지지 말고 큐에 넣기만 해야 한다.
    """

    def __init__(self, gpio=None, *, din=DEFAULT_DIN, active_low=True,
                 debounce_ms=DEFAULT_DEBOUNCE_MS, on_trigger=None, chip=0):
        self.gpio = DIN_PINS[din] if gpio is None else gpio
        self.din = din
        self.active_low = active_low
        self.debounce_ms = debounce_ms
        self.on_trigger = on_trigger
        self.chip = chip

        self.edges = 0              # 감지 횟수 (디바운스 후)
        self.last_edge = None       # time.monotonic
        self.error = None
        self._h = None
        self._cb = None
        self._lock = threading.Lock()

    # ---------- 수명 ----------
    @property
    def available(self):
        return lgpio is not None

    def start(self):
        """감시 시작. 실패하면 self.error 에 이유를 남기고 False.

        GPIO 를 못 써도 프로그램이 죽지는 않아야 한다 — 수동 시작으로 계속
        쓸 수 있어야 하기 때문이다. 그래서 예외를 올리지 않고 이유만 남긴다.
        """
        if lgpio is None:
            self.error = ("lgpio 를 쓸 수 없습니다 (%s).\n"
                          "설치:  sudo apt install -y python3-lgpio" % _LGPIO_ERR)
            return False
        try:
            self._h = lgpio.gpiochip_open(self.chip)
            edge = lgpio.FALLING_EDGE if self.active_low else lgpio.RISING_EDGE
            # 풀 저항은 유휴 레벨을 활성의 반대쪽으로 잡아, 센서가 빠져 있을 때
            # 엣지가 저절로 발생하지 않게 한다.
            flags = lgpio.SET_PULL_UP if self.active_low else lgpio.SET_PULL_DOWN
            lgpio.gpio_claim_alert(self._h, self.gpio, edge, flags)
            lgpio.gpio_set_debounce_micros(self._h, self.gpio,
                                           int(self.debounce_ms * 1000))
            self._cb = lgpio.callback(self._h, self.gpio, edge, self._on_edge)
            self.error = None
            return True
        except Exception as e:
            self.error = ("GPIO%d 를 열 수 없습니다: %s\n"
                          "· 'gpio' 그룹에 속해 있는지 확인하세요 "
                          "(sudo usermod -aG gpio $USER 후 재로그인)" % (self.gpio, e))
            self._cleanup()
            return False

    def stop(self):
        self._cleanup()

    def _cleanup(self):
        if self._cb is not None:
            try:
                self._cb.cancel()
            except Exception:
                pass
            self._cb = None
        if self._h is not None:
            try:
                lgpio.gpio_free(self._h, self.gpio)
            except Exception:
                pass
            try:
                lgpio.gpiochip_close(self._h)
            except Exception:
                pass
            self._h = None

    # ---------- 콜백 ----------
    def _on_edge(self, chip, gpio, level, timestamp):
        with self._lock:
            self.edges += 1
            self.last_edge = time.monotonic()
        if self.on_trigger:
            try:
                self.on_trigger()
            except Exception:
                # 콜백 예외가 lgpio 스레드를 죽이면 이후 트리거를 전부 놓친다.
                pass

    # ---------- 조회 ----------
    def level(self):
        """현재 원시 레벨 (0/1). 읽을 수 없으면 None."""
        if self._h is None:
            return None
        try:
            return lgpio.gpio_read(self._h, self.gpio)
        except Exception:
            return None

    def is_active(self):
        """지금 포토센서가 '감지' 상태인가. 읽을 수 없으면 None."""
        lv = self.level()
        if lv is None:
            return None
        return (lv == 0) if self.active_low else (lv == 1)

    def describe(self):
        lv = self.level()
        if lv is None:
            return "GPIO%d — 읽을 수 없음" % self.gpio
        return "DIN%d (GPIO%d) %s — %s" % (
            self.din, self.gpio, "LOW" if lv == 0 else "HIGH",
            "감지" if self.is_active() else "대기")


# ===================== 배선 확인 도구 =====================

def probe(chip=0, seconds=10, din=DEFAULT_DIN):
    """일정 시간 레벨을 관찰해 활성 레벨을 추정한다.

    유휴 레벨이 무엇인지 보면 활성은 그 반대다. 포토센서를 한 번 가려 보라고
    안내하고, 변화가 관찰되면 그것으로 확정한다.
    """
    if lgpio is None:
        print("❌ lgpio 를 쓸 수 없습니다: %s" % _LGPIO_ERR)
        return 3
    gpio = DIN_PINS[din]
    h = lgpio.gpiochip_open(chip)
    try:
        lgpio.gpio_claim_input(h, gpio, lgpio.SET_PULL_UP)
        print("DIN%d (GPIO%d) 를 %d초 관찰합니다." % (din, gpio, seconds))
        print("이 동안 **포토센서를 한 번 가렸다 떼세요.**\n")
        seen = {}
        first = lgpio.gpio_read(h, gpio)
        t0 = time.monotonic()
        last = first
        changes = 0
        while time.monotonic() - t0 < seconds:
            lv = lgpio.gpio_read(h, gpio)
            seen[lv] = seen.get(lv, 0) + 1
            if lv != last:
                changes += 1
                print("  %5.1f초  %s → %s"
                      % (time.monotonic() - t0,
                         "HIGH" if last else "LOW", "HIGH" if lv else "LOW"))
                last = lv
            time.sleep(0.002)
        print("")
        total = sum(seen.values())
        for lv in sorted(seen):
            print("  %s 로 읽힌 비율 %5.1f%%"
                  % ("HIGH" if lv else "LOW", 100.0 * seen[lv] / total))
        if changes == 0:
            print("\n변화가 없었습니다. 가능한 원인:")
            print("  · 포토센서가 배선되지 않았다 (무입력은 HIGH 로 읽힌다)")
            print("  · 관찰 중에 센서를 가리지 않았다")
            print("  → 지금 상태로는 활성 레벨을 판정할 수 없습니다.")
            return 1
        # 오래 머무는 쪽이 유휴, 반대가 활성이다
        idle = max(seen, key=lambda k: seen[k])
        print("\n판정: 유휴 %s → **활성 레벨은 %s**"
              % ("HIGH" if idle else "LOW", "LOW" if idle else "HIGH"))
        print("      GUI 의 '활성 레벨' 을 %s 로 두세요."
              % ("LOW" if idle else "HIGH"))
        return 0
    finally:
        try:
            lgpio.gpio_free(h, gpio)
        except Exception:
            pass
        lgpio.gpiochip_close(h)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="포토센서(DIN) 트리거 확인")
    ap.add_argument("--din", type=int, default=DEFAULT_DIN, choices=sorted(DIN_PINS),
                    help="DIN 번호 (기본 1 = GPIO5)")
    ap.add_argument("--active", choices=["low", "high"], default="low",
                    help="활성 레벨 (기본 low)")
    ap.add_argument("--watch", action="store_true", help="레벨·엣지 실시간 표시")
    ap.add_argument("--probe", action="store_true", help="활성 레벨 판정")
    ap.add_argument("--seconds", type=float, default=10, help="--probe 관찰 시간")
    args = ap.parse_args(argv)

    if args.probe:
        return probe(seconds=args.seconds, din=args.din)

    fired = []
    t = PhotoTrigger(din=args.din, active_low=(args.active == "low"),
                     on_trigger=lambda: fired.append(time.monotonic()))
    if not t.start():
        print("❌ %s" % t.error)
        return 3
    print("감시 시작: %s · 활성 %s · 디바운스 %dms"
          % (t.describe(), args.active.upper(), t.debounce_ms))
    print("Ctrl+C 로 종료.\n")
    try:
        n = 0
        while True:
            time.sleep(0.25)
            if len(fired) != n:
                n = len(fired)
                print("  [%s] 트리거 %d회째" % (time.strftime("%H:%M:%S"), n))
            print("\r  현재: %-40s" % t.describe(), end="", flush=True)
    except KeyboardInterrupt:
        print("\n\n엣지 %d회 감지됨." % t.edges)
    finally:
        t.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
