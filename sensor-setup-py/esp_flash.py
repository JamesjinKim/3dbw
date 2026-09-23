#!/usr/bin/env python3
"""esptool 래퍼 — 펌웨어 플래시 / NVS 주입 / 디바이스 확인

GUI 와 분리해 둔 이유: 여기까지는 tkinter 없이 SSH 에서 헤드리스로 검증할 수 있다.

    python3 esp_flash.py which
    python3 esp_flash.py detect /dev/ttyACM0
    python3 esp_flash.py flash-fw /dev/ttyACM0 <패키지경로>
    python3 esp_flash.py flash-nvs /dev/ttyACM0 <nvs.bin>

라즈베리파이 고유 문제 — USB bulk OUT 스톨:
    브리지의 OUT 엔드포인트가 멈추면 esptool 의 write() 가 영구 블로킹된다.
    커널 로그에 오류가 남지 않고 TIOCOUTQ 만 줄지 않아, 겉보기로는 "멈춤" 이다.
    그래서 출력이 끊긴 시간을 재는 워치독을 두고 UsbStallError 로 올린다.
    복구는 usb_reset.py (USBDEVFS_RESET) 가 담당한다.
"""

import glob
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import fw_manifest

NVS_OFFSET = 0x9000
NVS_SIZE = 0x6000
# 보드레이트는 전 구간 115200 으로 고정한다.
#
# 근거:
#   · 펌웨어 콘솔이 115200 고정이라(CONFIG_ESP_CONSOLE_UART_BAUDRATE) 로그 캡처는
#     어차피 이 값이어야 한다. 한 값으로 통일하면 경로별 차이가 사라진다.
#   · 고속(230400/460800/921600)에서 USB 스톨이 간헐적으로 발생하는 것을 실측했다.
#     460800 쓰기는 대체로 되지만 읽기는 실패가 잦다. 배포툴에서는 30초 빠른 것보다
#     매번 되는 것이 중요하다.
#   · 앱 840KB 를 115200 으로 굽는 데 약 2분이 걸린다. 프로비저닝 작업으로는 충분하다.
BAUD = 115200
NVS_BAUD = BAUD
FW_BAUD_LADDER = (BAUD,)

# 워치독: 이 시간 동안 esptool 출력이 없으면 스톨로 본다.
# esptool 은 진행률을 자주 갱신하므로 정상 동작 중 25초 침묵은 없다.
IDLE_TIMEOUT = 25
TOTAL_TIMEOUT_FW = 600
TOTAL_TIMEOUT_NVS = 120


class EsptoolError(Exception):
    """esptool 실행 실패. `hint` 에 사용자가 취할 조치를 담는다."""

    def __init__(self, message, hint=None, output=""):
        super().__init__(message)
        self.hint = hint
        self.output = output

    def full(self):
        s = str(self)
        if self.hint:
            s += "\n\n" + self.hint
        return s


class UsbStallError(EsptoolError):
    """USB OUT 엔드포인트 스톨 — usb_reset 으로 복구 가능."""


class PortBusyError(EsptoolError):
    """다른 프로세스가 포트를 점유."""


# ===================== esptool 탐색 / 버전 =====================
_esptool_cache = None
_version_cache = None


def find_esptool():
    """esptool 실행 커맨드(리스트)를 돌려준다.

    탐색 순서:
      0) $IIS3DWB_ESPTOOL (명시적 오버라이드)
      1) PATH 의 esptool / esptool.py
         · 라즈베리파이 apt 패키지는 /usr/bin/esptool (esptool.py 가 아니다)
         · ESP-IDF export.sh 를 소싱했다면 IDF venv 쪽이 먼저 잡힌다
      2) ~/.espressif/python_env/*/bin/esptool.py
         venv 스크립트는 같은 venv 의 python 으로 돌려야 import 가 된다
      3) 현재 파이썬의 -m esptool
    """
    global _esptool_cache
    if _esptool_cache is not None:
        return _esptool_cache

    override = os.environ.get("IIS3DWB_ESPTOOL")
    if override:
        _esptool_cache = override.split()
        return _esptool_cache

    for name in ("esptool", "esptool.py"):
        p = shutil.which(name)
        if p:
            _esptool_cache = [p]
            return _esptool_cache

    for cand in sorted(Path.home().glob(".espressif/python_env/*/bin/esptool.py")):
        venv_py = cand.parent / "python"
        if cand.is_file() and venv_py.exists():
            _esptool_cache = [str(venv_py), "-m", "esptool"]
            return _esptool_cache

    _esptool_cache = [sys.executable, "-m", "esptool"]
    return _esptool_cache


