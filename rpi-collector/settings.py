"""수집기 공통 설정 — 화면(GUI)에서 바꾼 값을 다음 실행에도 쓴다.

    ~/.config/iis3dwb-collector/settings.json

GUI 와 글자 화면(collect_cli.py)이 **같은 파일**을 읽는다. 화면 없는 Lite 이미지에서
글자 화면으로 돌 때도 GUI 에서 정해 둔 수집 시간이 그대로 적용돼야 하기 때문이다.
(명령줄 옵션을 주면 그 실행에서만 옵션이 이긴다.)
"""

import json
from pathlib import Path

import slots

SETTINGS_PATH = slots.CONFIG_DIR / "settings.json"

DEFAULTS = {
    "minutes": 5.0,             # 수집 길이 (두 센서 공통)
    "fmt": "csv",               # csv | bin
    "out": str(Path.home() / "vibdata"),
    "active_low": True,         # 포토센서 활성 레벨 (NPN = LOW)
    "auto_start": True,         # 포토센서로 자동 시작
}


def load():
    """저장된 설정. 없거나 깨졌거나 값이 이상하면 그 항목만 기본값."""
    out = dict(DEFAULTS)
    try:
        d = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    if not isinstance(d, dict):
        return out
    try:
        m = float(d.get("minutes", out["minutes"]))
        if 0 < m <= 24 * 60:
            out["minutes"] = m
    except (TypeError, ValueError):
        pass
    if d.get("fmt") in ("csv", "bin"):
        out["fmt"] = d["fmt"]
    if isinstance(d.get("out"), str) and d["out"].strip():
        out["out"] = d["out"]
    for k in ("active_low", "auto_start"):
        if isinstance(d.get(k), bool):
            out[k] = d[k]
    return out


def save(values):
    """알려진 키만 저장한다. 실패해도 수집에는 지장이 없으므로 예외를 올리지 않는다."""
    doc = load()
    doc.update({k: v for k, v in values.items() if k in DEFAULTS})
    try:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                                 encoding="utf-8")
        return True
    except OSError:
        return False
