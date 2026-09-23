#!/usr/bin/env python3
"""부팅 로그 캡처 + 판정 — 플래시/주입이 실제로 반영됐는지 확인한다

배포 환경에는 ESP-IDF 가 없어 `idf.py monitor` 를 쓸 수 없다. pyserial 로 직접
UART0 콘솔을 읽어 판정한다.

**1차 판정 근거는 WiFi 접속 성공이 아니라, 펌웨어가 되울려주는 NVS 값이다.**
  config_manager.c:234  "스트리밍 설정 로드 (서버 %s:%u, rate=%u, transport=%u, fs=±%ug)"
  config_manera.c:125   "WiFi 설정 로드 완료 (SSID: %s)"
입력값과 필드별로 비교하면, AP 가 안 잡히는 환경에서도 "주입은 됐다" 를 증명할 수 있다.

CLI:
    python3 boot_log.py capture /dev/ttyACM0 [--timeout 30]
    python3 boot_log.py selftest
"""

import argparse
import os
import re
import sys
import time

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# ---- 스트리밍 판정 마커 ----
# 주의: main.c:639 은 sensor_streamer_start() **호출 전에**
#   "Step 4: 센서 데이터 스트리밍 시작" 을 찍고, main.c:663 은
#   "스트리밍 시작 실패: %s" 를 찍는다. 두 줄 모두 "스트리밍 시작" 과
#   "센서 데이터 스트리밍" 을 포함하므로, 그 부분문자열을 성공 마커로 쓰면
#   **실패를 성공으로 오판한다**(Rust 설정툴의 실제 결함).
# 따라서 실패를 먼저 보고, 성공은 화살표까지 포함한 문자열로 판정한다.
STREAM_FAIL = ("스트리밍 시작 실패", "스트리밍 비활성")
STREAM_OK = ("스트리밍 시작 → ",              # sensor_streamer.c:501/506 (성공 후)
             "센서 데이터 스트리밍 중",        # main.c:656
             "[스트리밍] 패킷=")               # main.c:675

SENSOR_FAIL = ("센서를 찾을 수 없습니다",)

CRED_NVS = "WiFi 자격증명 출처: NVS(devcfg)"
CRED_KCONFIG_FALLBACK = "WiFi 자격증명 출처: Kconfig 기본값"
CRED_KCONFIG_DEVBUILD = "WiFi 자격증명 출처: Kconfig (개발 빌드"

# config_manager.c:234 의 형식을 되읽는다
STREAM_CFG_RE = re.compile(
    r"스트리밍 설정 로드 \(서버 ([0-9.]+):(\d+), rate=(\d+), transport=(\d+), fs=±(\d+)g\)")
WIFI_CFG_RE = re.compile(r"WiFi 설정 로드 완료 \(SSID: (.*?)\)")

# 디바이스 IP 는 이 줄들에만 앵커링한다.
# 로그에는 사용자가 입력한 **서버 IP** 도 함께 찍히므로(sensor_streamer.c:506
# "스트리밍 시작 → <srv_ip>:<port>"), 일반 정규식으로 긁으면 서버 IP 를
# 디바이스 IP 로 오인한다.
DEVICE_IP_RE = (
    re.compile(r"sta ip:\s*([0-9]{1,3}(?:\.[0-9]{1,3}){3})"),
    re.compile(r"Got IP address:\s*([0-9]{1,3}(?:\.[0-9]{1,3}){3})"),
)

EXCERPT_KEYS = ("WIFI_MGR", "WIFI_APP", "CONFIG_MGR", "STREAMER", "IIS3DWB",
                "자격증명", "설정 로드", "스트리밍", "Disconnected", "reason",
                "Got IP", "sta ip", "센서를", "Project name", "App version")


def strip_ansi(text):
    return ANSI_RE.sub("", text)


def _valid_ip(s):
    parts = s.split(".")
    return len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)


