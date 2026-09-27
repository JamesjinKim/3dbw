#!/usr/bin/env bash
#
# install.sh — IIS3DWB 배포 패키지 최초 1회 설치 (인터넷 불필요)
#
# 폐쇄망 현장을 전제로 한다. apt / pip 를 전혀 쓰지 않는다.
# 필요한 파이썬 라이브러리(pyserial·esptool)는 패키지의 vendor/ 에 들어 있다.
#
# 하는 일:
#   1) 실행 환경 점검 — 파이썬 버전, tkinter(GUI), 패키지 무결성
#   2) udev 규칙 설치 — ModemManager 간섭 차단 + USB 리셋 권한
#                        + 데스크톱 사용자 즉시 권한(uaccess)            (sudo)
#   3) 사용자 그룹 — dialout(시리얼) · plugdev(USB 리셋) · gpio(포토센서)  (sudo)
#                    SSH 접속용. 데스크톱에서는 [2] 의 uaccess 로 재로그인 없이 된다.
#
# 보통은 직접 부르지 않는다 — 패키지의 run.sh 가 매번 --check 로 점검하고,
# 빠진 것이 있을 때만 이 스크립트로 설치한 뒤 프로그램을 띄운다.
#
# 사용법:
#   bash install.sh            # 설치
#   bash install.sh --check    # 점검만 (아무것도 바꾸지 않는다)
#
# 종료코드 (run.sh 가 이것으로 갈린다):
#   0  정상 — 바로 실행 가능 (⚠ 는 일부 기능 제한일 뿐)
#   1  설치로 고칠 수 없는 문제 (python 없음, tkinter 없음, 패키지 손상)
#   3  --check 전용: 설치하면 고쳐지는 항목이 있다 (udev 규칙·그룹)
#
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

TARGET_USER="${SUDO_USER:-$USER}"
FAIL=0
WARN=0
NEED=0
ok()   { echo "   ✓ $*"; }
warn() { echo "   ⚠ $*"; WARN=1; }
bad()  { echo "   ❌ $*"; FAIL=1; }
need() { echo "   ⚠ $* — 설치가 필요합니다"; NEED=1; }

echo "▶ [1/3] 실행 환경 점검"

if ! command -v python3 >/dev/null 2>&1; then
  bad "python3 가 없습니다 — Raspberry Pi OS 에는 기본으로 들어 있어야 합니다"
  exit 1
fi
PYV="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 7) else 1)'; then
  ok "python $PYV"
else
  bad "python $PYV — 3.7 이상이 필요합니다"
fi

# tkinter — 설정툴은 GUI 뿐이라 없으면 실패, 수집기는 글자 화면으로 대신할 수 있어 경고
if [ -f "$HERE/set_sensor_gui.py" ] || [ -f "$HERE/collector_gui.py" ]; then
  if python3 -c 'import tkinter' >/dev/null 2>&1; then
    ok "tkinter (GUI) 있음"
  elif [ -f "$HERE/set_sensor_gui.py" ]; then
    bad "tkinter 가 없어 설정툴 GUI 가 뜨지 않습니다 (Raspberry Pi OS Lite 이미지로 보임)"
  else
    warn "tkinter 가 없어 수집기 화면(GUI)을 쓸 수 없습니다 — 글자 화면으로 동작합니다"
  fi
fi

# 패키지에 넣어 온 라이브러리가 실제로 import 되는지 — 시스템 것은 막고 확인한다
check_vendor() {  # $1 = 모듈 이름
  if python3 -S -c "import sys; sys.path.insert(0, '$HERE/vendor'); import $1" >/dev/null 2>&1; then
    ok "$1 (패키지 내장)"
  else
    bad "$1 을 패키지에서 불러오지 못했습니다 — tar.gz 를 다시 풀어 보세요"
  fi
}
# 폴더 유무로 건너뛰면 안 된다 — 빠진 것이 곧 손상이다 (폴더째 빠지면 점검을 통과했었다).
check_vendor serial                                        # 두 패키지 모두
[ -f "$HERE/set_sensor_gui.py" ] && check_vendor esptool   # 설정툴만

