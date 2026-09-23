#!/usr/bin/env python3
"""USB 물리 슬롯 ↔ 센서 이름 매핑.

## 왜 슬롯으로 구분하는가

같은 종류의 진동센서 두 대를 한 RPi 에 꽂으면 **소프트웨어로는 구분할 방법이 없다.**

  · USB-UART 브리지(Cypress CY7C65213)에 **고유 시리얼 번호가 없다.**
    두 대 모두 `ID_SERIAL=Cypress_Semiconductor_USB-UART_LP` 로 같다.
    → `/dev/serial/by-id/` 는 충돌해서 쓸 수 없다.
  · **패킷 헤더에도 장비 식별자가 없다** (v2 헤더 18B: magic·version·rate_step·
    sample_count·seq·timestamp_ms·full_scale_g·reserved).
    → 흘러들어오는 데이터만 봐서는 어느 센서인지 알 수 없다.
  · `/dev/ttyACM0` 번호는 **꽂는 순서·재부팅에 따라 바뀐다.**

남는 근거는 **어느 USB 구멍에 꽂혔는가** 하나뿐이다. 커널이 `/dev/serial/by-path/`
에 허브 포트 경로를 그대로 노출하며, 이 경로는 재부팅·재연결에도 유지된다.

따라서 **USB 포트에 A/B 라벨을 붙이는 것이 이 프로그램의 운영 전제다.**
케이블을 다른 구멍에 꽂으면 센서 이름이 바뀐다 — README 와 GUI 에 명시한다.

## 슬롯 키

    /dev/serial/by-path/platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.2:1.0
                        └──────────────── 슬롯 키 ────────────────────┘ └┬┘
                                                                    인터페이스 번호

끝의 `:1.0` 은 USB 인터페이스 번호라 같은 장치 안에서도 여러 개가 생긴다.
이를 떼어낸 앞부분이 **물리 구멍**을 가리킨다. 커널은 같은 장치에 `usb-` 와
`usbv2-` 두 가지 링크를 함께 만들기도 하므로 `usbv2` 를 `usb` 로 정규화해
같은 슬롯이 둘로 세어지지 않게 한다.
"""

import json
import os
import re
from pathlib import Path

BY_PATH = Path("/dev/serial/by-path")

