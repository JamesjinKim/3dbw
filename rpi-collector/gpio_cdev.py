"""리눅스 GPIO 문자 장치(v2 uAPI)를 순수 파이썬으로 쓴다 — lgpio 대체.

## 왜 lgpio 가 아닌가

현장 RPi 는 폐쇄망이고 OS 버전·32/64비트를 모른다. lgpio 는 C 확장이라 패키지에
넣으려면 (Python 버전 × 아키텍처 × glibc) 조합마다 빌드해야 하고, PyPI 휠도
64비트 bookworm 에서만 단독으로 돈다. 반면 lgpio 가 내부에서 쓰는 것은 결국 커널의
/dev/gpiochipN ioctl 이다. 이것을 파이썬 표준 라이브러리(fcntl·struct)로 직접 부르면
**의존성이 0** 이 되고 OS·아키텍처·Python 버전과 무관해진다.

필요 조건은 커널 5.10 이상(v2 uAPI + 커널 디바운스). Raspberry Pi OS bullseye 부터
해당한다. 그보다 오래된 커널에서는 요청이 ENOTTY/EINVAL 로 실패하므로
`Unsupported` 를 올리고, 호출 쪽(trigger.py)이 lgpio 로 물러선다.

## 구조체 (include/uapi/linux/gpio.h)

모든 u64 가 8바이트 정렬이라 32/64비트에서 크기가 같다 — 그래서 한 코드로 둘 다 된다.
  gpio_v2_line_attribute         16 B  {u32 id, u32 pad, u64 value}
  gpio_v2_line_config_attribute  24 B  {attribute, u64 mask}
  gpio_v2_line_config           272 B  {u64 flags, u32 num_attrs, u32 pad[5], attrs[10]}
  gpio_v2_line_request          592 B  {u32 offsets[64], char consumer[32], config,
                                         u32 num_lines, u32 event_buffer_size,
                                         u32 pad[5], s32 fd}
  gpio_v2_line_values            16 B  {u64 bits, u64 mask}
  gpio_v2_line_event             48 B  {u64 timestamp_ns, u32 id, u32 offset,
                                         u32 seqno, u32 line_seqno, u32 pad[6]}
"""

import errno
import fcntl
import glob
import os
import select
import struct
import threading


def _iowr(nr, size):
    return (3 << 30) | (size << 16) | (0xB4 << 8) | nr


_REQ_SIZE, _CFG_SIZE, _VAL_SIZE, _EVT_SIZE = 592, 272, 16, 48
_GET_LINE = _iowr(0x07, _REQ_SIZE)
_SET_CONFIG = _iowr(0x0D, _CFG_SIZE)
_GET_VALUES = _iowr(0x0E, _VAL_SIZE)

_F_INPUT = 1 << 2
_F_EDGE_RISING = 1 << 4
_F_EDGE_FALLING = 1 << 5
_F_BIAS_PULL_UP = 1 << 8
_F_BIAS_PULL_DOWN = 1 << 9
_F_BIAS_DISABLED = 1 << 10
_ATTR_DEBOUNCE = 3

RISING, FALLING = 1, 2          # gpio_v2_line_event.id

_EDGE = {None: 0, "rising": _F_EDGE_RISING, "falling": _F_EDGE_FALLING,
         "both": _F_EDGE_RISING | _F_EDGE_FALLING}
_BIAS = {None: 0, "pull_up": _F_BIAS_PULL_UP, "pull_down": _F_BIAS_PULL_DOWN,
         "disabled": _F_BIAS_DISABLED}

# 라즈베리파이 40핀 헤더를 가진 칩의 라벨 (4 이하 / 5)
_HEADER_LABELS = ("pinctrl-bcm2711", "pinctrl-bcm2835", "pinctrl-rp1")


class Unsupported(OSError):
    """이 커널이 GPIO v2 uAPI 를 지원하지 않는다 (5.10 미만)."""


def _chip_label(path):
    """칩 라벨 (예: 'pinctrl-bcm2711'). GPIO_GET_CHIPINFO_IOCTL 로 커널에 직접 묻는다.

    sysfs 의 label 파일 위치는 커널 버전마다 달라 믿을 수 없다. chipinfo ioctl 은
    문자 장치가 생긴 4.8 부터 그대로다. {char name[32], char label[32], u32 lines}
    """
    buf = bytearray(68)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        try:
            fcntl.ioctl(fd, (2 << 30) | (68 << 16) | (0xB4 << 8) | 0x01, buf, True)
        finally:
            os.close(fd)
    except OSError:
        return ""
    return bytes(buf[32:64]).split(b"\0", 1)[0].decode(errors="replace")


