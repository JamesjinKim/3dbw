#!/usr/bin/env bash
#
# run.sh — IIS3DWB 배포 패키지의 유일한 진입점 (설정툴·수집기 공용 원본)
#
# 현장 작업자는 이것 하나만 안다:   bash run.sh
#
# 매번 하는 일:
#   1) 점검   — install.sh --check (아무것도 바꾸지 않아 빠르다)
#   2) 설치   — 빠진 것(udev 규칙·그룹)이 있을 때만 install.sh 로. 관리자 비밀번호를
#               물을 수 있다. 이미 돼 있으면 묻지 않는다.
#   3) 실행   — 설정툴이면 설정 GUI, 수집기면 수집 GUI (화면 없으면 글자 화면)
#
# 어느 패키지인지는 들어 있는 파일로 가른다 (install.sh 와 같은 방식).
# `./run.sh` 가 아니라 `bash run.sh` 로 안내한다 — FAT 포맷 USB 메모리로 옮기면
# 실행권한이 사라져 ./run.sh 는 "허가 거부" 가 난다. bash 로 부르면 상관없다.
#
# 사용법 (설정툴):
#   bash run.sh                    # GUI 실행
#
# 사용법 (수집기):
#   bash run.sh                    # 수집기 화면(GUI). 화면이 없으면 글자 화면 자동 수집
#   bash run.sh --auto             # 글자 화면 자동 수집 (포토센서 대기)
#   bash run.sh --status           # 그 밖의 옵션은 collect_cli.py 로 그대로 전달
#   bash run.sh --manual --minutes 1
#   bash run.sh slots [옵션]        # USB 포트 ↔ 센서 번호 (slots.py)
#   bash run.sh udp [옵션]          # WiFi(UDP) 수신 (udp_receiver.py)
#   bash run.sh help               # 사용설명서(help.html) 열기
#
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$HERE/set_sensor_gui.py" ]; then
  KIND=setup
elif [ -f "$HERE/collect_cli.py" ]; then
  KIND=collector
else
  echo "❌ 패키지가 손상됐습니다 — tar.gz 를 다시 풀어 그 폴더에서 실행하세요" >&2
  exit 1
fi

# 바탕화면 아이콘으로 띄우면 터미널이 없다. 그때는 메시지를 창으로 보여 준다
# (설정툴은 tkinter 가 있어야 돌므로, 없으면 어차피 터미널 안내밖에 없다).
has_tty() { [ -t 0 ] && [ -t 1 ]; }
notify() {  # $1 = 메시지
  if has_tty; then
    echo "$1"
  else
    python3 -c "
import sys, tkinter, tkinter.messagebox as mb
r = tkinter.Tk(); r.withdraw(); mb.showwarning('IIS3DWB', sys.argv[1])" "$1" \
      2>/dev/null || echo "$1" >&2
  fi
}

# ---- 1) 점검 · 2) 필요할 때만 설치 ----
CHECK_OUT="$(bash "$HERE/install.sh" --check 2>&1)"
case $? in
  0)
    # 정상. 일부 기능 제한(⚠, 예: 포토센서 GPIO 없음)만 한 줄씩 보여 준다.
    echo "$CHECK_OUT" | grep '^   ⚠' || true
    ;;
  3)
    if ! has_tty; then
      notify "처음 한 번은 USB 권한 설정이 필요합니다.

터미널을 열고 이 폴더에서 다음을 실행하세요:
  bash run.sh

($HERE)"
      exit 1
    fi
    echo "$CHECK_OUT" | grep '^   ⚠'
    echo ""
    echo "▶ USB 권한을 설정합니다 — 관리자 비밀번호를 물을 수 있습니다 (처음 한 번만)"
    bash "$HERE/install.sh" || { echo "❌ 설치에 실패했습니다 — 위 메시지를 확인하세요" >&2; exit 1; }
    echo ""
    ;;
  *)
    echo "$CHECK_OUT"
    # 터미널이면 원인이 바로 위에 찍혔다. 아이콘 실행일 때만 창으로 알린다.
    has_tty || notify "실행 환경에 문제가 있어 시작할 수 없습니다.
터미널에서 bash run.sh 를 실행하면 원인이 표시됩니다."
    exit 1
    ;;
esac