def _config_home():
    """설정 폴더의 상위 경로.

    udev 규칙 설치는 root 권한이 필요해 `sudo python3 slots.py --make-udev` 로
    실행되는데, 그때 `Path.home()` 은 **/root** 를 가리켜 사용자가 지정해 둔
    매핑을 찾지 못한다. sudo 가 남기는 SUDO_USER 로 원래 사용자의 홈을 찾는다.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg)
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and os.geteuid() == 0:
        try:
            import pwd
            return Path(pwd.getpwnam(sudo_user).pw_dir) / ".config"
        except (KeyError, ImportError):
            pass
    return Path.home() / ".config"


CONFIG_DIR = _config_home() / "iis3dwb-collector"
SLOTS_PATH = CONFIG_DIR / "slots.json"

SCHEMA = 1

# 센서 보드가 쓰는 USB-시리얼 칩. 마우스·키보드 등 다른 CDC 장치를 센서로
# 오인하지 않도록 화이트리스트로 거른다.
KNOWN_BRIDGES = {
    (0x04B4, 0x0003): "Cypress CY7C65213",
    (0x10C4, 0xEA60): "CP210x",
    (0x1A86, 0x7523): "CH340",
    (0x0403, 0x6001): "FTDI FT232",
}
# ESP32-S3 내장 USB(Espressif VID)는 PID 가 보드마다 달라 VID 로만 본다.
ESPRESSIF_VID = 0x303A

_IFACE_SUFFIX = re.compile(r":\d+\.\d+$")


class PortInfo:
    """감지된 센서 포트 하나."""

    __slots__ = ("device", "slot", "vid", "pid", "chip", "fixed")

    def __init__(self, device, slot, vid, pid, chip, fixed=None):
        self.device = device      # /dev/ttyACM0 — 번호는 꽂는 순서에 따라 바뀐다
        self.slot = slot          # 물리 슬롯 키 (변하지 않음)
        self.vid = vid
        self.pid = pid
        self.chip = chip          # 사람이 읽는 칩 이름
        self.fixed = fixed        # /dev/iis3dwb1 — udev 가 만든 고정 이름 (없으면 None)

    @property
    def open_path(self):
        """포트를 열 때 쓸 경로. 고정 이름이 있으면 그쪽을 쓴다.

        `/dev/ttyACM0` 은 재부팅·재연결로 번호가 바뀌지만 고정 이름은 그대로다.
        """
        return self.fixed or self.device

    @property
    def short_slot(self):
        """화면에 띄울 짧은 슬롯 표기 — 'usb-0:1.2' 부분만."""
        i = self.slot.rfind("-usb-")
        return self.slot[i + 1:] if i >= 0 else self.slot

    def __repr__(self):
        return "PortInfo(%s, %s, %s)" % (self.device, self.short_slot, self.chip)


def slot_key(by_path_name):
    """by-path 링크 이름에서 물리 슬롯 키를 뽑는다."""
    name = by_path_name.replace("-usbv2-", "-usb-")
    return _IFACE_SUFFIX.sub("", name)


def _device_to_slot():
    """{/dev/ttyACM0: 슬롯키} 사전. by-path 가 없으면 빈 사전."""
    out = {}
    if not BY_PATH.is_dir():
        return out
    for link in sorted(BY_PATH.iterdir()):
        try:
            dev = os.path.realpath(link)
        except OSError:
            continue
        key = slot_key(link.name)
        # usb-/usbv2- 가 같은 키로 정규화되므로 먼저 본 것을 유지하면 된다
        out.setdefault(dev, key)
    return out


def list_ports():
    """연결된 센서 포트를 슬롯 정보와 함께 돌려준다 (슬롯 키 순 정렬)."""
    try:
        from serial.tools import list_ports as lp
    except ImportError:                       # pragma: no cover
        raise RuntimeError(
            "pyserial 이 필요합니다:  sudo apt install -y python3-serial")

    dev2slot = _device_to_slot()
    # 고정 이름(/dev/iis3dwb*) 이 가리키는 실제 장치 → 고정 이름
    real2fixed = {real: link for link, real in udev_installed_links().items()}
    found = []
    for p in lp.comports():
        vid, pid = p.vid, p.pid
        if vid is None:
            continue
        if (vid, pid) in KNOWN_BRIDGES:
            chip = KNOWN_BRIDGES[(vid, pid)]
        elif vid == ESPRESSIF_VID:
            chip = "ESP32 내장 USB"
        else:
            continue                          # 센서 보드가 아닌 USB 장치
        slot = dev2slot.get(p.device)
        if slot is None:
            # by-path 링크가 없는 경우(udev 미동작 등). 슬롯을 특정할 수 없으므로
            # 장치 경로를 키로 쓰되, 이것은 재부팅에 안정적이지 않음을 이름으로 알린다.
            slot = "unstable:" + p.device
        found.append(PortInfo(p.device, slot, vid, pid, chip,
                              fixed=real2fixed.get(p.device)))
    found.sort(key=lambda x: x.slot)
    return found


# ===================== 매핑 저장/조회 =====================

def load_mapping():
    """{슬롯키: 센서이름}. 파일이 없거나 깨졌으면 빈 사전."""
    try:
        d = json.loads(SLOTS_PATH.read_text(encoding="utf-8"))
        if isinstance(d, dict) and isinstance(d.get("slots"), dict):
            return {str(k): str(v) for k, v in d["slots"].items()}
    except (OSError, ValueError):
        pass
    return {}


def save_mapping(mapping, din_map=None):
    """{슬롯키: 센서이름} 저장. 상위 폴더가 없으면 만든다.

    포토센서 배정(din_map)을 함께 넘기면 갱신하고, 생략하면 기존 값을 보존한다.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    doc = {"schema": SCHEMA, "slots": mapping}
    din = _raw_din_map() if din_map is None else {str(k): int(v)
                                                 for k, v in din_map.items()}
    if din:
        doc["din"] = din
    SLOTS_PATH.write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                          encoding="utf-8")