def find_header_chip():
    """40핀 헤더 GPIO 를 가진 /dev/gpiochipN 을 찾는다. 없으면 None.

    번호는 모델·커널마다 다르다 (RPi4 는 gpiochip0, RPi5 는 커널에 따라 0 또는 4).
    그래서 번호가 아니라 라벨로 찾는다. 심볼릭 링크(gpiochip4 → gpiochip0)는 건너뛴다.
    """
    chips = sorted(p for p in glob.glob("/dev/gpiochip*") if not os.path.islink(p))
    for p in chips:
        if _chip_label(p) in _HEADER_LABELS:
            return p
    return chips[0] if chips else None


def _config(flags, debounce_us):
    n = 0
    attrs = b""
    if debounce_us:
        attrs += struct.pack("<IIQQ", _ATTR_DEBOUNCE, 0, int(debounce_us), 1)
        n = 1
    attrs += b"\0" * (24 * (10 - n))
    return struct.pack("<QI5I", flags, n, 0, 0, 0, 0, 0) + attrs


class Line:
    """입력 라인 하나. `with` 로 쓰거나 close() 로 닫는다."""

    def __init__(self, chip, offset, *, edge=None, bias=None, debounce_us=0,
                 consumer="iis3dwb"):
        self.chip = chip
        self.offset = offset
        self.fd = None
        flags = _F_INPUT | _EDGE[edge] | _BIAS[bias]
        req = bytearray(
            struct.pack("<64I", offset, *([0] * 63))
            + consumer.encode()[:31].ljust(32, b"\0")
            + _config(flags, debounce_us)
            + struct.pack("<II5Ii", 1, 0, 0, 0, 0, 0, 0, 0))
        cfd = os.open(chip, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        try:
            fcntl.ioctl(cfd, _GET_LINE, req, True)
        except OSError as e:
            if e.errno in (errno.ENOTTY, errno.EINVAL):
                raise Unsupported(e.errno, "이 커널은 GPIO v2 uAPI 를 지원하지 않습니다 "
                                           "(커널 5.10 이상 필요)") from e
            raise
        finally:
            os.close(cfd)
        self.fd = struct.unpack_from("<i", req, _REQ_SIZE - 4)[0]

    def read(self):
        """현재 원시 레벨 0/1."""
        buf = bytearray(struct.pack("<QQ", 0, 1))
        fcntl.ioctl(self.fd, _GET_VALUES, buf, True)
        return struct.unpack("<QQ", buf)[0] & 1

    def reconfigure(self, *, edge=None, bias=None, debounce_us=0):
        flags = _F_INPUT | _EDGE[edge] | _BIAS[bias]
        fcntl.ioctl(self.fd, _SET_CONFIG, bytearray(_config(flags, debounce_us)), True)

    def wait_events(self, timeout):
        """엣지 이벤트를 기다린다. [(RISING|FALLING, timestamp_ns), ...] — 없으면 []."""
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return []
        data = os.read(self.fd, _EVT_SIZE * 16)
        out = []
        for i in range(0, len(data) - _EVT_SIZE + 1, _EVT_SIZE):
            ts, eid = struct.unpack_from("<QI", data, i)
            out.append((eid, ts))
        return out

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class EdgeWatcher:
    """라인의 엣지 이벤트를 전용 스레드에서 받아 callback(kind, timestamp_ns) 을 부른다.

    lgpio.callback 과 같은 역할이다. callback 은 **이 스레드에서** 불린다.
    """

    POLL_S = 0.2        # stop() 이 반영되기까지의 최대 지연

    def __init__(self, line, callback):
        self.line = line
        self.callback = callback
        self._stop = threading.Event()
        self._th = threading.Thread(target=self._run, daemon=True,
                                    name="gpio-edge-%d" % line.offset)
        self._th.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                events = self.line.wait_events(self.POLL_S)
            except (OSError, ValueError):
                return              # 라인이 닫혔다
            for kind, ts in events:
                self.callback(kind, ts)

    def cancel(self):
        self._stop.set()
        if self._th is not threading.current_thread():
            self._th.join(timeout=2 * self.POLL_S + 1)