def esptool_version():
    """(major, minor, patch) 튜플. 알 수 없으면 (0,0,0)."""
    global _version_cache
    if _version_cache is not None:
        return _version_cache
    try:
        r = subprocess.run(find_esptool() + ["version"],
                           capture_output=True, text=True, timeout=15)
        m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", r.stdout or r.stderr or "")
        _version_cache = (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)) \
            if m else (0, 0, 0)
    except Exception:
        _version_cache = (0, 0, 0)
    return _version_cache


def subcmd(name):
    """서브커맨드 표기를 esptool 버전에 맞춘다.

    esptool 4.x: write_flash / read_mac / flash_id
    esptool 5.x: write-flash / read-mac / flash-id  (구표기는 경고와 함께 동작)
    apt 는 4.7, ESP-IDF v5.4.3 venv 는 4.8 이라 지금은 4.x 지만,
    PATH 에 어느 쪽이 잡히는지에 따라 달라지므로 버전으로 결정한다.
    """
    if esptool_version()[0] >= 5:
        return name.replace("_", "-")
    return name


def describe_tool():
    return "%s (v%d.%d.%d)" % (" ".join(find_esptool()), *esptool_version())


_stub_cache = {}


def stub_available(chip="esp32s3"):
    """이 esptool 설치본에 해당 칩의 스텁 플래셔가 들어 있는지 확인한다.

    왜 확인하는가 — 라즈베리파이/Debian 의 `esptool` 패키지(4.7.0+dfsg)는
    **esp32 / esp32s2 / esp32s3 스텁을 제거한 채** 배포된다(DFSG 재패키징:
    xtensa 스텁은 소스 없는 선빌드 바이너리라 빠지고, RISC-V 것만 남았다).
    그 상태로 기본 동작(스텁 사용)을 시도하면 이렇게 죽는다:

        FileNotFoundError: .../stub_flasher/stub_flasher_32s3.json

    스텁이 없으면 --no-stub 으로 ROM 로더만 써서 진행한다. 조금 느리지만
    플래시·검증은 정상 동작한다.
    """
    if chip in _stub_cache:
        return _stub_cache[chip]

    # 칩 이름 → 스텁 파일 접미사 (esptool 내부 규칙)
    suffix = {"esp32": "32", "esp32s2": "32s2", "esp32s3": "32s3",
              "esp32c2": "32c2", "esp32c3": "32c3", "esp32c6": "32c6",
              "esp32h2": "32h2", "esp32p4": "32p4",
              "esp8266": "8266"}.get(chip, chip.replace("esp", ""))

    found = False
    try:
        r = subprocess.run(
            [sys.executable, "-c",
             "import esptool, os; print(os.path.dirname(esptool.__file__))"],
            capture_output=True, text=True, timeout=15)
        roots = [r.stdout.strip()] if r.returncode == 0 and r.stdout.strip() else []
    except Exception:
        roots = []
    # find_esptool() 이 다른 파이썬/경로를 가리킬 수 있으므로 흔한 경로도 훑는다
    roots += glob.glob("/usr/lib/python3*/dist-packages/esptool")
    roots += glob.glob(str(Path.home() / ".espressif/python_env/*/lib/python*/site-packages/esptool"))
    for root in roots:
        if root and os.path.exists(os.path.join(
                root, "targets", "stub_flasher", "stub_flasher_%s.json" % suffix)):
            found = True
            break

    _stub_cache[chip] = found
    return found


def stub_args(chip="esp32s3"):
    """스텁이 없으면 ['--no-stub'] 을 돌려준다."""
    return [] if stub_available(chip) else ["--no-stub"]


# ===================== 포트 상태 =====================
def port_holder(port):
    """포트를 열고 있는 프로세스를 "이름(PID)" 문자열로 돌려준다 (없으면 None).

    sudo 없이 /proc 를 훑는다. 접근할 수 없는 프로세스는 그냥 건너뛴다.
    esptool 이 "Device or resource busy" 로 죽기 전에 원인을 알려주기 위한 것.
    """
    try:
        target = os.path.realpath(port)
    except OSError:
        return None
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        fd_dir = "/proc/%s/fd" % pid
        try:
            for fd in os.listdir(fd_dir):
                try:
                    if os.path.realpath(os.path.join(fd_dir, fd)) == target:
                        try:
                            name = open("/proc/%s/comm" % pid).read().strip()
                        except OSError:
                            name = "?"
                        if int(pid) != os.getpid():
                            return "%s(PID %s)" % (name, pid)
                except OSError:
                    continue
        except OSError:
            continue
    return None