class BootStatus:
    """부팅 로그 판정 결과."""

    def __init__(self):
        self.cred_source = "unknown"   # nvs | kconfig_fallback | kconfig_devbuild | unknown
        self.wifi_status = "unknown"   # connected | ssid_not_found | auth_failed | trying
                                       #   | skipped_serial_mode | unknown
        self.device_ip = None
        self.nvs_ssid = None
        self.nvs_stream = None         # dict(server_ip, server_port, rate, transport, fs)
        self.streaming = False
        self.stream_fail_reason = None
        self.sensor_missing = False
        self.app_name = None
        self.app_version = None
        self.mismatches = []           # [(field, expected, actual)]
        self.excerpt = ""
        self.raw = ""

    @property
    def deploy_build(self):
        """번들 펌웨어가 배포용(CONFIG_WIFI_PREFER_NVS=y)인지."""
        return self.cred_source != "kconfig_devbuild"

    @property
    def config_applied(self):
        """주입한 설정이 펌웨어에 반영됐는지 (불일치가 없고 설정을 읽었음)."""
        return self.nvs_stream is not None and not self.mismatches

    def summary(self):
        lines = []
        if self.app_name:
            lines.append("펌웨어: %s %s" % (self.app_name, self.app_version or ""))
        lines.append("자격증명 출처: %s" % {
            "nvs": "NVS (설정툴 주입값) ✅",
            "kconfig_fallback": "Kconfig 폴백 (NVS 미설정)",
            "kconfig_devbuild": "Kconfig — 개발 빌드, NVS 무시 ❌",
            "unknown": "확인 불가",
        }[self.cred_source])
        if self.nvs_stream:
            s = self.nvs_stream
            lines.append("펌웨어가 읽은 설정: 서버 %s:%s, rate=%s, transport=%s, fs=±%sg"
                         % (s["server_ip"], s["server_port"], s["rate"],
                            s["transport"], s["fs"]))
        if self.nvs_ssid:
            lines.append("펌웨어가 읽은 SSID: %s" % self.nvs_ssid)
        if self.mismatches:
            lines.append("불일치:")
            for f, exp, act in self.mismatches:
                lines.append("   %s: 입력 %r ≠ 디바이스 %r" % (f, exp, act))
        lines.append("WiFi: %s%s" % (self.wifi_status,
                                     " (%s)" % self.device_ip if self.device_ip else ""))
        lines.append("스트리밍: %s" % ("시작됨 ✅" if self.streaming
                                     else (self.stream_fail_reason or "미시작")))
        if self.sensor_missing:
            lines.append("⚠ 센서를 찾을 수 없습니다 — 하드웨어 문제입니다")
        return "\n".join(lines)


