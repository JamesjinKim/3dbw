#!/usr/bin/env python3
"""boardcheck.py — IIS3DWB 센서 보드 수입검사 실행기.

보드를 USB 로 꽂고 실행하면 검사 펌웨어를 굽고, 부팅 로그에서 판정을 읽어
사람이 읽는 요약과 종료 코드로 돌려준다. 로그는 results/ 에 보관된다.

    ./boardcheck.py --list         # 연결된 보드를 MAC 으로 확인 (여러 대일 때)
    ./boardcheck.py                # 굽고 검사
    ./boardcheck.py --all          # 꽂힌 모든 보드를 차례로 검사 (2대 이상일 때)
    ./boardcheck.py --no-flash     # 이미 검사 펌웨어가 있는 보드를 재검사
    ./boardcheck.py --port /dev/ttyUSB0
    ./boardcheck.py --loop         # 보드를 갈아 끼우며 연속 검사

종료 코드:  0=PASS  1=WARN  2=FAIL  3=실행 오류(연결·플래시 실패 등)

이 스크립트는 판정을 직접 하지 않는다. 판정은 전부 펌웨어가 하고
`BOARDCHECK_RESULT=` / `BOARDCHECK_ITEM=` 줄로 내보낸다. 호스트가 로그를
해석해 판정하면, 로그 문구를 바꿀 때마다 판정이 조용히 망가진다.
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(HERE, "dist")
RESULTS = os.path.join(HERE, "results")

# esptool 호출·USB 스톨 복구는 설정툴과 같은 구현을 쓴다. 두 벌로 나뉘면
# "굽기는 되는데 검사만 안 되는" 차이가 생겨 원인을 찾기 어려워진다.
for cand in (os.path.join(HERE, "lib"),                     # 배포 사본
             os.path.join(HERE, "..", "sensor-setup-py")):  # 저장소 안
    if os.path.isdir(cand) and cand not in sys.path:
        sys.path.insert(0, os.path.abspath(cand))

try:
    import esp_flash
    import ports
    import usb_reset
except ImportError as e:            # pragma: no cover
    sys.stderr.write(
        "❌ esp_flash / ports / usb_reset 모듈을 찾지 못했습니다 (%s).\n"
        "   이 폴더 옆에 sensor-setup-py/ 가 있거나, boardcheck/lib/ 에 사본이 있어야 합니다.\n" % e)
    sys.exit(3)

try:
    import serial
except ImportError:                 # pragma: no cover
    sys.stderr.write("❌ pyserial 이 필요합니다:  sudo apt install python3-serial\n")
    sys.exit(3)

CONSOLE_BAUD = 115200               # 부팅 로그는 항상 이 속도 (펌웨어 기본 콘솔)
CAPTURE_TIMEOUT = 90                # 검사 전체 상한 (INT 측정 10초 + 여유)

RE_RESULT = re.compile(r"^BOARDCHECK_RESULT=(\w+)")
RE_ITEM = re.compile(r"^BOARDCHECK_ITEM=(\d+)\|([^|]*)\|(\w+)\|(.*)$")

EXIT = {"PASS": 0, "WARN": 1, "FAIL": 2}

C = {"pass": "\033[32m", "fail": "\033[31m", "warn": "\033[33m",
     "dim": "\033[2m", "bold": "\033[1m", "off": "\033[0m"}
if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
    C = {k: "" for k in C}


def pad(s, width):
    """표시 폭 기준 왼쪽 정렬 패딩.

    `"%-16s" % s` 는 **문자 수**로 채우는데 한글은 터미널에서 두 칸을
    차지하므로 열이 어긋난다. 검사표는 담당자가 눈으로 훑는 것이라
    정렬이 깨지면 읽기 부담이 커진다.
    """
    import unicodedata
    w = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)
    return s + " " * max(0, width - w)


# ===================== 포트 =====================
#
# 열거는 sensor-setup-py/ports.py 를 쓴다. by-id 를 쓰면 두 대가 1개로 보여
# 한 대를 놓치고, 그 상태의 "자동 감지" 는 엉뚱한 보드를 굽는다 — 그 판단과
# 구현을 한 곳에만 두기 위해 공용 모듈로 뽑았다. (esp_flash·usb_reset 과 같은 경로)


def list_candidates():
    """[(장치경로, 물리슬롯)] — 슬롯 순."""
    return [(b.device, b.slot) for b in ports.list_ports()]


def short_slot(slot):
    i = slot.rfind("-usb-")
    return slot[i + 1:] if i >= 0 else slot


def find_port(explicit=None):
    """검사할 보드의 시리얼 포트를 고른다."""
    if explicit:
        if not os.path.exists(explicit):
            raise RuntimeError("포트를 찾을 수 없습니다: %s" % explicit)
        return explicit

    cands = list_candidates()
    if not cands:
        raise RuntimeError(
            "USB 시리얼 포트를 찾지 못했습니다.\n"
            "  · 보드가 꽂혀 있는지, 충전 전용 케이블이 아닌지 확인하세요\n"
            "  · dmesg | tail 로 장치 인식 여부를 볼 수 있습니다")
    if len(cands) > 1:
        # 어느 보드인지 모르는 채로 굽지 않는다. 고를 수 있도록 안내한다.
        lines = ["  %-14s %s" % (d, short_slot(s)) for d, s in cands]
        raise RuntimeError(
            "보드가 %d대 꽂혀 있습니다. 검사할 포트를 지정하세요:\n%s\n\n"
            "  어느 것이 어느 보드인지 모르겠다면 MAC 으로 확인하세요:\n"
            "    ./boardcheck.py --list\n"
            "  전부 차례로 검사하려면:\n"
            "    ./boardcheck.py --all"
            % (len(cands), "\n".join(lines)))
    return cands[0][0]


def cmd_list():
    """연결된 보드를 MAC 과 함께 보여준다.

    MAC 조회는 esptool 로 부트로더에 진입하므로 보드가 리셋된다. 굽지는 않는다.
    """
    boards = ports.list_ports()
    if not boards:
        print("연결된 USB 시리얼 포트가 없습니다.")
        return 1
    print("연결된 보드 %d대 — MAC 을 읽는 중입니다 (포트당 약 3초)\n" % len(boards))
    ports.read_macs(boards)
    print("  %-14s %-12s %s" % ("장치", "USB 포트", "MAC"))
    for b in boards:
        print("  %-14s %-12s %s"
              % (b.device, b.short_slot,
                 b.mac or ("읽기 실패 — %s" % (b.error or "")[:40])))
    print("\n검사:  ./boardcheck.py --port <장치>   또는   --all")
    return 0


# ===================== 플래시 =====================

def load_manifest():
    path = os.path.join(DIST, "manifest.json")
    if not os.path.exists(path):
        raise RuntimeError(
            "검사 펌웨어가 없습니다 (%s).\n  · 저장소에서라면  ./build.sh  를 먼저 실행하세요" % path)
    with open(path) as f:
        m = json.load(f)
    if m.get("kind") != "iis3dwb-boardcheck":
        raise RuntimeError("manifest.json 이 검사 펌웨어의 것이 아닙니다: kind=%r" % m.get("kind"))
    return m


def verify_images(m):
    """굽기 전에 sha256 을 확인한다 — 손상된 사본이 보드에 닿지 않게."""
    import hashlib
    bad = []
    for im in m["images"]:
        p = os.path.join(DIST, im["file"])
        if not os.path.exists(p):
            bad.append("%s — 파일 없음" % im["file"])
            continue
        with open(p, "rb") as f:
            got = hashlib.sha256(f.read()).hexdigest()
        if got != im["sha256"]:
            bad.append("%s — sha256 불일치" % im["file"])
    if bad:
        raise RuntimeError("검사 펌웨어가 손상되었습니다:\n  " + "\n  ".join(bad))


def flash(port, m, verbose=False):
    """검사 펌웨어 3종을 단일 esptool 호출로 굽는다.

    NVS(0x9000)는 건드리지 않는다. 검사 후 운영 펌웨어를 다시 구우면
    기존 설정이 그대로 살아 있다 (파티션 표가 같기 때문).
    """
    chip = m.get("chip", "esp32s3")
    args = (["--chip", chip, "--port", port, "--baud", str(esp_flash.BAUD),
             "--before", "default_reset",
             # hard_reset: 굽자마자 보드가 스스로 재부팅해 검사를 시작한다.
             "--after", "hard_reset"]
            + esp_flash.stub_args(chip)
            + [esp_flash.subcmd("write_flash")] + m["write_flash_args"])
    for im in m["images"]:
        args += [im["offset"], os.path.join(DIST, im["file"])]

    last_pct = [-1]

    def on_progress(pct):
        if verbose or pct - last_pct[0] < 10:
            return
        last_pct[0] = pct
        sys.stdout.write("\r  굽는 중... %3d%%" % pct)
        sys.stdout.flush()

    out = esp_flash.run_esptool(
        args, on_line=(print if verbose else None), on_progress=on_progress,
        total_timeout=esp_flash.TOTAL_TIMEOUT_FW)
    if not verbose:
        sys.stdout.write("\r  굽는 중... 완료   \n")

    verified = out.count("Hash of data verified.")
    if verified < len(m["images"]):
        raise RuntimeError("플래시 검증이 부족합니다 (%d/%d).\n%s"
                           % (verified, len(m["images"]), out[-2000:]))
    return out


# ===================== 로그 수집 =====================

def capture(port, timeout=CAPTURE_TIMEOUT, echo=True):
    """`BOARDCHECK_DONE` 이 나올 때까지 콘솔 로그를 모은다.

    반환: (줄 리스트, 완료 여부)
    """
    lines, buf = [], b""
    deadline = time.time() + timeout
    done = False

    with serial.Serial(port, CONSOLE_BAUD, timeout=0.2) as ser:
        # 굽기 직후 hard_reset 으로 이미 부팅이 시작됐을 수 있다. DTR/RTS 를
        # 눌러 한 번 더 리셋하면 로그를 첫 줄부터 확실히 받는다.
        try:
            ser.dtr = False
            ser.rts = True
            time.sleep(0.1)
            ser.rts = False
        except Exception:
            pass                    # DTR/RTS 를 못 쓰는 브리지도 있다 — 치명적이지 않다
        ser.reset_input_buffer()

        while time.time() < deadline and not done:
            chunk = ser.read(4096)
            if not chunk:
                continue
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode("utf-8", "replace").rstrip("\r")
                lines.append(line)
                if echo:
                    print(line)
                if line.startswith("BOARDCHECK_DONE"):
                    done = True
                    break
    return lines, done


def parse(lines):
    result, items = None, []
    for line in lines:
        m = RE_RESULT.match(line)
        if m:
            result = m.group(1)
            continue
        m = RE_ITEM.match(line)
        if m:
            items.append({"no": int(m.group(1)), "name": m.group(2),
                          "verdict": m.group(3), "detail": m.group(4)})
    return result, items


def save_log(lines, result, items):
    os.makedirs(RESULTS, exist_ok=True)
    mac = "unknown"
    for it in items:
        m = re.search(r"MAC ([0-9A-Fa-f:]{17})", it["detail"])
        if m:
            mac = m.group(1).replace(":", "")
            break
    path = os.path.join(RESULTS, "%s_%s_%s.log" % (
        datetime.now().strftime("%Y%m%d-%H%M%S"), mac, result or "NORESULT"))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


# ===================== 한 보드 검사 =====================

def mac_of(items):
    """검사 항목에서 MAC 을 뽑는다 (1번 항목의 detail 에 들어 있다)."""
    for it in items:
        m = re.search(r"MAC ([0-9A-Fa-f:]{17})", it["detail"])
        if m:
            return m.group(1).lower()
    return None


def run_once(args, port=None):
    """보드 한 대를 검사한다. (종료코드, 판정, MAC) 을 돌려준다."""
    port = port or find_port(args.port)
    print("포트: %s" % port)

    if not args.no_flash:
        m = load_manifest()
        verify_images(m)
        print("펌웨어: %s  (빌드 %s)" % (
            ", ".join(i["file"] for i in m["images"]), m.get("built_at", "?")))
        pin = m.get("pinmap", {})
        print("핀맵: MOSI=%s MISO=%s SCLK=%s CS=%s INT1=%s INT2=%s" % (
            pin.get("IIS3DWB_SPI_MOSI_GPIO"), pin.get("IIS3DWB_SPI_MISO_GPIO"),
            pin.get("IIS3DWB_SPI_SCLK_GPIO"), pin.get("IIS3DWB_SPI_CS_GPIO"),
            pin.get("IIS3DWB_INT1_GPIO"), pin.get("IIS3DWB_INT2_GPIO")))
        try:
            flash(port, m, verbose=args.verbose)
        except esp_flash.UsbStallError:
            # 라즈베리파이에서 흔한 bulk-OUT 스톨. 장치 리셋 후 한 번만 재시도한다.
            print("%sUSB 전송이 멈췄습니다 — 장치를 리셋하고 한 번 더 시도합니다.%s"
                  % (C["warn"], C["off"]))
            port = usb_reset.recover(port, on_line=print)
            flash(port, m, verbose=args.verbose)

    print("")
    print("%s─ 보드 로그 ─%s" % (C["dim"], C["off"]))
    lines, done = capture(port, timeout=args.timeout, echo=True)

    result, items = parse(lines)
    log_path = save_log(lines, result, items)

    print("")
    if not done or result is None:
        print("%s✗ 검사가 끝나지 않았습니다 (%d초 안에 판정 줄이 오지 않음).%s"
              % (C["fail"], args.timeout, C["off"]))
        print("  · 보드가 부팅 중 리셋을 반복하는지 위 로그를 확인하세요")
        print("  · 검사 펌웨어가 아닌 다른 펌웨어가 들어 있을 수 있습니다 (--no-flash 를 뺐는지 확인)")
        print("  로그: %s" % log_path)
        return 3, None, mac_of(items)

    color = C["pass"] if result == "PASS" else (C["warn"] if result == "WARN" else C["fail"])
    print("%s%s╔══════════════════════════════════════╗%s" % (C["bold"], color, C["off"]))
    print("%s%s║  판정:  %-28s ║%s" % (C["bold"], color, result, C["off"]))
    print("%s%s╚══════════════════════════════════════╝%s" % (C["bold"], color, C["off"]))
    for it in items:
        c = C["pass"] if it["verdict"] == "PASS" else (
            C["warn"] if it["verdict"] == "WARN" else
            C["fail"] if it["verdict"] == "FAIL" else C["dim"])
        print("  %s%-4s%s  %s %s" % (c, it["verdict"], C["off"],
                                     pad(it["name"], 18), it["detail"]))
    print("  로그: %s" % log_path)
    return EXIT.get(result, 3), result, mac_of(items)


def cmd_all(args):
    """꽂혀 있는 보드를 **차례로** 검사하고 요약표를 낸다.

    동시에 하지 않는다 — esptool 은 포트를 독점하고, USB 스톨 복구가 포트 번호를
    재배치해 다른 보드의 경로를 바꿀 수 있다. 한 대가 실패해도 나머지는 계속
    검사한다(한 번 꽂아 최대한 많이 알기 위해). 종료코드는 가장 나쁜 결과를 따른다.
    """
    cands = list_candidates()
    if not cands:
        sys.stderr.write("❌ 연결된 USB 시리얼 포트가 없습니다.\n")
        return 3
    print("=" * 64)
    print(" 보드 %d대를 차례로 검사합니다 (동시 진행하지 않습니다)" % len(cands))
    print("=" * 64)

    rows = []
    for i, (dev, slot) in enumerate(cands, 1):
        print("\n%s[%d/%d] %s  (%s)%s"
              % (C["bold"], i, len(cands), dev, short_slot(slot), C["off"]))
        print("-" * 64)
        try:
            rc, result, mac = run_once(args, port=dev)
        except (RuntimeError, esp_flash.EsptoolError, usb_reset.UsbResetError) as e:
            sys.stderr.write("❌ %s\n" % e)
            rc, result, mac = 3, None, None
        rows.append((mac, short_slot(slot), dev, result, rc))

    print("\n" + "=" * 64)
    print(" 요약")
    print("=" * 64)
    print("  %-19s %-12s %-14s %s" % ("MAC", "USB 포트", "장치", "판정"))
    worst = 0
    for mac, slot, dev, result, rc in rows:
        label = result or "미완료"
        c = (C["pass"] if rc == 0 else C["warn"] if rc == 1 else C["fail"])
        print("  %-19s %-12s %-14s %s%s%s"
              % (mac or "?", slot, dev, c, label, C["off"]))
        worst = max(worst, rc)
    ok = sum(1 for r in rows if r[4] == 0)
    print("\n  PASS %d / %d" % (ok, len(rows)))
    return worst


def main(argv=None):
    p = argparse.ArgumentParser(
        description="IIS3DWB 센서 보드 수입검사",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    p.add_argument("--list", action="store_true",
                   help="연결된 보드를 MAC 과 함께 보여준다 (검사하지 않음)")
    p.add_argument("--all", action="store_true",
                   help="꽂혀 있는 모든 보드를 차례로 검사하고 요약표를 낸다")
    p.add_argument("--port", help="시리얼 포트 (기본: 자동 탐지)")
    p.add_argument("--no-flash", action="store_true",
                   help="굽지 않고 현재 보드의 검사 결과만 읽는다 (재부팅해 재검사)")
    p.add_argument("--loop", action="store_true",
                   help="보드를 갈아 끼우며 연속 검사 (Ctrl+C 로 종료)")
    p.add_argument("--timeout", type=int, default=CAPTURE_TIMEOUT,
                   help="판정 대기 상한 초 (기본 %d)" % CAPTURE_TIMEOUT)
    p.add_argument("--verbose", action="store_true", help="esptool 출력을 모두 표시")
    args = p.parse_args(argv)

    if args.list:
        return cmd_list()

    if args.all:
        return cmd_all(args)

    if not args.loop:
        try:
            return run_once(args)[0]
        except (RuntimeError, esp_flash.EsptoolError, usb_reset.UsbResetError) as e:
            sys.stderr.write("\n❌ %s\n" % e)
            return 3

    tally = {"PASS": 0, "WARN": 0, "FAIL": 0, "ERROR": 0}
    try:
        while True:
            print("\n" + "=" * 64)
            print(" 보드를 연결하고 Enter — 종료는 Ctrl+C")
            print("=" * 64)
            input()
            try:
                rc = run_once(args)[0]
                key = {0: "PASS", 1: "WARN", 2: "FAIL"}.get(rc, "ERROR")
            except (RuntimeError, esp_flash.EsptoolError, usb_reset.UsbResetError) as e:
                sys.stderr.write("\n❌ %s\n" % e)
                key = "ERROR"
            tally[key] += 1
            print("\n누계  PASS %d · WARN %d · FAIL %d · 오류 %d"
                  % (tally["PASS"], tally["WARN"], tally["FAIL"], tally["ERROR"]))
    except (KeyboardInterrupt, EOFError):
        print("\n\n검사 종료.  PASS %d · WARN %d · FAIL %d · 오류 %d"
              % (tally["PASS"], tally["WARN"], tally["FAIL"], tally["ERROR"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
