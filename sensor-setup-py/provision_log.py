#!/usr/bin/env python3
"""작업 이력 — 어느 보드(MAC)에 무엇을 넣었는지 기록한다.

    python3 provision_log.py              전체 이력 보기
    python3 provision_log.py 9c:13:...    한 보드만
    python3 provision_log.py --forget MAC 한 건 삭제

## 왜 필요한가

보드를 여러 대 다루면 **"이 보드에 어떤 서버 IP·포트를 넣었나"** 를 되짚을 일이
생긴다. 그런데 되짚을 방법이 마땅치 않다.

  · 브리지칩에 고유 일련번호가 없어 USB 로는 보드를 구분할 수 없다
  · 패킷 헤더에도 장비 식별자가 없다
  · NVS 를 직접 읽으려면 스트리밍을 끊고 esptool 로 부트로더에 넣어야 한다

**MAC 은 보드마다 다르고 esptool 로 읽을 수 있는 유일한 식별자다.** 그래서
MAC 을 열쇠로 작업 내용을 남긴다. 수집기가 쓰는 설정 폴더를 함께 써서,
나중에 수집 데이터와 대조할 때 한 곳만 보면 되게 한다.

## 비밀번호는 기록하지 않는다

WiFi 비밀번호는 이 파일에 넣지 않는다. 이력은 "무엇을 넣었나" 를 되짚는 용도이고,
비밀번호는 그 목적에 필요하지 않다. 파일 권한도 0600 으로 좁힌다.
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path


def _config_home():
    """설정 폴더의 상위 경로.

    sudo 로 실행될 때 /root 를 보지 않도록 SUDO_USER 를 확인한다
    (rpi-collector/slots.py 와 같은 규칙 — 같은 폴더를 공유하므로 맞춰야 한다).
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


# 수집기와 같은 폴더를 쓴다 — 설정과 이력이 흩어지지 않게.
CONFIG_DIR = _config_home() / "iis3dwb-collector"
LOG_PATH = CONFIG_DIR / "provisioned.json"

SCHEMA = 1

# 이력에 남기는 항목. **wifi_pass 는 의도적으로 제외한다.**
FIELDS = ("slot", "sensor", "ssid", "srv_ip", "srv_port",
          "rate", "transport", "read_mode", "full_scale_g",
          "fw_version", "fw_package")


def load():
    """{MAC소문자: 기록} 사전. 파일이 없거나 깨졌으면 빈 사전."""
    try:
        d = json.loads(LOG_PATH.read_text(encoding="utf-8"))
        if isinstance(d, dict) and isinstance(d.get("boards"), dict):
            return {str(k).lower(): v for k, v in d["boards"].items()}
    except (OSError, ValueError):
        pass
    return {}