def parse_boot_log(text, expected=None):
    """부팅 로그를 판정한다.

    expected: collect() 가 만든 cfg dict (있으면 필드별 비교를 수행)
    """
    text = strip_ansi(text)
    st = BootStatus()
    st.raw = text

    for line in text.splitlines():
        if "Project name:" in line:
            st.app_name = line.split(":", 1)[1].strip()
        elif "App version:" in line:
            st.app_version = line.split(":", 1)[1].strip()

        if CRED_NVS in line:
            st.cred_source = "nvs"
        elif CRED_KCONFIG_FALLBACK in line:
            st.cred_source = "kconfig_fallback"
        elif CRED_KCONFIG_DEVBUILD in line:
            st.cred_source = "kconfig_devbuild"

        m = STREAM_CFG_RE.search(line)
        if m:
            st.nvs_stream = {
                "server_ip": m.group(1), "server_port": int(m.group(2)),
                "rate": int(m.group(3)), "transport": int(m.group(4)),
                "fs": int(m.group(5)),
            }
        m = WIFI_CFG_RE.search(line)
        if m:
            st.nvs_ssid = m.group(1)

        for rex in DEVICE_IP_RE:
            m = rex.search(line)
            if m and _valid_ip(m.group(1)):
                st.device_ip = m.group(1)

        if any(k in line for k in SENSOR_FAIL):
            st.sensor_missing = True

    # ---- 스트리밍: 실패를 먼저 본다 (부분문자열 함정 회피) ----
    for k in STREAM_FAIL:
        if k in text:
            st.stream_fail_reason = k
            break
    if not st.stream_fail_reason:
        st.streaming = any(k in text for k in STREAM_OK)

    # ---- WiFi 상태 ----
    if "USB 직결" in text or (st.nvs_stream and st.nvs_stream["transport"] == 2):
        st.wifi_status = "skipped_serial_mode"
    elif ("Got IP address" in text) or ("WiFi Connected Successfully" in text):
        st.wifi_status = "connected"
    else:
        fail_lines = [l for l in text.splitlines()
                      if "Disconnected" in l or "reason:" in l]
        joined = "\n".join(fail_lines)
        no_ap = (joined.count("reason: 201") + joined.count("reason: 205")
                 + joined.count("NO_AP_FOUND"))
        # "reason: 2" 는 "reason: 201/205" 의 부분문자열이라 그대로 세면 안 된다.
        auth = (len(re.findall(r"reason: 15\b", joined))
                + len(re.findall(r"reason: 2\b", joined))
                + len(re.findall(r"reason: 3\b", joined))
                + joined.count("HANDSHAKE") + joined.count("AUTH_"))
        if no_ap >= 2:
            st.wifi_status = "ssid_not_found"
        elif auth >= 1:
            st.wifi_status = "auth_failed"
        elif fail_lines:
            st.wifi_status = "trying"
        else:
            st.wifi_status = "unknown"

    # ---- 입력값 vs 펌웨어가 읽은 값 ----
    if expected and st.nvs_stream:
        s = st.nvs_stream
        pairs = [
            ("서버 IP", str(expected.get("srv_ip")), s["server_ip"]),
            ("서버 포트", int(expected.get("srv_port", 0)), s["server_port"]),
            ("측정 속도(rate)", int(expected.get("rate", 0)), s["rate"]),
            ("전송 방식(transport)", int(expected.get("transport", 0)), s["transport"]),
            ("측정 범위(fs)", int(expected.get("full_scale_g", 0)), s["fs"]),
        ]
        for name, exp, act in pairs:
            # USB 직결 모드에서는 서버 IP/포트를 쓰지 않으므로 비교 제외
            if s["transport"] == 2 and name in ("서버 IP", "서버 포트"):
                continue
            if exp != act:
                st.mismatches.append((name, exp, act))
    if expected and st.nvs_ssid and expected.get("ssid"):
        if st.nvs_ssid != expected["ssid"]:
            st.mismatches.append(("SSID", expected["ssid"], st.nvs_ssid))

    st.excerpt = "\n".join(
        l.strip() for l in text.splitlines()
        if l.strip() and any(k in l for k in EXCERPT_KEYS))[:4000]
    return st


def capture(port, *, baud=115200, timeout=30, on_line=None, reset=True,
            stop_on=None):
    """리셋 후 부팅 로그를 캡처해 문자열로 돌려준다.

    포트를 열 때 DTR/RTS 를 건드리지 않는다(열자마자 리셋되면 앞부분을 놓친다).
    그 다음 RTS 만 토글해 정상 부팅시킨다. BOOT(=DTR) 는 계속 해제해 둬야
    다운로드 모드로 들어가지 않는다.
    """
    import serial                                  # 지연 import (없을 때 안내)

    s = serial.Serial()
    s.port = port
    s.baudrate = baud
    s.timeout = 0.2
    s.dtr = False          # BOOT high — 일반 부팅
    s.rts = False
    s.open()
    try:
        s.reset_input_buffer()
        if reset:
            s.rts = True                           # EN low
            time.sleep(0.15)
            s.rts = False                          # EN high → 부팅
            s.reset_input_buffer()

        buf = b""
        deadline = time.time() + timeout
        hit = None
        while time.time() < deadline:
            chunk = s.read(4096)
            if chunk:
                buf += chunk
                if on_line:
                    # 새로 들어온 완성된 줄만 넘긴다
                    text = strip_ansi(buf.decode("utf-8", "replace"))
                    while "\n" in text:
                        line, text = text.split("\n", 1)
                        line = line.strip()
                        if line:
                            on_line(line)
                    buf = text.encode("utf-8")
                    full = None
                else:
                    full = None
            if stop_on:
                cur = strip_ansi(buf.decode("utf-8", "replace")) if not on_line else None
                # on_line 모드에서는 누적 원문을 따로 들고 있지 않으므로
                # stop_on 판정은 비활성 (아래 capture_full 사용)
                if cur and any(k in cur for k in stop_on):
                    hit = True
                    break
        return strip_ansi(buf.decode("utf-8", "replace")), hit
    finally:
        s.close()