def preflight(port):
    """플래시 전 점검. 문제가 있으면 EsptoolError 를 올린다."""
    if not os.path.exists(port):
        raise EsptoolError(
            "포트가 없습니다: %s" % port,
            "· USB 케이블이 '데이터 전송용'인지 확인하세요 (충전 전용 케이블 불가)\n"
            "· 케이블을 다시 꽂고 '포트 새로고침' 을 누르세요")
    if not os.access(port, os.W_OK):
        raise EsptoolError(
            "%s 에 쓰기 권한이 없습니다." % port,
            "· install.sh 를 실행했는지 확인하세요\n"
            "· dialout 그룹 추가 후에는 **재로그인(또는 재부팅)** 이 필요합니다")
    holder = port_holder(port)
    if holder:
        raise PortBusyError(
            "%s 를 다른 프로그램이 사용 중입니다: %s" % (port, holder),
            "· 시리얼 모니터나 수신기를 먼저 종료하세요")


# ===================== esptool 실행 =====================
_PCT_RE = re.compile(r"\((\d{1,3})\s*%\)")


def run_esptool(args, *, on_line=None, on_progress=None,
                idle_timeout=IDLE_TIMEOUT, total_timeout=TOTAL_TIMEOUT_FW,
                cancel=None):
    """esptool 을 돌리며 출력을 실시간으로 넘겨준다.

    on_line(str)      : 출력 한 줄 (진행률 줄 포함)
    on_progress(int)  : 0~100 퍼센트
    cancel            : threading.Event — set 되면 프로세스를 종료

    esptool 의 진행률은 개행이 아니라 '\\r' 로 갱신되므로 readline() 으로는
    잡히지 않는다. raw read 후 \\r 과 \\n 양쪽으로 쪼갠다.
    """
    cmd = find_esptool() + args
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, bufsize=0, env=env)

    collected = []
    last_output = [time.time()]
    lock = threading.Lock()

    def reader():
        buf = b""
        try:
            while True:
                chunk = os.read(proc.stdout.fileno(), 4096)
                if not chunk:
                    break
                with lock:
                    last_output[0] = time.time()
                buf += chunk
                buf = buf.replace(b"\r\n", b"\n")
                while True:
                    idx = min((i for i in (buf.find(b"\n"), buf.find(b"\r")) if i >= 0),
                              default=-1)
                    if idx < 0:
                        break
                    line = buf[:idx].decode("utf-8", "replace").rstrip()
                    buf = buf[idx + 1:]
                    if not line:
                        continue
                    collected.append(line)
                    if on_progress:
                        m = _PCT_RE.search(line)
                        if m:
                            on_progress(min(100, int(m.group(1))))
                    if on_line:
                        on_line(line)
        except Exception:
            pass

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    started = time.time()
    stalled = False
    try:
        while proc.poll() is None:
            time.sleep(0.2)
            now = time.time()
            with lock:
                idle = now - last_output[0]
            if cancel is not None and cancel.is_set():
                proc.terminate()
                raise EsptoolError("사용자가 취소했습니다.",
                                   output="\n".join(collected))
            if idle > idle_timeout:
                stalled = True
                break
            if now - started > total_timeout:
                stalled = True
                break
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        t.join(timeout=2)

    out = "\n".join(collected)

    if stalled:
        raise UsbStallError(
            "USB 포트가 응답하지 않습니다 (bulk OUT 엔드포인트 스톨).",
            "· 이 증상은 드라이버 재바인딩으로는 복구되지 않고 USB 장치 리셋이 필요합니다\n"
            "· 자동 복구를 시도하거나, 케이블을 뽑았다 꽂으세요",
            output=out)

    if proc.returncode != 0:
        raise _classify(out, proc.returncode)
    return out


