#!/usr/bin/env bash
#
# run.sh — 보드 검사 실행 (하드웨어 담당자용 진입점)
#
# 이 폴더에서:
#   ./run.sh              보드 한 개 검사
#   ./run.sh --loop       보드를 갈아 끼우며 연속 검사
#   ./run.sh --help       모든 옵션
#
# ESP-IDF 는 필요 없다. 펌웨어는 dist/ 에 이미 구워질 형태로 들어 있다.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "❌ '$1' 이 없습니다.  설치:  $2" >&2
    exit 3
  }
}

need python3 "sudo apt install python3"
python3 -c "import serial" 2>/dev/null || {
  echo "❌ pyserial 이 없습니다.  설치:  sudo apt install python3-serial" >&2
  exit 3
}
command -v esptool >/dev/null 2>&1 || command -v esptool.py >/dev/null 2>&1 || \
  python3 -c "import esptool" 2>/dev/null || {
    echo "❌ esptool 이 없습니다.  설치:  sudo apt install esptool" >&2
    exit 3
  }

# dialout 그룹에 없으면 포트를 열지 못한다. 흔한 첫 실패라 미리 짚어 준다.
if ! id -nG | tr ' ' '\n' | grep -qx dialout; then
  echo "⚠️  현재 사용자가 dialout 그룹에 없습니다. 포트 열기가 거부될 수 있습니다."
  echo "    해결:  sudo usermod -aG dialout $USER   (실행 후 로그아웃·재로그인)"
  echo ""
fi

exec python3 "$HERE/boardcheck.py" "$@"