# ---------- 포토센서 배정 ----------
#
# 진동센서 1대마다 포토센서 1개가 짝을 이룬다 (1:1). 어느 센서가 어느 DIN 을
# 바라볼지는 배선에 달렸으므로 설정으로 둔다.
#
# 기본값은 **이름 순서대로 DIN1, DIN2, …** 다 (A→DIN1, B→DIN2).
# 배선이 다르면 이 값을 고친다 — 틀리면 A 를 지나간 제품이 B 로 기록된다.

def _raw_din_map():
    try:
        d = json.loads(SLOTS_PATH.read_text(encoding="utf-8"))
        if isinstance(d, dict) and isinstance(d.get("din"), dict):
            return {str(k): int(v) for k, v in d["din"].items()}
    except (OSError, ValueError, TypeError):
        pass
    return {}


def load_din_map(names=None):
    """{센서이름: DIN번호}. 설정에 없는 이름은 순서대로 1,2,3… 을 준다."""
    saved = _raw_din_map()
    if names is None:
        return saved
    out = {}
    nxt = 1
    used = set(saved.get(n) for n in names if n in saved)
    for n in sorted(names):
        if n in saved:
            out[n] = saved[n]
            continue
        while nxt in used:
            nxt += 1
        out[n] = nxt
        used.add(nxt)
        nxt += 1
    return out


def save_din_map(din_map):
    """포토센서 배정만 갱신한다 (슬롯 매핑은 보존)."""
    save_mapping(load_mapping(), din_map)


# ===================== 고정 장치 이름 (udev) =====================
#
# 윈도우의 "COM3 은 항상 1번 센서" 에 해당하는 것을 리눅스에서 만드는 방법이다.
#
# `/dev/ttyACM0` 번호는 꽂는 순서·재부팅에 따라 바뀌므로 그대로는 쓸 수 없다.
# udev 규칙으로 **USB 물리 포트마다 고정된 이름**을 붙이면 번호가 바뀌어도
# 같은 구멍은 항상 같은 이름이 된다.
#
#     /dev/iis3dwb1  →  1번 포트에 꽂힌 센서 (항상)
#     /dev/iis3dwb2  →  2번 포트에 꽂힌 센서 (항상)
#
# 포트에 스티커를 붙이지 않아도 `ls -l /dev/iis3dwb*` 로 어느 구멍이 몇 번인지
# 언제든 확인할 수 있다.

UDEV_RULE_PATH = "/etc/udev/rules.d/99-iis3dwb-ports.rules"
UDEV_PREFIX = "iis3dwb"


def udev_link_name(sensor_name):
    """센서 이름 → /dev 에 만들 이름. 숫자면 iis3dwb1, 아니면 iis3dwb_A 식."""
    s = str(sensor_name).strip()
    if s.isdigit():
        return "%s%s" % (UDEV_PREFIX, s)
    safe = re.sub(r"[^A-Za-z0-9]", "_", s) or "x"
    return "%s_%s" % (UDEV_PREFIX, safe)


def udev_link_path(sensor_name):
    return "/dev/" + udev_link_name(sensor_name)


def udev_rules_text(mapping=None):
    """현재 슬롯 매핑으로 udev 규칙 파일 내용을 만든다."""
    mapping = load_mapping() if mapping is None else mapping
    lines = [
        "# IIS3DWB 수집기 — USB 포트별 고정 장치 이름",
        "#",
        "# 이 파일은 `python3 slots.py --make-udev` 가 만든 것입니다. 직접 고치기보다",
        "# 슬롯 지정을 바꾼 뒤 다시 생성하십시오.",
        "#",
        "# 브리지칩에 고유 일련번호가 없어 **꽂힌 USB 구멍**이 유일한 구분 근거입니다.",
        "# ID_PATH 는 그 구멍을 가리키며 재부팅·재연결에도 변하지 않습니다.",
        "",
    ]
    for slot, name in sorted(mapping.items(), key=lambda kv: str(kv[1])):
        if slot.startswith("unstable:"):
            # by-path 가 없어 장치 경로를 키로 쓴 경우 — 고정 이름을 만들 수 없다
            lines.append("# (건너뜀) %s 는 물리 포트를 특정할 수 없습니다" % name)
            continue
        lines.append('# 센서 %s' % name)
        lines.append('SUBSYSTEM=="tty", ENV{ID_PATH}=="%s:*", '
                     'SYMLINK+="%s", ENV{ID_MM_DEVICE_IGNORE}="1"'
                     % (slot, udev_link_name(name)))
        lines.append("")
    return "\n".join(lines) + "\n"


