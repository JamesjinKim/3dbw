#!/usr/bin/env python3
"""도움말(help.html) 열기.

GUI 의 '도움말' 버튼과 CLI(`--help-doc`)가 같이 쓴다.

## 왜 별도 모듈인가

라즈베리파이 데스크톱은 labwc(Wayland) 이고, 브라우저를 띄우는 방법이 환경마다
다르다. `webbrowser` 모듈만 쓰면 헤드리스나 최소 설치 환경에서 조용히 실패해
**버튼을 눌러도 아무 일도 일어나지 않는 것처럼 보인다.** 그래서 여러 경로를
순서대로 시도하고, 전부 실패하면 **파일 경로를 돌려줘서** 호출부가 사용자에게
"이 파일을 직접 여세요" 라고 알릴 수 있게 한다.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HELP_FILE = HERE / "help.html"


class HelpError(Exception):
    """도움말을 열지 못한 이유. 메시지를 그대로 사용자에게 보인다."""


def help_path():
    """도움말 파일 경로. 없으면 HelpError."""
    if not HELP_FILE.is_file():
        raise HelpError("도움말 파일을 찾을 수 없습니다:\n%s" % HELP_FILE)
    return HELP_FILE


def open_help():
    """기본 브라우저로 도움말을 연다. 성공하면 연 방법을, 실패하면 HelpError."""
    path = help_path()
    url = path.as_uri()

    # 1) 데스크톱 표준 — 라즈베리파이 OS 에 기본 설치돼 있다
    for opener in ("xdg-open", "gio"):
        exe = shutil.which(opener)
        if not exe:
            continue
        args = [exe, "open", url] if opener == "gio" else [exe, url]
        try:
            # 브라우저가 뜨는 데 시간이 걸리므로 기다리지 않는다.
            # stdout/stderr 를 버려야 GUI 콘솔이 지저분해지지 않는다.
            subprocess.Popen(args, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            return opener
        except Exception:
            continue

    # 2) 브라우저 실행파일 직접
    for br in ("chromium-browser", "chromium", "firefox", "epiphany-browser"):
        exe = shutil.which(br)
        if not exe:
            continue
        try:
            subprocess.Popen([exe, url], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            return br
        except Exception:
            continue

    # 3) 파이썬 표준 모듈 (위가 다 없을 때)
    try:
        import webbrowser
        if webbrowser.open(url):
            return "webbrowser"
    except Exception:
        pass

    raise HelpError(
        "브라우저를 열지 못했습니다.\n아래 파일을 직접 열어 주세요:\n\n%s" % path)


def main(argv=None):
    try:
        how = open_help()
    except HelpError as e:
        print("❌ %s" % e, file=sys.stderr)
        return 1
    print("도움말을 열었습니다 (%s): %s" % (how, HELP_FILE))
    return 0


if __name__ == "__main__":
    sys.exit(main())