def _classify(out, rc):
    """esptool 출력에서 원인을 추정해 실행 가능한 안내를 붙인다."""
    low = out.lower()
    if "write timeout" in low or "timed out waiting for packet header" in low:
        return UsbStallError(
            "USB 쓰기가 시간 초과되었습니다 (엔드포인트 스톨).",
            "· USB 장치 리셋으로 복구됩니다 (자동 복구를 시도하세요)", output=out)
    if "resource busy" in low or "device or resource busy" in low:
        return PortBusyError(
            "포트를 다른 프로그램이 사용 중입니다.",
            "· 시리얼 모니터나 수신기를 종료하고 다시 시도하세요", output=out)
    if "permission denied" in low:
        return EsptoolError(
            "포트 접근 권한이 없습니다.",
            "· install.sh 실행 후 **재로그인** 이 필요합니다 (dialout 그룹)", output=out)
    if "no serial data received" in low:
        return EsptoolError(
            "디바이스가 응답하지 않습니다.",
            "· 충전 전용 USB 케이블이 아닌지 확인하세요\n"
            "· 보드에 USB 포트가 두 개면 다른 쪽에 꽂아보세요\n"
            "· 포트 선택이 올바른지 확인하세요", output=out)
    if "failed to connect" in low or "could not open" in low:
        return EsptoolError(
            "디바이스에 연결하지 못했습니다.",
            "· 포트 선택과 케이블을 확인하세요\n"
            "· '디바이스 확인' 으로 먼저 연결을 점검해 보세요", output=out)
    if "no module named esptool" in low:
        return EsptoolError(
            "esptool 이 설치되지 않았습니다.",
            "· ./install.sh 를 실행하세요 (sudo apt install esptool)", output=out)
    return EsptoolError("esptool 이 실패했습니다 (종료코드 %d)." % rc,
                        output=out)


# ===================== 기능 =====================
def detect_device(port, *, on_line=None):
    """칩 종류 / MAC / 플래시 크기를 읽는다 (쓰기 없음)."""
    preflight(port)
    out = run_esptool(
        ["--chip", "esp32s3", "--port", port, "--baud", str(NVS_BAUD)]
        + stub_args("esp32s3") + [subcmd("flash_id")],
        on_line=on_line, total_timeout=90)

    info = {"chip": None, "mac": None, "flash_size": None, "features": None}
    for line in out.splitlines():
        if line.startswith("Chip is "):
            info["chip"] = line[len("Chip is "):].strip()
        elif line.startswith("MAC: "):
            info["mac"] = line[len("MAC: "):].strip()
        elif line.startswith("Features: "):
            info["features"] = line[len("Features: "):].strip()
        elif "flash size:" in line.lower():
            info["flash_size"] = line.split(":", 1)[1].strip()
    if not info["chip"]:
        raise EsptoolError("칩 정보를 읽지 못했습니다.", output=out)
    return info


def flash_firmware(port, pkg_dir, *, on_line=None, on_progress=None,
                   on_status=None, cancel=None):
    """번들 펌웨어 3종을 디바이스에 굽는다.

    · manifest sha256 을 먼저 검증한다 (손상된 복사본이 디바이스에 닿지 않게)
    · **단일 esptool 호출**로 3개 이미지를 쓴다. 3번 나눠 부르면 연결·리셋이
      3배가 되어 USB 스톨 노출도 3배가 된다.
    · 쓰기 순서는 manifest 기록대로 app → partition-table → bootloader.
      부트로더(21KB, 1초 미만)를 마지막에 두면 중간에 끊겨도 부팅 불가 구간이 짧다.
    · NVS(0x9000) 는 건드리지 않는다. --erase-all 은 절대 쓰지 않는다.
    """
    manifest = fw_manifest.load_manifest(pkg_dir)

    if not manifest.get("wifi_prefer_nvs"):
        raise EsptoolError(
            "번들된 펌웨어가 배포용 빌드가 아닙니다 (CONFIG_WIFI_PREFER_NVS=n).",
            "· 이 펌웨어는 GUI 로 입력한 WiFi 를 무시합니다\n"
            "· 배포 담당자에게 정상 패키지를 요청하세요")

    if on_status:
        on_status("펌웨어 무결성 확인 중...")
    bad = fw_manifest.verify_images(pkg_dir, manifest)
    if bad:
        raise EsptoolError(
            "번들 펌웨어가 손상되었습니다:\n" +
            "\n".join("  %s — %s" % (f, why) for f, why in bad),
            "· 패키지를 다시 복사하거나 다시 받으세요")

    preflight(port)

    images = fw_manifest.image_paths_in_write_order(pkg_dir, manifest)
    chip = manifest.get("chip", "esp32s3")
    wargs = manifest.get("write_flash_args", [])

    last_err = None
    for baud in FW_BAUD_LADDER:
        args = (["--chip", chip, "--port", port, "--baud", str(baud),
                 "--before", "default_reset",
                 # no_reset: 굽자마자 우리가 직접 리셋해 부팅 로그를 첫 바이트부터 받는다.
                 # hard_reset 이면 파이썬이 포트를 여는 사이에 앞부분을 놓친다.
                 "--after", "no_reset"]
                + stub_args(chip)
                + [subcmd("write_flash")] + wargs)
        for offset, path in images:
            args += [offset, path]

        if on_status:
            on_status("펌웨어 쓰기 중... (%d bps)" % baud)
        if on_line:
            on_line("▶ %d bps 로 플래시 (이미지 %d개)" % (baud, len(images)))
        try:
            out = run_esptool(args, on_line=on_line, on_progress=on_progress,
                              total_timeout=TOTAL_TIMEOUT_FW, cancel=cancel)
        except UsbStallError:
            raise                       # 스톨은 래더로 해결되지 않는다 — 바로 올린다
        except EsptoolError as e:
            last_err = e
            continue

        # esptool 은 이미지마다 MD5 읽기검증을 하고 그 결과를 출력한다.
        verified = out.count("Hash of data verified.")
        if verified < len(images):
            raise EsptoolError(
                "플래시 검증이 부족합니다 (%d/%d)." % (verified, len(images)),
                "· 케이블을 확인하고 다시 굽기를 시도하세요", output=out)
        if on_progress:
            on_progress(100)
        return {"baud": baud, "images": len(images), "verified": verified,
                "manifest": manifest, "output": out}

    raise last_err or EsptoolError("펌웨어 플래시에 실패했습니다.")