def capture_full(port, *, baud=115200, timeout=30, on_line=None, reset=True,
                 success_markers=(), failure_markers=(), settle=0.6):
    """capture 의 실사용 버전 — 원문을 누적하면서 마커로 조기 종료한다."""
    import serial

    s = serial.Serial()
    s.port = port
    s.baudrate = baud
    s.timeout = 0.2
    s.dtr = False
    s.rts = False
    s.open()
    acc = ""
    pending = ""
    try:
        s.reset_input_buffer()
        if reset:
            s.rts = True
            time.sleep(0.15)
            s.rts = False
            s.reset_input_buffer()

        deadline = time.time() + timeout
        while time.time() < deadline:
            chunk = s.read(4096)
            if not chunk:
                continue
            text = strip_ansi(chunk.decode("utf-8", "replace"))
            acc += text
            if on_line:
                pending += text
                while "\n" in pending:
                    line, pending = pending.split("\n", 1)
                    line = line.strip()
                    if line:
                        on_line(line)
            if failure_markers and any(k in acc for k in failure_markers):
                break
            if success_markers and any(k in acc for k in success_markers):
                # 성공 직후 몇 줄(IP 등)이 더 나오므로 잠깐 더 받는다
                end = time.time() + settle
                while time.time() < end:
                    extra = s.read(4096)
                    if extra:
                        t = strip_ansi(extra.decode("utf-8", "replace"))
                        acc += t
                        if on_line:
                            pending += t
                            while "\n" in pending:
                                line, pending = pending.split("\n", 1)
                                if line.strip():
                                    on_line(line.strip())
                break
        if on_line and pending.strip():
            on_line(pending.strip())
        return acc
    finally:
        s.close()


def verify_after_inject(port, expected, *, on_line=None, timeout=None):
    """설정 주입 후 리셋해 반영 여부를 판정한다."""
    transport = int(expected.get("transport", 0)) if expected else 0
    # USB 직결 모드는 WiFi 를 건너뛰므로 오래 기다릴 필요가 없다
    if timeout is None:
        timeout = 20 if transport == 2 else 75
    text = capture_full(
        port, timeout=timeout, on_line=on_line,
        success_markers=STREAM_OK + ("[스트리밍] 패킷=",),
        failure_markers=STREAM_FAIL + SENSOR_FAIL,
    )
    return parse_boot_log(text, expected)


def verify_after_flash(port, *, on_line=None, timeout=30):
    """펌웨어 플래시 후 부팅 확인 (설정 비교는 하지 않음)."""
    text = capture_full(
        port, timeout=timeout, on_line=on_line,
        success_markers=("자격증명 출처", "Step 2", "스트리밍 시작 → ",
                         "스트리밍 비활성"),
        failure_markers=SENSOR_FAIL,
    )
    return parse_boot_log(text, None)


