#!/usr/bin/env python3
"""연결된 센서 보드 열거 — USB 물리 슬롯 기준.

    python3 ports.py            연결된 보드 목록 (MAC 포함)
    python3 ports.py --no-mac   MAC 조회 없이 빠르게

## `/dev/serial/by-id` 를 쓰면 안 되는 이유

이 보드의 Cypress CY7C65213 브리지에는 **고유 일련번호가 없다.** 두 대를 꽂아도
by-id 항목은 `usb-Cypress_Semiconductor_USB-UART_LP-if00` 하나뿐이고, 나중에
열거된 쪽이 그 링크를 가져간다.

그래서 by-id 로 열거하면 **후보가 1개로 보여 한 대를 놓치고**, 그 상태에서
"자동 감지" 를 하면 **경고 없이 엉뚱한 보드에 펌웨어를 굽는다.**
(실제로 boardcheck 와 usb_reset 에서 이 문제를 겪었다)

`/dev/serial/by-path` 는 USB 구멍마다 반드시 하나씩 생기므로 이것을 쓴다.

## 보드를 구분하는 유일한 방법은 MAC 이다

슬롯은 "어느 구멍" 을 말해 주지만 "어느 보드" 를 말해 주지 않는다. 보드 자체를
식별하려면 esptool 로 MAC 을 읽어야 한다 (`read_macs`). 이 조회는 보드를
부트로더로 리셋하므로, 스트리밍 중이던 보드는 멈춘다 — 굽기 전 단계에서만 쓴다.
"""

import os
import re
import sys

BY_PATH = "/dev/serial/by-path"

# 센서 보드가 쓰는 USB-시리얼 칩. 마우스 등 다른 CDC 장치를 센서로 오인하지
# 않도록 화이트리스트로 거른다. (rpi-collector/slots.py 와 같은 목록)
KNOWN_BRIDGES = {
    (0x04B4, 0x0003): "Cypress CY7C65213",
    (0x10C4, 0xEA60): "CP210x",
    (0x1A86, 0x7523): "CH340",
    (0x0403, 0x6001): "FTDI FT232",
}
ESPRESSIF_VID = 0x303A

_IFACE_SUFFIX = re.compile(r":\d+\.\d+$")


class Board:
    """감지된 보드 하나."""

    __slots__ = ("device", "slot", "mac", "chip", "error")

    def __init__(self, device, slot, chip=None):
        self.device = device      # /dev/ttyACM0 — 꽂는 순서로 바뀐다
        self.slot = slot          # USB 구멍 경로 — 바뀌지 않는다
        self.chip = chip          # 브리지칩 이름 (있으면)
        self.mac = None           # read_macs() 가 채운다
        self.error = None         # MAC 조회 실패 이유

    @property
    def short_slot(self):
        i = self.slot.rfind("-usb-")
        return self.slot[i + 1:] if i >= 0 else self.slot

    @property
    def short_dev(self):
        return os.path.basename(self.device)

    def __repr__(self):
        return "Board(%s, %s, %s)" % (self.device, self.short_slot, self.mac)


def slot_key(by_path_name):
    """by-path 링크 이름 → 물리 슬롯 키.

    끝의 `:1.0` 은 USB 인터페이스 번호라 같은 장치에서도 여러 개가 생긴다.
    커널이 `usb-` 와 `usbv2-` 두 링크를 함께 만들기도 하므로 정규화한다.
    """
    name = by_path_name.replace("-usbv2-", "-usb-")
    return _IFACE_SUFFIX.sub("", name)


def _bridge_chip(device):
    """pyserial 로 이 장치의 브리지칩 이름을 알아낸다 (모르면 None)."""
    try:
        from serial.tools import list_ports as lp
    except ImportError:
        return None
    for p in lp.comports():
        if p.device != device or p.vid is None:
            continue
        if (p.vid, p.pid) in KNOWN_BRIDGES:
            return KNOWN_BRIDGES[(p.vid, p.pid)]
        if p.vid == ESPRESSIF_VID:
            return "ESP32 내장 USB"
    return None


