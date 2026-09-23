#!/usr/bin/env python3
"""디바이스의 기존 설정 읽기 — NVS 파티션 되읽기 + 파싱

왜 필요한가:
    셋업 시 "기존 값이 있으면 있는 대로 보여주고, 없으면 입력받는다" 를 하려면
    PC 의 기억(~/.iis3dwb_sensor_setup.json)이 아니라 **디바이스의 실제 NVS** 를
    읽어야 한다. 특히 WiFi 비밀번호는 PC 에 저장하지 않는 것이 바람직하므로,
    디바이스에서 읽어와 채우는 편이 안전하고 정확하다.

주의 — NVS 파티션에는 devcfg 만 있는 것이 아니다:
    ESP-IDF WiFi 스택이 같은 파티션에 nvs.net80211 / misc 등을 쓴다.
    실기기 확인 결과 다음 네임스페이스가 공존한다:
        devcfg (우리 설정), misc, nvs.net80211, ap.sndchan ...
    따라서 파서는 devcfg 네임스페이스의 키만 골라내야 한다.
    그리고 설정툴이 24KB 이미지를 통째로 쓰면 WiFi 스택 데이터도 함께 지워진다
    (재생성되므로 기능상 문제는 없다).

보안:
    wifi_pass 는 NVS 에 평문으로 저장된다(펌웨어 설계). 이 모듈은 값을 돌려주지만
    CLI 출력에서는 기본 마스킹한다. --show-secrets 로만 노출한다.

CLI:
    python3 nvs_read.py device /dev/ttyACM0
    python3 nvs_read.py file <nvs.bin> [--show-secrets]
"""

import argparse
import os
import struct
import sys
import tempfile

PAGE_SIZE = 4096
ENTRY_SIZE = 32
ENTRIES_PER_PAGE = 126
ENTRY_TABLE_OFFSET = 32
ENTRY_DATA_OFFSET = 64

# 엔트리 상태 비트맵: 2비트/엔트리. 0b10 = written, 0b00 = erased, 0b11 = empty
STATE_WRITTEN = 0b10
STATE_ERASED = 0b00

TYPE_U8 = 0x01
TYPE_I8 = 0x11
TYPE_U16 = 0x02
TYPE_I16 = 0x12
TYPE_U32 = 0x04
TYPE_I32 = 0x14
TYPE_STR = 0x21
TYPE_BLOB_DATA = 0x42
TYPE_BLOB_IDX = 0x48

DEVCFG_KEYS = ("wifi_ssid", "wifi_pass", "srv_ip", "srv_port",
               "stream_rate", "transport", "read_mode", "full_scale_g")
SECRET_KEYS = ("wifi_pass",)


class NvsReadError(Exception):
    pass


def _entry_state(page, idx):
    """엔트리 상태 비트맵에서 idx 번째 엔트리의 2비트 상태를 꺼낸다."""
    byte = page[ENTRY_TABLE_OFFSET + idx // 4]
    return (byte >> ((idx % 4) * 2)) & 0b11


def parse_nvs(data, namespace="devcfg"):
    """NVS 파티션 바이너리에서 지정 네임스페이스의 key→value 를 돌려준다.

    여러 페이지를 순서대로 훑으며, 나중에 쓰인 값이 앞의 값을 덮는다
    (NVS 는 추가 기록 방식이라 같은 키가 여러 번 나타날 수 있다).
    """
    ns_index = None
    out = {}

    for page_start in range(0, len(data), PAGE_SIZE):
        page = data[page_start:page_start + PAGE_SIZE]
        if len(page) < PAGE_SIZE:
            break
        state = struct.unpack_from("<I", page, 0)[0]
        # 0xFFFFFFFF = 미사용 페이지. ACTIVE(0xFFFFFFFE) / FULL(0xFFFFFFFC) 등만 본다.
        if state == 0xFFFFFFFF:
            continue

        i = 0
        while i < ENTRIES_PER_PAGE:
            if _entry_state(page, i) != STATE_WRITTEN:
                i += 1
                continue
            off = ENTRY_DATA_OFFSET + i * ENTRY_SIZE
            e = page[off:off + ENTRY_SIZE]
            if len(e) < ENTRY_SIZE:
                break
            ns, ty, span = e[0], e[1], e[2]
            key = e[8:24].split(b"\x00", 1)[0].decode("utf-8", "replace")

            # 네임스페이스 등록 엔트리: ns==0, 값(offset 24)이 그 네임스페이스의 인덱스
            if ns == 0 and ty == TYPE_U8:
                if key == namespace:
                    ns_index = e[24]
                i += max(1, span)
                continue

            if ns_index is None or ns != ns_index:
                i += max(1, span)
                continue

            if ty == TYPE_STR:
                size = struct.unpack_from("<H", e, 24)[0]
                blob = page[off + ENTRY_SIZE: off + span * ENTRY_SIZE]
                val = blob[:max(0, size - 1)].decode("utf-8", "replace")
                out[key] = val
            elif ty in (TYPE_U8, TYPE_I8):
                val = e[24]
                out[key] = val - 256 if (ty == TYPE_I8 and val > 127) else val
            elif ty in (TYPE_U16, TYPE_I16):
                fmt = "<h" if ty == TYPE_I16 else "<H"
                out[key] = struct.unpack_from(fmt, e, 24)[0]
            elif ty in (TYPE_U32, TYPE_I32):
                fmt = "<i" if ty == TYPE_I32 else "<I"
                out[key] = struct.unpack_from(fmt, e, 24)[0]
            # blob 은 이 프로젝트의 devcfg 에 쓰이지 않아 생략
            i += max(1, span)

    return out


def list_namespaces(data):
    """파티션에 존재하는 네임스페이스 이름들 (진단용)."""
    names = []
    for page_start in range(0, len(data), PAGE_SIZE):
        page = data[page_start:page_start + PAGE_SIZE]
        if len(page) < PAGE_SIZE:
            break
        if struct.unpack_from("<I", page, 0)[0] == 0xFFFFFFFF:
            continue
        for i in range(ENTRIES_PER_PAGE):
            if _entry_state(page, i) != STATE_WRITTEN:
                continue
            off = ENTRY_DATA_OFFSET + i * ENTRY_SIZE
            e = page[off:off + ENTRY_SIZE]
            if len(e) == ENTRY_SIZE and e[0] == 0 and e[1] == TYPE_U8:
                nm = e[8:24].split(b"\x00", 1)[0].decode("utf-8", "replace")
                if nm not in names:
                    names.append(nm)
    return names


def read_from_device(port, *, offset=0x9000, size=0x6000, on_line=None):
    """디바이스에서 NVS 파티션을 읽어 파싱한 dict 를 돌려준다.

    스텁 없는 esptool(Debian 패키지)에서는 고속 읽기가 실패하므로 115200 을 쓴다.
    (460800 + --no-stub 은 "Packet content transfer stopped" 로 실패하는 것을 확인)
    """
    import esp_flash

    esp_flash.preflight(port)
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "nvs_readback.bin")
        args = (["--chip", "esp32s3", "--port", port, "--baud", "115200",
                 "--before", "default_reset", "--after", "no_reset"]
                + esp_flash.stub_args("esp32s3")
                + [esp_flash.subcmd("read_flash"), hex(offset), hex(size), out])
        esp_flash.run_esptool(args, on_line=on_line, total_timeout=240)
        if not os.path.exists(out):
            raise NvsReadError("NVS 읽기 결과 파일이 없습니다.")
        data = open(out, "rb").read()
    if len(data) < PAGE_SIZE:
        raise NvsReadError("NVS 읽기 크기가 too small: %d" % len(data))
    return parse_nvs(data)