# ===================== 자가 테스트 =====================
_FIXTURES = [
    # (이름, 로그, 기대값)
    ("실패를 성공으로 오판하지 않는다",
     "I (639) MAIN: Step 4: 센서 데이터 스트리밍 시작\n"
     "E (663) MAIN: 스트리밍 시작 실패: ESP_ERR_NO_MEM\n",
     dict(streaming=False)),
    ("Step 4 로그만으로 성공 판정하지 않는다",
     "I (639) MAIN: Step 4: 센서 데이터 스트리밍 시작\n",
     dict(streaming=False)),
    ("실제 성공 마커(화살표)를 인식한다",
     "I (6090) STREAMER: 스트리밍 시작 → 192.168.0.77:9000 (1000 Hz, 폴링, ±4g)\n",
     dict(streaming=True)),
    ("통계 줄로도 성공 판정",
     "I (16107) MAIN: [스트리밍] 패킷=50 샘플=10000 드롭=0 에러=0 INT=0\n",
     dict(streaming=True)),
    ("스트리밍 비활성은 실패",
     "I (6112) MAIN: 스트리밍 비활성 (센서/WiFi/서버설정 중 하나 미충족)\n",
     dict(streaming=False)),
    ("개발 빌드를 검출한다",
     "I (2500) WIFI_APP: WiFi 자격증명 출처: Kconfig (개발 빌드 — NVS 무시)\n",
     dict(cred_source="kconfig_devbuild", deploy_build=False)),
    ("배포 빌드 + NVS 주입값",
     "I (2512) WIFI_APP: WiFi 자격증명 출처: NVS(devcfg) — 설정 툴 주입값\n",
     dict(cred_source="nvs", deploy_build=True)),
    ("NVS 미설정 폴백",
     "I (2500) WIFI_APP: WiFi 자격증명 출처: Kconfig 기본값 (NVS 미설정)\n",
     dict(cred_source="kconfig_fallback", deploy_build=True)),
    ("디바이스 IP 를 서버 IP 와 혼동하지 않는다",
     "I (6090) STREAMER: 스트리밍 시작 → 192.168.0.77:9000 (1000 Hz, 폴링, ±4g)\n"
     "I (4429) esp_netif_handlers: sta ip: 192.168.0.110, mask: 255.255.255.0\n",
     dict(device_ip="192.168.0.110")),
    ("ANSI 색상 코드를 제거한다",
     "\x1b[42m\x1b[1;37m  \xeb\x9d\xbc".decode("utf-8", "replace") if False else
     "\x1b[42m  📡 센서 데이터 스트리밍 중!  \x1b[0m\n",
     dict(streaming=True)),
    ("센서 없음을 하드웨어 문제로 분류",
     "  ✗ 센서를 찾을 수 없습니다  \n",
     dict(sensor_missing=True)),
    ("SSID 없음(reason 201)",
     "W (5000) WIFI_MGR: Disconnected reason: 201\n"
     "W (6000) WIFI_MGR: Disconnected reason: 201\n",
     dict(wifi_status="ssid_not_found")),
    ("비밀번호 오류(reason 15)",
     "W (5000) WIFI_MGR: Disconnected reason: 15\n",
     dict(wifi_status="auth_failed")),
]


def selftest():
    ok = True
    for name, log, exp in _FIXTURES:
        st = parse_boot_log(log)
        for k, v in exp.items():
            actual = getattr(st, k)
            if actual != v:
                print("❌ %s: %s = %r (기대 %r)" % (name, k, actual, v))
                ok = False
                break
        else:
            print("✅ %s" % name)

    # 설정 비교 테스트
    log = ("I (2473) CONFIG_MGR: 스트리밍 설정 로드 "
           "(서버 192.168.0.77:9000, rate=0, transport=0, fs=±4g)\n")
    st = parse_boot_log(log, dict(srv_ip="192.168.0.77", srv_port=9000, rate=0,
                                  transport=0, full_scale_g=4))
    if st.mismatches or not st.config_applied:
        print("❌ 일치하는 설정을 불일치로 판정: %r" % st.mismatches); ok = False
    else:
        print("✅ 입력값과 일치하면 config_applied=True")

    st = parse_boot_log(log, dict(srv_ip="192.168.0.99", srv_port=9000, rate=2,
                                  transport=0, full_scale_g=16))
    fields = {m[0] for m in st.mismatches}
    if fields != {"서버 IP", "측정 속도(rate)", "측정 범위(fs)"}:
        print("❌ 불일치 필드 검출 실패: %r" % st.mismatches); ok = False
    else:
        print("✅ 불일치 3개 필드를 정확히 검출")

    print("\n%s" % ("전체 통과" if ok else "실패 있음"))
    return 0 if ok else 1


def main(argv):
    ap = argparse.ArgumentParser(description="부팅 로그 캡처/판정")
    ap.add_argument("cmd", choices=["capture", "selftest"])
    ap.add_argument("port", nargs="?")
    ap.add_argument("--timeout", type=float, default=30)
    args = ap.parse_args(argv[1:])

    if args.cmd == "selftest":
        return selftest()

    if not args.port:
        print("포트를 지정하세요", file=sys.stderr)
        return 2
    text = capture_full(args.port, timeout=args.timeout,
                        on_line=lambda l: print("  " + l, flush=True))
    print("\n" + "=" * 60)
    print(parse_boot_log(text).summary())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