def save(boards):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    LOG_PATH.write_text(
        json.dumps({"schema": SCHEMA, "boards": boards},
                   ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    try:
        os.chmod(LOG_PATH, 0o600)
    except OSError:
        pass


def record(mac, cfg, *, slot=None, sensor=None, fw_version=None,
           fw_package=None, at=None):
    """한 보드의 작업 내용을 남긴다. cfg 는 설정툴 collect() 의 결과.

    같은 MAC 을 다시 기록하면 **덮어쓴다** — 마지막으로 넣은 설정이 지금 보드에
    들어 있는 값이므로, 이력을 쌓기보다 현재 상태를 반영하는 것이 맞다.
    직전 값은 `previous` 에 한 단계만 남겨 되짚을 수 있게 한다.
    """
    if not mac:
        raise ValueError("MAC 이 없으면 기록할 수 없습니다 (보드를 특정할 수 없음).")
    mac = mac.lower()
    boards = load()

    entry = {
        "at": at or datetime.now().isoformat(timespec="seconds"),
        "slot": slot,
        "sensor": sensor,
        "ssid": cfg.get("ssid"),
        "srv_ip": cfg.get("srv_ip"),
        "srv_port": cfg.get("srv_port"),
        "rate": cfg.get("rate"),
        "transport": cfg.get("transport"),
        "read_mode": cfg.get("read_mode"),
        "full_scale_g": cfg.get("full_scale_g"),
        "fw_version": fw_version,
        "fw_package": fw_package,
    }
    # 비밀번호가 실수로 섞여 들어오지 않았는지 단정한다. 이 파일에는 들어가면 안 된다.
    assert "pw" not in entry and "wifi_pass" not in entry

    prev = boards.get(mac)
    if prev:
        prev = {k: v for k, v in prev.items() if k != "previous"}
        entry["previous"] = prev
    boards[mac] = entry
    save(boards)
    return entry


def forget(mac):
    boards = load()
    if mac.lower() in boards:
        del boards[mac.lower()]
        save(boards)
        return True
    return False


TRANSPORT_LABEL = {0: "WiFi UDP", 1: "TCP", 2: "USB 직결"}
RATE_LABEL = {0: "1 kHz", 1: "3.3 kHz", 2: "6.6 kHz", 3: "13.3 kHz", 4: "26.6 kHz"}


def describe(mac, entry):
    """사람이 읽는 한 건 요약."""
    tr = entry.get("transport")
    lines = ["%s   (%s)" % (mac, entry.get("at") or "?")]
    if entry.get("sensor") or entry.get("slot"):
        lines.append("  센서 %s · 슬롯 %s"
                     % (entry.get("sensor") or "?",
                        _short_slot(entry.get("slot")) or "?"))
    lines.append("  전송 %s" % TRANSPORT_LABEL.get(tr, tr))
    if tr == 0:
        lines.append("  WiFi %s → %s:%s"
                     % (entry.get("ssid") or "?", entry.get("srv_ip") or "?",
                        entry.get("srv_port") or "?"))
    lines.append("  측정 %s · ±%sg · %s"
                 % (RATE_LABEL.get(entry.get("rate"), entry.get("rate")),
                    entry.get("full_scale_g"),
                    "인터럽트" if entry.get("read_mode") == 1 else "폴링"))
    if entry.get("fw_version"):
        lines.append("  펌웨어 %s%s"
                     % (entry["fw_version"],
                        " (패키지 %s)" % entry["fw_package"]
                        if entry.get("fw_package") else ""))
    if entry.get("previous"):
        lines.append("  직전: %s → %s:%s (%s)"
                     % (TRANSPORT_LABEL.get(entry["previous"].get("transport"), "?"),
                        entry["previous"].get("srv_ip") or "-",
                        entry["previous"].get("srv_port") or "-",
                        entry["previous"].get("at") or "?"))
    return "\n".join(lines)


def _short_slot(slot):
    if not slot:
        return None
    i = slot.rfind("-usb-")
    return slot[i + 1:] if i >= 0 else slot


def summary_line(mac, entry):
    """설정툴 보드 목록에 붙일 짧은 표기."""
    if not entry:
        return "대기"
    at = (entry.get("at") or "")[11:16]         # HH:MM
    tr = TRANSPORT_LABEL.get(entry.get("transport"), "?")
    if entry.get("transport") == 0:
        return "완료 %s · %s :%s" % (at, tr, entry.get("srv_port"))
    return "완료 %s · %s" % (at, tr)


# ===================== CLI =====================

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--forget":
        if len(argv) < 2:
            print("사용법: provision_log.py --forget <MAC>")
            return 2
        print("지웠습니다." if forget(argv[1]) else "그 MAC 의 기록이 없습니다.")
        return 0

    boards = load()
    print("이력 파일: %s" % LOG_PATH)
    if not boards:
        print("\n기록이 없습니다. 설정툴로 설정을 주입하면 자동으로 남습니다.")
        return 0
    if argv:
        mac = argv[0].lower()
        if mac not in boards:
            print("\n그 MAC 의 기록이 없습니다: %s" % mac)
            return 1
        print("")
        print(describe(mac, boards[mac]))
        return 0
    print("\n기록된 보드 %d대\n" % len(boards))
    for mac in sorted(boards):
        print(describe(mac, boards[mac]))
        print("")
    return 0


if __name__ == "__main__":
    sys.exit(main())