def to_gui_prefill(devcfg):
    """파싱 결과를 GUI 입력 필드 형태로 바꾼다. 없는 값은 키를 빼서 돌려준다.

    "있으면 있는 대로 보여주고, 없으면 없는 대로 입력받는다" 를 위한 어댑터.
    """
    out = {}
    if devcfg.get("wifi_ssid"):
        out["ssid"] = devcfg["wifi_ssid"]
    if devcfg.get("wifi_pass"):
        out["pw"] = devcfg["wifi_pass"]
    if devcfg.get("srv_ip") and devcfg["srv_ip"] not in ("0.0.0.0",):
        out["srv_ip"] = devcfg["srv_ip"]
    if devcfg.get("srv_port"):
        out["srv_port"] = int(devcfg["srv_port"])
    for src, dst in (("stream_rate", "rate"), ("transport", "transport"),
                     ("read_mode", "read_mode"), ("full_scale_g", "full_scale_g")):
        if src in devcfg:
            out[dst] = int(devcfg[src])
    return out


def describe(devcfg, *, show_secrets=False):
    """사람이 읽을 요약. 비밀번호는 기본 마스킹."""
    if not devcfg:
        return "디바이스에 저장된 설정이 없습니다 (공장 초기 상태)."
    label = {
        "wifi_ssid": "WiFi 이름",
        "wifi_pass": "WiFi 비밀번호",
        "srv_ip": "서버 IP",
        "srv_port": "서버 포트",
        "stream_rate": "측정 속도(rate_step)",
        "transport": "전송 방식",
        "read_mode": "읽기 방식",
        "full_scale_g": "측정 범위(g)",
    }
    tname = {0: "WiFi UDP", 1: "TCP", 2: "USB 직결(시리얼)"}
    lines = []
    for k in DEVCFG_KEYS:
        if k not in devcfg:
            lines.append("   %-20s (없음)" % label.get(k, k))
            continue
        v = devcfg[k]
        if k in SECRET_KEYS and not show_secrets:
            v = "*" * len(str(v)) + " (%d자)" % len(str(v))
        elif k == "transport":
            v = "%s (%s)" % (v, tname.get(v, "?"))
        lines.append("   %-20s %s" % (label.get(k, k), v))
    return "\n".join(lines)


def main(argv):
    ap = argparse.ArgumentParser(description="디바이스 NVS 설정 읽기")
    ap.add_argument("source", choices=["device", "file"])
    ap.add_argument("target")
    ap.add_argument("--show-secrets", action="store_true",
                    help="WiFi 비밀번호를 마스킹하지 않고 출력")
    ap.add_argument("--namespaces", action="store_true",
                    help="파티션의 모든 네임스페이스 나열 (진단)")
    args = ap.parse_args(argv[1:])

    try:
        if args.source == "device":
            cfg = read_from_device(args.target,
                                   on_line=lambda l: print("  " + l, flush=True))
            data = None
        else:
            data = open(args.target, "rb").read()
            cfg = parse_nvs(data)
    except Exception as e:
        print("❌ %s: %s" % (type(e).__name__, e), file=sys.stderr)
        return 1

    print("\n=== 디바이스에 저장된 설정 (devcfg) ===")
    print(describe(cfg, show_secrets=args.show_secrets))
    if args.namespaces and data is not None:
        print("\n=== 파티션 내 네임스페이스 ===")
        for n in list_namespaces(data):
            print("   %s%s" % (n, "  ← 우리 설정" if n == "devcfg" else ""))
    print("\n=== GUI 프리필 값 ===")
    pre = to_gui_prefill(cfg)
    for k, v in pre.items():
        if k == "pw":
            v = "*" * len(str(v))
        print("   %-14s %r" % (k, v))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