if [ -f "$HERE/gpio_cdev.py" ]; then
  # 포토센서 입력(DIN1=GPIO5)을 실제로 열어 본다. 수집기와 같은 경로(cdev → lgpio).
  # 그룹을 방금 추가했다면 재로그인 전이라 권한 오류가 날 수 있다.
  GPIO_MSG="$(cd "$HERE" && python3 -c "
import trigger
h = trigger.open_input(5, pull='up'); print(h.name); h.close()" 2>&1 | tail -1)"
  case "$GPIO_MSG" in
    cdev|lgpio) ok "포토센서 GPIO 사용 가능 (백엔드 $GPIO_MSG)" ;;
    *) warn "포토센서 GPIO 를 열 수 없습니다 — 자동 수집 불가, 수동 수집(--manual)은 가능"
       echo "      ($GPIO_MSG)" ;;
  esac
fi

echo "▶ [2/3] udev 규칙"
RULE_SRC="$HERE/udev/70-iis3dwb.rules"
RULE_DST="/etc/udev/rules.d/70-iis3dwb.rules"
# 0.0.6 까지의 이름. 73-seat-late 뒤라 uaccess 가 먹지 않는다 — 새 규칙과 겹치지 않게 지운다.
RULE_OLD="/etc/udev/rules.d/99-iis3dwb.rules"
if [ ! -f "$RULE_SRC" ]; then
  bad "패키지에 $RULE_SRC 가 없습니다"
elif cmp -s "$RULE_SRC" "$RULE_DST" 2>/dev/null && [ ! -e "$RULE_OLD" ]; then
  ok "이미 설치됨 ($RULE_DST)"
elif [ $CHECK_ONLY = 1 ]; then
  if [ -e "$RULE_OLD" ]; then need "예전 규칙($RULE_OLD) 교체"
  elif [ -e "$RULE_DST" ]; then need "udev 규칙이 이 패키지와 다름 (갱신)"
  else need "udev 규칙 미설치"
  fi
else
  # trigger 는 이 규칙이 다루는 장치만 — 전체를 다시 돌리면 입력장치까지 흔들린다.
  # settle 로 기다려야 run.sh 가 곧바로 띄운 프로그램이 ACL 이 붙은 포트를 연다.
  if sudo install -m 0644 "$RULE_SRC" "$RULE_DST" && \
     sudo rm -f "$RULE_OLD" && \
     sudo udevadm control --reload-rules && \
     sudo udevadm trigger --subsystem-match=tty --subsystem-match=usb \
                          --subsystem-match=gpio && \
     sudo udevadm settle --timeout=10; then
    ok "설치: $RULE_DST"
  else
    bad "udev 규칙 설치 실패"
  fi
fi

echo "▶ [3/3] 사용자 그룹 ($TARGET_USER)"
NEED_RELOGIN=0
for g in dialout plugdev gpio; do
  if ! getent group "$g" >/dev/null; then
    [ "$g" = gpio ] && continue          # gpio 그룹은 Raspberry Pi OS 에만 있다
    warn "그룹 $g 가 이 OS 에 없습니다"
    continue
  fi
  if id -nG "$TARGET_USER" | tr ' ' '\n' | grep -qx "$g"; then
    ok "$g"
  elif [ $CHECK_ONLY = 1 ]; then
    need "$g 그룹에 없음"
  elif sudo usermod -aG "$g" "$TARGET_USER"; then
    ok "$g 추가"
    NEED_RELOGIN=1
  else
    bad "$g 그룹 추가 실패"
  fi
done

echo ""
if [ $FAIL = 1 ]; then
  echo "❌ 문제가 있습니다 — 위의 ❌ 항목을 확인하세요"
  exit 1
fi
if [ $NEED_RELOGIN = 1 ]; then
  echo "ℹ 그룹이 추가됐습니다. 이 화면(데스크톱)에서는 바로 쓸 수 있습니다."
  echo "  SSH 로 접속해 쓰는 경우에만 로그아웃 후 다시 로그인해야 적용됩니다."
fi
if [ $CHECK_ONLY = 1 ] && [ $NEED = 1 ]; then
  echo "⚠ 설치가 필요한 항목이 있습니다 — bash run.sh (또는 bash install.sh)"
  exit 3
fi
if [ $WARN = 1 ]; then
  echo "✓ 설치 완료 (⚠ 항목은 일부 기능 제한)"
else
  echo "✓ 설치 완료"
fi
exit 0
