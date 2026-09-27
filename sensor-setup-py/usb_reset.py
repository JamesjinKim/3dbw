#!/usr/bin/env python3
"""USB 장치 리셋 — bulk OUT 엔드포인트 스톨 복구 (sudo 불필요)

배경:
    라즈베리파이에서 USB-UART 브리지의 OUT 엔드포인트가 스톨되면
    읽기는 되는데 쓰기만 영구 블로킹된다. esptool 은 "Write timeout" 으로 죽는다.
    cdc_acm 드라이버 unbind/rebind 로는 **복구되지 않는다** (엔드포인트 상태가 남는다).
    USBDEVFS_RESET ioctl 로 장치를 재열거해야 풀린다.

sudo 없이 되는 이유:
    install.sh 가 넣는 udev 규칙(70-iis3dwb.rules)이 /dev/bus/usb/* 를 plugdev 그룹
    0660 으로 만들고, 데스크톱 세션 사용자에게는 uaccess ACL 을 붙인다.
    GUI 가 비밀번호를 묻지 않아야 하므로 이 경로가 중요하다.
    규칙이 없으면 PermissionError 를 올리고 sudo tools/usb-recover.sh 를 안내한다.

대상 선정:
    tools/usb-recover.sh 는 `lsusb | head -1` 방식이라 보드가 두 개 꽂혀 있으면
    엉뚱한 장치를 리셋할 수 있다. 여기서는 **선택된 포트에서** sysfs 를 거슬러
    올라가 정확히 그 브리지만 찾는다.

CLI:
    python3 usb_reset.py info /dev/ttyACM0
    python3 usb_reset.py reset /dev/ttyACM0
"""

import fcntl
import glob
import os
import sys
import time

# linux/usbdevice_fs.h — _IO('U', 20)
USBDEVFS_RESET = ord("U") << 8 | 20


class UsbResetError(Exception):
    def __init__(self, message, hint=None):
        super().__init__(message)
        self.hint = hint

    def full(self):
        return str(self) + (("\n\n" + self.hint) if self.hint else "")