def udev_installed_links():
    """지금 존재하는 /dev/iis3dwb* 심볼릭 링크 → 실제 장치 경로."""
    out = {}
    dev = Path("/dev")
    try:
        for p in dev.iterdir():
            if p.name.startswith(UDEV_PREFIX) and p.is_symlink():
                try:
                    out[str(p)] = os.path.realpath(p)
                except OSError:
                    pass
    except OSError:
        pass
    return out


class Resolution:
    """현재 연결 상태를 매핑과 대조한 결과."""

    __slots__ = ("assigned", "unknown", "missing")

    def __init__(self, assigned, unknown, missing):
        self.assigned = assigned    # [(이름, PortInfo)] — 이름 순
        self.unknown = unknown      # [PortInfo] — 매핑에 없는 슬롯
        self.missing = missing      # [이름] — 매핑에 있으나 지금 안 꽂힌 센서

    @property
    def names(self):
        return [n for n, _ in self.assigned]

    @property
    def ok(self):
        """수집을 시작해도 되는 상태인가.

        **이름이 지정되지 않은 슬롯이 하나라도 있으면 시작하지 않는다.** 어느
        센서인지 모르는 데이터를 A 로 기록하는 것이 가장 나쁜 결과다.

        반면 **일부만 연결된 것은 정상 운영이다.** 센서 1대만 꽂고 쓰는 경우가
        실제로 있으므로 여기서 막지 않고, 사실만 안내한다.
        """
        return bool(self.assigned) and not self.unknown


def resolve(mapping=None):
    """연결된 포트를 매핑에 비춰 정리한다."""
    mapping = load_mapping() if mapping is None else mapping
    ports = list_ports()

    assigned, unknown = [], []
    seen_names = set()
    for p in ports:
        name = mapping.get(p.slot)
        if name:
            assigned.append((name, p))
            seen_names.add(name)
        else:
            unknown.append(p)
    assigned.sort(key=lambda t: t[0])
    missing = sorted(n for n in mapping.values() if n not in seen_names)
    return Resolution(assigned, unknown, missing)


# ===================== CLI =====================