def list_ports():
    """연결된 보드를 [Board] 로 돌려준다 (슬롯 순 — 물리 배치 순에 가깝다)."""
    found = {}
    if os.path.isdir(BY_PATH):
        for name in sorted(os.listdir(BY_PATH)):
            try:
                dev = os.path.realpath(os.path.join(BY_PATH, name))
            except OSError:
                continue
            found.setdefault(dev, slot_key(name))
    if not found:
        # by-path 가 없는 환경 대비. 라즈베리파이 내장 UART(/dev/ttyAMA*,
        # /dev/serial0)는 센서와 무관하므로 후보에서 뺀다.
        import glob
        for d in sorted(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*")):
            found[d] = "unstable:" + d

    boards = [Board(d, s, _bridge_chip(d)) for d, s in found.items()]
    boards.sort(key=lambda b: natural_key(b.slot))
    return boards


def natural_key(s):
    """문자열 속 숫자를 수로 비교한다 — 'usb-0:1.2' 가 'usb-0:1.10' 보다 앞.

    이 순서가 곧 순차 굽기의 1번·2번 순서다. 문자열로 정렬하면 허브에서
    1.10 이 1.2 보다 먼저 와 번호가 뒤바뀐다.
    """
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


def read_macs(boards, *, on_line=None, on_board=None, recover=True):
    """각 보드의 MAC 을 esptool 로 읽어 채운다 (포트당 약 3초).

    **보드를 부트로더로 리셋한다.** 스트리밍 중이던 보드는 멈추므로, 굽기 전
    준비 단계에서만 호출한다. 한 대가 실패해도 나머지는 계속 읽는다.

    on_board(board, 상태) — 상태는 "checking" / "recovering" / "done".
        GUI 가 한 대 끝날 때마다 목록을 바로 갱신하게 한다. 예전에는 전부 끝난 뒤
        한꺼번에 채워, 기다리는 동안 반응이 없고 뒤 순번은 실패처럼 보였다.
    recover — USB 스톨이면 묻지 않고 USB 리셋 후 한 번 더 읽는다. MAC 주소 확인은
        원래 보드를 리셋하는 동작이라 확인을 받을 이유가 없다. 리셋 뒤 포트
        경로가 바뀌면 board.device 를 새 경로로 고친다.
    """
    import esp_flash

    def say(b, state):
        if on_board:
            on_board(b, state)

    for b in boards:
        if on_line:
            on_line("%s 확인 중..." % b.device)
        say(b, "checking")
        try:
            try:
                info = esp_flash.detect_device(b.device)
            except esp_flash.UsbStallError:
                if not recover:
                    raise
                import usb_reset
                if on_line:
                    on_line("%s USB 멈춤 감지 → 자동 복구 후 다시 읽습니다" % b.device)
                say(b, "recovering")
                b.device = usb_reset.recover(b.device, on_line=on_line)
                info = esp_flash.detect_device(b.device)
            b.mac = (info.get("mac") or "").lower() or None
            b.error = None
            if on_line:     # 문의 대응용 진단 정보 — '로그 저장' 으로 받는다
                on_line("%s 칩 %s · 플래시 %s"
                        % (b.device, info.get("chip"), info.get("flash_size")))
        except Exception as e:
            b.mac = None
            b.error = str(e).splitlines()[0]
        say(b, "done")
    return boards


def find_by_mac(boards, mac):
    """MAC 으로 보드를 찾는다 (대소문자 무시). 없으면 None."""
    if not mac:
        return None
    m = mac.lower()
    for b in boards:
        if b.mac and b.mac.lower() == m:
            return b
    return None


# ===================== CLI =====================

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    boards = list_ports()
    if not boards:
        print("연결된 USB 시리얼 포트가 없습니다.")
        print("  · 보드가 꽂혀 있는지, 충전 전용 케이블이 아닌지 확인하세요")
        return 1

    if "--no-mac" not in argv:
        print("보드 %d대 — MAC 을 읽는 중입니다 (포트당 약 3초)\n" % len(boards))
        read_macs(boards)
    else:
        print("보드 %d대\n" % len(boards))

    print("  %-3s %-12s %-10s %-19s %s"
          % ("#", "USB 포트", "장치", "MAC", "브리지칩"))
    for i, b in enumerate(boards, 1):
        print("  %-3d %-12s %-10s %-19s %s"
              % (i, b.short_slot, b.short_dev,
                 b.mac or ("? (%s)" % b.error[:20] if b.error else "?"),
                 b.chip or "?"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
