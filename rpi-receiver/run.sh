#!/usr/bin/env bash
#
# IIS3DWB UDP 수신기 실행 스크립트 (라즈베리파이)
#
# 사용법:
#   ./run.sh                # 포트 9000, raw 값으로 vibration.csv 저장
#   ./run.sh --mg --fs 4    # mg 단위로 저장
#   PORT=9001 ./run.sh      # 포트 변경
#
set -euo pipefail

cd "$(dirname "$0")"

PORT="${PORT:-9000}"

# python3 확인
if ! command -v python3 >/dev/null 2>&1; then
    echo "❌ python3 가 필요합니다.  sudo apt install -y python3"
    exit 1
fi

# 방화벽이 있다면 UDP 포트 안내 (자동 변경은 안 함)
echo "ℹ️  방화벽 사용 시 UDP ${PORT} 포트를 열어주세요:"
echo "    sudo ufw allow ${PORT}/udp   # ufw 사용하는 경우"
echo ""

exec python3 udp_receiver.py --port "${PORT}" "$@"