def _read_attr(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def find_bridge(port):
    """포트 경로에서 그 포트를 제공하는 USB 장치를 찾는다.

    /sys/class/tty/ttyACM0/device 에서 시작해, busnum/devnum/idVendor/idProduct 를
    모두 가진 디렉터리(= USB 장치 노드)까지 부모를 거슬러 올라간다.
    """
    dev = os.path.realpath(port)                 # 심볼릭 링크 해소
    name = os.path.basename(dev)
    sys_dev = "/sys/class/tty/%s/device" % name
    if not os.path.exists(sys_dev):
        raise UsbResetError(
            "%s 의 sysfs 정보를 찾을 수 없습니다." % port,
            "· USB 시리얼 포트가 아닐 수 있습니다 (예: 라즈베리파이 내장 UART)")

    cur = os.path.realpath(sys_dev)
    for _ in range(8):                           # 인터페이스 → 장치 → 허브 …
        busnum = _read_attr(os.path.join(cur, "busnum"))
        devnum = _read_attr(os.path.join(cur, "devnum"))
        vid = _read_attr(os.path.join(cur, "idVendor"))
        pid = _read_attr(os.path.join(cur, "idProduct"))
        if busnum and devnum and vid and pid:
            return {
                "bus": int(busnum), "dev": int(devnum),
                "vid": vid.lower(), "pid": pid.lower(),
                "product": _read_attr(os.path.join(cur, "product")),
                "manufacturer": _read_attr(os.path.join(cur, "manufacturer")),
                "node": "/dev/bus/usb/%03d/%03d" % (int(busnum), int(devnum)),
                "sysfs": cur,
            }
        parent = os.path.dirname(cur)
        if parent == cur or not parent.startswith("/sys"):
            break
        cur = parent
    raise UsbResetError("%s 의 USB 장치를 특정할 수 없습니다." % port)


def stable_id(port):
    """포트의 안정 식별자를 돌려준다 (없으면 None).

    USB 리셋 후 ttyACM0 → ttyACM1 로 번호가 바뀔 수 있으므로, 번호가 아닌
    무언가를 기준으로 다시 찾아야 한다.

    **`/dev/serial/by-id/` 를 쓰면 안 된다.** 이 보드의 Cypress 브리지에는 고유
    일련번호가 없어 두 대를 꽂으면 `usb-Cypress_Semiconductor_USB-UART_LP-if00`
    하나만 생기고 나중에 열거된 쪽이 그 링크를 가져간다. 그 상태에서 by-id 로
    되찾으면 **엉뚱한 보드를 복구된 포트로 돌려주고**, 호출자는 그 포트에
    펌웨어를 굽는다. 실제로 보드 2대를 꽂고 겪었다.

    그래서 `/dev/serial/by-path/` — **USB 구멍의 물리 경로** — 를 쓴다.
    리셋해도 같은 구멍이면 같은 경로이고, 보드마다 반드시 다르다.
    """
    target = os.path.realpath(port)
    for link in sorted(glob.glob("/dev/serial/by-path/*")):
        try:
            if os.path.realpath(link) == target:
                return link
        except OSError:
            continue
    return None


def reset(bus, dev):
    """USBDEVFS_RESET 을 보낸다."""
    node = "/dev/bus/usb/%03d/%03d" % (bus, dev)
    try:
        fd = os.open(node, os.O_WRONLY)
    except PermissionError:
        raise UsbResetError(
            "%s 에 접근 권한이 없습니다." % node,
            "· bash run.sh 로 실행하지 않았거나, SSH 접속에서 plugdev 그룹 적용 전(다시 접속)입니다\n"
            "· 즉시 복구가 필요하면:  sudo tools/usb-recover.sh")
    except OSError as e:
        raise UsbResetError("%s 를 열 수 없습니다: %s" % (node, e))
    try:
        fcntl.ioctl(fd, USBDEVFS_RESET, 0)
    except OSError as e:
        raise UsbResetError("USB 리셋 실패: %s" % e,
                            "· 케이블을 뽑았다 다시 꽂으세요")
    finally:
        os.close(fd)


def wait_for_port(stable, fallback, timeout=15):
    """리셋 후 포트가 다시 나타날 때까지 기다리고 실제 경로를 돌려준다.

    `stable` 은 by-path 링크다 (stable_id 참조). 이것이 없으면 원래 포트 경로가
    다시 나타나기만 기다린다 — **아무 포트나 집어 돌려주지 않는다.** 보드가
    여러 대일 때 그렇게 하면 다른 보드를 복구된 것으로 착각하게 된다.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if stable and os.path.exists(stable):
            return os.path.realpath(stable)
        if not stable and os.path.exists(fallback):
            return fallback
        time.sleep(0.5)
    return None


def recover(port, *, on_line=None):
    """스톨 복구 전체 절차. 복구된 포트 경로를 돌려준다 (바뀔 수 있다).

    호출자는 반환값으로 선택 포트를 갱신해야 한다.
    """
    def log(msg):
        if on_line:
            on_line(msg)

    info = find_bridge(port)
    log("USB 장치: %s:%s %s (bus %d dev %d)"
        % (info["vid"], info["pid"], info["product"] or "?",
           info["bus"], info["dev"]))

    stable = stable_id(port)
    if stable:
        log("물리 포트: %s" % os.path.basename(stable))
    else:
        log("⚠ by-path 링크가 없어 포트 번호로만 되찾습니다 "
            "(보드가 여러 대면 확인이 필요합니다)")

    log("USBDEVFS_RESET 실행...")
    reset(info["bus"], info["dev"])

    newport = wait_for_port(stable, port)
    if not newport:
        raise UsbResetError(
            "리셋 후 포트가 다시 나타나지 않았습니다.",
            "· USB 케이블을 뽑았다 다시 꽂으세요")
    if newport != os.path.realpath(port):
        log("포트가 %s 로 다시 잡혔습니다." % newport)
    else:
        log("포트 복구: %s" % newport)
    # 장치가 열거된 직후에는 아직 준비되지 않은 경우가 있어 잠깐 기다린다
    time.sleep(1.0)
    return newport


# ===================== CLI =====================
def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 2
    cmd, port = argv[1], argv[2]
    try:
        if cmd == "info":
            info = find_bridge(port)
            for k in ("vid", "pid", "manufacturer", "product", "bus", "dev", "node"):
                print("  %-13s %s" % (k, info[k]))
            print("  %-13s %s" % ("by-path", stable_id(port) or "(없음)"))
            print("  %-13s %s" % ("writable", os.access(info["node"], os.W_OK)))
            return 0
        if cmd == "reset":
            newport = recover(port, on_line=lambda s: print("  " + s, flush=True))
            print("✅ 복구 완료: %s" % newport)
            return 0
    except UsbResetError as e:
        print("❌ " + e.full(), file=sys.stderr)
        return 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
