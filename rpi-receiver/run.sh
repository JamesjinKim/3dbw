#!/usr/bin/env bash
#
# IIS3DWB 수신기 실행 스크립트 (라즈베리파이)
#
# 전송 방식은 디바이스 NVS 의 transport 값에 따라 갈린다:
#   transport = 0  → WiFi UDP   → 이 스크립트 (udp_receiver.py)
#   transport = 2  → USB 직결   → ./run.sh serial  (serial_receiver.py)
#
# 사용법:
#   ./run.sh                    # UDP 수신, 포트 9000, vibration.csv 저장
#   ./run.sh --fs 4             # 풀스케일 지정 (구형 v1 펌웨어에만 필요)
#   PORT=9001 ./run.sh          # 수신 포트 변경
#   ./run.sh serial             # USB 시리얼 수신 (포트 자동 감지)
#   ./run.sh serial --port /dev/ttyACM0
#
# CSV 는 raw(LSB) 와 mg 를 항상 함께 기록한다. v2 펌웨어는 패킷 헤더에 풀스케일이
# 들어 있어 --fs 없이도 mg 환산이 정확하다.
#
set -euo pipefail

cd "$(dirname "$0")"

PORT="${PORT:-9000}"

# python3 확인
if ! command -v python3 >/dev/null 2>&1; then
    echo "❌ python3 가 필요합니다.  sudo apt install -y python3"
    exit 1
fi

# ---- USB 시리얼 모드 ----
if [ "${1:-}" = "serial" ]; then
    shift
    if ! python3 -c "import serial" >/dev/null 2>&1; then
        echo "❌ pyserial 이 필요합니다.  sudo apt install -y python3-serial"
        exit 1
    fi
    exec python3 serial_receiver.py "$@"
fi

# ---- WiFi UDP 모드 ----
# 방화벽이 있다면 UDP 포트 안내 (자동 변경은 안 함)
echo "ℹ️  방화벽 사용 시 UDP ${PORT} 포트를 열어주세요:"
echo "    sudo ufw allow ${PORT}/udp   # ufw 사용하는 경우"
echo ""

exec python3 udp_receiver.py --port "${PORT}" "$@"