def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(
        description="USB 슬롯 ↔ 센서 이름 매핑 확인·지정")
    ap.add_argument("--assign", nargs=2, metavar=("장치", "이름"),
                    help="예: --assign /dev/ttyACM0 A")
    ap.add_argument("--din", nargs=2, metavar=("이름", "DIN번호"),
                    help="포토센서 배정. 예: --din A 1  (A 는 DIN1 을 바라본다)")
    ap.add_argument("--auto", action="store_true",
                    help="지금 연결된 포트에 1, 2, … 번호를 순서대로 자동 지정")
    ap.add_argument("--make-udev", action="store_true",
                    help="USB 포트별 고정 장치 이름(/dev/iis3dwb1 …) 규칙을 설치한다")
    ap.add_argument("--print-udev", action="store_true",
                    help="설치하지 않고 규칙 내용만 출력")
    ap.add_argument("--forget", metavar="이름", help="해당 이름의 매핑을 지운다")
    ap.add_argument("--clear", action="store_true", help="매핑 전체 삭제")
    args = ap.parse_args(argv)

    mapping = load_mapping()

    if args.clear:
        save_mapping({})
        print("매핑을 모두 지웠습니다.")
        return 0

    if args.forget:
        mapping = {k: v for k, v in mapping.items() if v != args.forget}
        save_mapping(mapping)
        print("'%s' 매핑을 지웠습니다." % args.forget)

    if args.assign:
        dev, name = args.assign
        for p in list_ports():
            if p.device == dev:
                mapping[p.slot] = name
                save_mapping(mapping)
                print("지정: %s (%s) → '%s'" % (dev, p.short_slot, name))
                break
        else:
            print("❌ %s 는 센서 포트 목록에 없습니다." % dev)
            return 1

    if args.auto:
        # 슬롯 키 순서대로 1, 2, 3 … 을 붙인다. 슬롯 키에는 USB 허브 포트 번호가
        # 들어 있어 정렬하면 물리적 배치 순서와 대체로 일치한다.
        ports = list_ports()
        if not ports:
            print("❌ 연결된 센서 포트가 없습니다.")
            return 1
        mapping = {p.slot: str(i + 1) for i, p in enumerate(ports)}
        save_mapping(mapping, {str(i + 1): i + 1 for i in range(len(ports))})
        print("자동 지정 (%d대)" % len(ports))
        for i, p in enumerate(ports):
            print("  %s  →  센서 %d   (포토센서 DIN%d)"
                  % (p.device, i + 1, i + 1))
        print("\n고정 이름을 만들려면:  sudo python3 slots.py --make-udev")

    if args.print_udev:
        print(udev_rules_text(), end="")
        return 0

    if args.make_udev:
        text = udev_rules_text()
        if not load_mapping():
            print("❌ 지정된 센서가 없습니다. 먼저 --auto 또는 --assign 으로 이름을 정하세요.")
            return 1
        try:
            with open(UDEV_RULE_PATH, "w", encoding="utf-8") as f:
                f.write(text)
        except PermissionError:
            print("❌ 권한이 없습니다. 이렇게 실행하세요:")
            print("   sudo python3 %s --make-udev" % os.path.basename(__file__))
            return 1
        import subprocess
        subprocess.run(["udevadm", "control", "--reload-rules"], check=False)
        subprocess.run(["udevadm", "trigger", "--subsystem-match=tty",
                        "--action=add"], check=False)
        print("고정 장치 이름 설치 완료: %s" % UDEV_RULE_PATH)
        import time
        time.sleep(1.5)
        links = udev_installed_links()
        if links:
            for link, real in sorted(links.items()):
                print("   %s  →  %s" % (link, real))
        else:
            print("   (지금 연결된 센서가 없어 링크는 다음 연결 시 만들어집니다)")

    if args.din:
        name, num = args.din
        num = int(num)
        if not 1 <= num <= 4:
            print("❌ DIN 번호는 1~4 여야 합니다.")
            return 1
        dm = _raw_din_map()
        dm[name] = num
        save_din_map(dm)
        print("포토센서 배정: '%s' → DIN%d" % (name, num))

    res = resolve(mapping)
    dins = load_din_map(res.names)
    print("설정 파일: %s" % SLOTS_PATH)
    print("")
    if res.assigned:
        print("지정된 센서  (진동센서 1대 ↔ 포토센서 1개)")
        print("  %-6s %-18s %-14s %-12s %s"
              % ("이름", "고정 장치 이름", "현재 장치", "USB 포트", "포토센서"))
        for name, p in res.assigned:
            print("  %-6s %-18s %-14s %-12s DIN%d"
                  % (name, p.fixed or "(미설치)", p.device, p.short_slot,
                     dins.get(name, 1)))
        if any(p.fixed is None for _, p in res.assigned):
            print("\n  ℹ 고정 장치 이름이 없습니다. 만들면 /dev/ttyACM 번호가 바뀌어도")
            print("    같은 구멍은 항상 같은 이름이 됩니다:")
            print("      sudo python3 slots.py --make-udev")
    if res.unknown:
        print("")
        print("⚠ 이름이 지정되지 않은 슬롯 — 이름을 지정해야 수집할 수 있습니다")
        for p in res.unknown:
            print("  %-14s %-24s %s" % (p.device, p.short_slot, p.chip))
            print("       지정:  python3 slots.py --assign %s A" % p.device)
    if res.missing:
        print("")
        print("ℹ 센서 %d대가 인식되었습니다. 이대로 수집할 수 있습니다."
              "  (%s 는 연결되지 않음)"
              % (len(res.assigned), ", ".join(res.missing)))
    if not res.assigned and not res.unknown:
        print("연결된 센서 포트가 없습니다.")
        print("  · USB 케이블이 데이터 전송용인지 확인하세요 (충전 전용 불가)")
    print("")
    print("수집 가능 상태: %s" % ("예" if res.ok else "아니오"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