def flash_nvs(port, bin_path, *, offset=NVS_OFFSET, baud=NVS_BAUD,
              verify=False, on_line=None, on_progress=None, on_status=None,
              cancel=None, chip="esp32s3"):
    """NVS 파티션(0x9000)에 설정 바이너리를 주입한다.

    기존 set_sensor_gui.flash_nvs 와 호환되는 시그니처를 유지한다.
    """
    preflight(port)
    if on_status:
        on_status("설정 주입 중...")
    args = (["--chip", chip, "--port", port, "--baud", str(baud),
             "--before", "default_reset", "--after", "no_reset"]
            + stub_args(chip) + [subcmd("write_flash"), hex(offset), bin_path])
    out = run_esptool(args, on_line=on_line, on_progress=on_progress,
                      total_timeout=TOTAL_TIMEOUT_NVS, cancel=cancel)
    if "Hash of data verified." not in out:
        raise EsptoolError("NVS 주입 검증에 실패했습니다.", output=out)

    if verify:
        if on_status:
            on_status("정밀 검증 중...")
        out += "\n" + run_esptool(
            ["--chip", chip, "--port", port, "--baud", str(baud),
             "--before", "no_reset", "--after", "no_reset"]
            + stub_args(chip) + [subcmd("verify_flash"), hex(offset), bin_path],
            on_line=on_line, total_timeout=TOTAL_TIMEOUT_NVS, cancel=cancel)
    if on_progress:
        on_progress(100)
    return out


# ===================== CLI =====================
def _p(line):
    print(line, flush=True)


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd = argv[1]

    if cmd == "which":
        _p("esptool  : %s" % describe_tool())
        _p("서브커맨드: %s / %s" % (subcmd("write_flash"), subcmd("flash_id")))
        ok = stub_available("esp32s3")
        _p("esp32s3 스텁: %s" % ("있음" if ok else "없음 → --no-stub 사용 (Debian 패키지는 제거됨)"))
        return 0

    if cmd == "detect" and len(argv) >= 3:
        try:
            info = detect_device(argv[2], on_line=_p)
        except EsptoolError as e:
            _p("❌ " + e.full())
            return 1
        _p("")
        for k in ("chip", "mac", "flash_size", "features"):
            if info.get(k):
                _p("  %-11s %s" % (k, info[k]))
        return 0

    if cmd == "flash-fw" and len(argv) >= 4:
        try:
            r = flash_firmware(argv[2], argv[3], on_line=_p,
                               on_status=lambda s: _p("[%s]" % s))
        except EsptoolError as e:
            _p("❌ " + e.full())
            return 1
        _p("✅ %d개 이미지 검증 완료 (%d bps)" % (r["verified"], r["baud"]))
        return 0

    if cmd == "flash-nvs" and len(argv) >= 4:
        try:
            flash_nvs(argv[2], argv[3], on_line=_p,
                      on_status=lambda s: _p("[%s]" % s))
        except EsptoolError as e:
            _p("❌ " + e.full())
            return 1
        _p("✅ NVS 주입 완료")
        return 0

    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
