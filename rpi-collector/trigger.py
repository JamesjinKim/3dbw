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
라인이 빠르면 제품이 지나가는 시간이 그보다 짧을 수 있다. 커널이 엣지를 잡아 주고
디바운스(채터링 제거)도 커널에서 한다.

## GPIO 백엔드

1. **cdev** (기본) — `gpio_cdev.py`. 커널 GPIO 문자 장치를 순수 파이썬으로 쓴다.
   의존성이 없어 폐쇄망 현장에 그대로 들고 갈 수 있다. 커널 5.10 이상.
2. **lgpio** (예비) — 커널이 오래돼 cdev 가 `Unsupported` 일 때만, 설치돼 있으면 쓴다.
"""

import threading
import time

DIN_PINS = {1: 5, 2: 17, 3: 27, 4: 22}      # DIN 번호 → BCM GPIO
DEFAULT_DIN = 1                              # "포토센서 1번"
DEFAULT_DEBOUNCE_MS = 50

import vendor_path  # noqa: F401
import gpio_cdev

try:
    import lgpio
    _LGPIO_ERR = None
except ImportError as e:                     # pragma: no cover
    lgpio = None
    _LGPIO_ERR = str(e)


# ===================== 백엔드 =====================
# 둘 다 같은 모양: open(gpio, edge, pull, debounce_us, callback) → 핸들,
# 핸들.read() → 0/1, 핸들.close(). edge 는 None/"falling"/"rising",
# pull 은 "up"/"down". callback 은 인자 없이 백엔드 스레드에서 불린다.

class _CdevHandle:
    name = "cdev"

    def __init__(self, gpio, edge, pull, debounce_us, callback):
        chip = gpio_cdev.find_header_chip()
        if chip is None:
            raise OSError("GPIO 장치(/dev/gpiochip*)가 없습니다")
        self.line = gpio_cdev.Line(chip, gpio, edge=edge, bias="pull_" + pull,
                                   debounce_us=debounce_us)
        self.watcher = None
        if edge and callback:
            self.watcher = gpio_cdev.EdgeWatcher(self.line, lambda k, ts: callback())

    def read(self):
        return self.line.read()

    def close(self):
        if self.watcher is not None:
            self.watcher.cancel()
            self.watcher = None
        self.line.close()


class _LgpioHandle:
    name = "lgpio"

    def __init__(self, gpio, edge, pull, debounce_us, callback):
        self.gpio = gpio
        self.cb = None
        self.h = lgpio.gpiochip_open(0)
        try:
            flags = lgpio.SET_PULL_UP if pull == "up" else lgpio.SET_PULL_DOWN
            if edge:
                e = lgpio.FALLING_EDGE if edge == "falling" else lgpio.RISING_EDGE
                lgpio.gpio_claim_alert(self.h, gpio, e, flags)
                lgpio.gpio_set_debounce_micros(self.h, gpio, debounce_us)
                if callback:
                    self.cb = lgpio.callback(self.h, gpio, e,
                                             lambda *_: callback())
            else:
                lgpio.gpio_claim_input(self.h, gpio, flags)
        except Exception:
            self.close()
            raise

    def read(self):
        return lgpio.gpio_read(self.h, self.gpio)

    def close(self):
        if self.cb is not None:
            try:
                self.cb.cancel()
            except Exception:
                pass
            self.cb = None
        if self.h is not None:
            try:
                lgpio.gpio_free(self.h, self.gpio)
            except Exception:
                pass
            try:
                lgpio.gpiochip_close(self.h)
            except Exception:
                pass
            self.h = None


def open_input(gpio, *, edge=None, pull="up", debounce_us=0, callback=None):
    """입력 핀을 연다. cdev 가 기본, 커널이 cdev v2 를 모르면 lgpio 로 물러선다.

    권한 오류 같은 일반 실패는 lgpio 로 넘기지 않는다 — 같은 이유로 또 실패하고,
    진짜 원인(권한)이 lgpio 의 오류 메시지에 가려진다.
    """
    try:
        return _CdevHandle(gpio, edge, pull, debounce_us, callback)
    except gpio_cdev.Unsupported as e:
        if lgpio is None:
            raise OSError("%s. lgpio 도 없습니다 (%s)" % (e.strerror, _LGPIO_ERR))
        return _LgpioHandle(gpio, edge, pull, debounce_us, callback)


class PhotoTrigger:
    """포토센서 입력 감시.

    `on_trigger` 는 **GPIO 백엔드의 감시 스레드에서 호출된다.** GUI 는 여기서
    위젯을 만지지 말고 큐에 넣기만 해야 한다.
    """

    def __init__(self, gpio=None, *, din=DEFAULT_DIN, active_low=True,
                 debounce_ms=DEFAULT_DEBOUNCE_MS, on_trigger=None):
        self.gpio = DIN_PINS[din] if gpio is None else gpio
        self.din = din
        self.active_low = active_low
        self.debounce_ms = debounce_ms
        self.on_trigger = on_trigger

        self.edges = 0              # 감지 횟수 (디바운스 후)
        self.last_edge = None       # time.monotonic
        self.error = None
        self._h = None
        self._lock = threading.Lock()

    # ---------- 수명 ----------
    @property
    def backend(self):
        """지금 쓰는 GPIO 백엔드 이름 ('cdev' / 'lgpio'). 열려 있지 않으면 None."""
        return self._h.name if self._h is not None else None

    def start(self):
        """감시 시작. 실패하면 self.error 에 이유를 남기고 False.

        GPIO 를 못 써도 프로그램이 죽지는 않아야 한다 — 수동 시작으로 계속
        쓸 수 있어야 하기 때문이다. 그래서 예외를 올리지 않고 이유만 남긴다.
        """
        try:
            # 풀 저항은 유휴 레벨을 활성의 반대쪽으로 잡아, 센서가 빠져 있을 때
            # 엣지가 저절로 발생하지 않게 한다.
            self._h = open_input(
                self.gpio,
                edge="falling" if self.active_low else "rising",
                pull="up" if self.active_low else "down",
                debounce_us=int(self.debounce_ms * 1000),
                callback=self._on_edge)
            self.error = None
            return True
        except Exception as e:
            self.error = ("GPIO%d 를 열 수 없습니다: %s\n"
                          "· 'gpio' 그룹에 속해 있는지 확인하세요 "
                          "(bash run.sh 로 실행 · SSH 면 그 뒤 다시 접속)\n"
                          "· 포토센서 자동 수집은 불가하고, 수동 시작은 쓸 수 있습니다."
                          % (self.gpio, e))
            self._cleanup()
            return False

    def stop(self):
        self._cleanup()

    def _cleanup(self):
        if self._h is not None:
            try:
                self._h.close()
            except Exception:
                pass
            self._h = None

    # ---------- 콜백 ----------
    def _on_edge(self, *_):
        with self._lock:
            self.edges += 1
            self.last_edge = time.monotonic()
        if self.on_trigger:
            try:
                self.on_trigger()
            except Exception:
                # 콜백 예외가 감시 스레드를 죽이면 이후 트리거를 전부 놓친다.
                pass

    # ---------- 조회 ----------
    def level(self):
        """현재 원시 레벨 (0/1). 읽을 수 없으면 None."""
        if self._h is None:
            return None
        try:
            return self._h.read()
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

def probe(seconds=10, din=DEFAULT_DIN):
    """일정 시간 레벨을 관찰해 활성 레벨을 추정한다.

    유휴 레벨이 무엇인지 보면 활성은 그 반대다. 포토센서를 한 번 가려 보라고
    안내하고, 변화가 관찰되면 그것으로 확정한다.
    """
    gpio = DIN_PINS[din]
    try:
        h = open_input(gpio, pull="up")
    except Exception as e:
        print("❌ GPIO%d 를 열 수 없습니다: %s" % (gpio, e))
        return 3
    try:
        print("DIN%d (GPIO%d) 를 %d초 관찰합니다. (GPIO 백엔드: %s)"
              % (din, gpio, seconds, h.name))
        print("이 동안 **포토센서를 한 번 가렸다 떼세요.**\n")
        seen = {}
        first = h.read()
        t0 = time.monotonic()
        last = first
        changes = 0
        while time.monotonic() - t0 < seconds:
            lv = h.read()
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
        h.close()


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
    print("감시 시작: %s · 활성 %s · 디바운스 %dms · GPIO 백엔드 %s"
          % (t.describe(), args.active.upper(), t.debounce_ms, t.backend))
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