# ---- 지금 세션에서 포트를 실제로 쓸 수 있는가 ----
# 데스크톱 세션은 udev 의 uaccess 로 바로 열린다. SSH 세션은 그룹에 기대는데,
# 방금 그룹을 추가했다면 재로그인 전까지 적용되지 않는다 — 그 경우만 알려 준다.
BLOCKED=""
for d in /dev/ttyACM* /dev/ttyUSB*; do
  [ -e "$d" ] || continue
  { [ -r "$d" ] && [ -w "$d" ]; } || BLOCKED="$BLOCKED $d"
done
if [ -n "$BLOCKED" ]; then
  echo "⚠ 이 세션에서 열 수 없는 포트:$BLOCKED"
  echo "  SSH 로 접속했다면 로그아웃 후 다시 접속하세요 (그룹 적용). 데스크톱 화면에서는"
  echo "  USB 케이블을 다시 꽂아 보세요."
fi

# ---- 3) 실행 ----
# 바탕화면 아이콘 — 두 번째부터는 터미널 없이 더블클릭. 폴더를 옮겼거나 새 버전을
# 풀어 실행하면 아이콘이 가리키는 경로를 이번 폴더로 고쳐 쓴다 (마지막 실행이 이긴다).
make_desktop_icon() {  # $1=파일이름 $2=표시이름 $3=설명
  [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] || return 0
  local desk file entry ver
  desk="$(xdg-user-dir DESKTOP 2>/dev/null || true)"
  [ -n "$desk" ] && [ "$desk" != "$HOME" ] && [ -d "$desk" ] || return 0
  file="$desk/$1"
  ver="$(head -1 "$HERE/VERSION" 2>/dev/null | cut -d' ' -f1)"
  entry="[Desktop Entry]
Type=Application
Name=$2
Comment=$3 (${ver:-?})
Exec=bash \"$HERE/run.sh\"
Path=$HERE
Icon=preferences-system
Terminal=false
Categories=Utility;"
  [ -f "$file" ] && [ "$(cat "$file")" = "$entry" ] && return 0
  if printf '%s\n' "$entry" > "$file" 2>/dev/null; then
    chmod +x "$file"
    echo "ℹ 바탕화면에 '$2' 아이콘을 만들었습니다 (${ver:-?})."
    echo "  다음부터는 아이콘을 더블클릭하세요. (\"실행\" 을 묻는 창이 뜨면 실행)"
  fi
}

# 화면이 있고 tkinter 가 있어야 GUI 를 띄울 수 있다 (Lite 이미지·SSH 에는 없다).
can_gui() {
  [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] && python3 -c 'import tkinter' 2>/dev/null
}

if [ $KIND = setup ]; then
  make_desktop_icon iis3dwb-setup.desktop "SHT 진동센서 설정" "펌웨어 굽기 + 설정 주입"
  exec python3 "$HERE/set_sensor_gui.py" "$@"
fi

# 수집기 — 옵션 없이 실행하면 GUI, 화면이 없으면 글자 화면(자동 수집)으로 대신한다
case "${1:-}" in
  ""|gui)
    if can_gui; then
      make_desktop_icon iis3dwb-collector.desktop "SHT 진동센서 수집" "포토센서 연동 진동 수집"
      exec python3 "$HERE/collector_gui.py"
    fi
    if [ "${1:-}" = gui ]; then
      echo "❌ 화면(데스크톱)이 없거나 tkinter 가 없어 GUI 를 띄울 수 없습니다." >&2
      exit 1
    fi
    echo "ℹ 화면이 없어 글자 화면으로 자동 수집을 시작합니다 (설정은 GUI 에서 저장한 값)."
    exec python3 "$HERE/collect_cli.py" --auto ;;
  slots)
    shift
    # 고정 장치 이름 규칙은 /etc/udev 에 쓰므로 root 가 필요하다
    for a in "$@"; do
      if [ "$a" = "--make-udev" ] && [ "$(id -u)" != 0 ]; then
        exec sudo python3 "$HERE/slots.py" "$@"
      fi
    done
    exec python3 "$HERE/slots.py" "$@" ;;
  udp)
    shift
    exec python3 "$HERE/udp_receiver.py" "$@" ;;
  help)
    exec python3 "$HERE/collect_cli.py" --help-doc ;;
  *)
    exec python3 "$HERE/collect_cli.py" "$@" ;;
esac
