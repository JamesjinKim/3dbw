#!/usr/bin/env bash
#
# usb-recover.sh — USB 시리얼 포트가 "쓰기만 막힌" 상태를 복구한다 (라즈베리파이/Linux)
#
# 증상:
#   · 디바이스 → PC 읽기는 정상 (부팅 로그가 보인다)
#   · PC → 디바이스 쓰기는 영구 블로킹
#   · esptool: "A serial exception error occurred: Write timeout"
#   · python: write() 가 반환하지 않고 TIOCOUTQ(out_waiting) 가 줄지 않음
#
# 원인:
#   USB-UART 브리지의 bulk OUT 엔드포인트가 스톨된 상태. 드라이버 unbind/rebind 로는
#   엔드포인트 상태가 초기화되지 않으므로 **USB 장치 레벨 리셋**이 필요하다.
#   (ModemManager 가 새 ttyACM 에 AT 명령을 쏘면서 유발하는 경우가 많다 —
#    재발 방지는 /etc/udev/rules.d/99-iis3dwb-no-modemmanager.rules 참고)
#
# 사용법:
#   ./tools/usb-recover.sh              # 연결된 ESP32 계열 브리지를 찾아 리셋
#   ./tools/usb-recover.sh 04b4:0003    # VID:PID 직접 지정
#
set -euo pipefail

# 이 프로젝트에서 쓰이는 USB 시리얼 브리지들
KNOWN_IDS=(
  "303a"        # Espressif 내장 USB Serial/JTAG
  "04b4:0003"   # Cypress USB-UART LP (센서 보드)
  "10c4:ea60"   # Silicon Labs CP210x
  "1a86"        # WCH CH340 / CH9102
  "0403"        # FTDI
)

TARGET="${1:-}"

find_device() {
  local id
  if [ -n "$TARGET" ]; then
    lsusb | grep -i "$TARGET" | head -1 && return 0 || return 1
  fi
  for id in "${KNOWN_IDS[@]}"; do
    if lsusb | grep -qi "$id"; then
      lsusb | grep -i "$id" | head -1
      return 0
    fi
  done
  return 1
}

if ! LINE="$(find_device)"; then
  echo "❌ ESP32 계열 USB 시리얼 장치를 찾지 못했습니다. 현재 USB 목록:" >&2
  lsusb >&2
  echo "   VID:PID 를 직접 지정하려면: $0 04b4:0003" >&2
  exit 1
fi

# "Bus 001 Device 006: ID 04b4:0003 ..." 에서 버스/장치 번호 추출
BUS="$(echo "$LINE"  | awk '{print $2}')"
DEVN="$(echo "$LINE" | awk '{gsub(":","",$4); print $4}')"
NODE="/dev/bus/usb/$BUS/$DEVN"

echo "대상: $LINE"
echo "노드: $NODE"

# 포트를 잡고 있는 프로세스가 있으면 먼저 알린다 (리셋해도 스톨이 재발할 수 있음)
for p in /dev/ttyACM* /dev/ttyUSB*; do
  [ -e "$p" ] || continue
  if HOLDER="$(sudo fuser "$p" 2>/dev/null)" && [ -n "$HOLDER" ]; then
    echo "⚠ $p 를 사용 중인 프로세스가 있습니다: $HOLDER"
    echo "  (idf.py monitor / 수신기 등을 먼저 종료하세요)"
  fi
done

echo "▶ USBDEVFS_RESET 실행..."
sudo python3 - "$NODE" <<'PY'
import fcntl, os, sys
USBDEVFS_RESET = ord('U') << 8 | 20
fd = os.open(sys.argv[1], os.O_WRONLY)
try:
    fcntl.ioctl(fd, USBDEVFS_RESET, 0)
    print("  ✓ 리셋 성공")
finally:
    os.close(fd)
PY

echo "▶ 포트 재생성 대기..."
for i in $(seq 1 15); do
  for p in /dev/ttyACM* /dev/ttyUSB*; do
    if [ -e "$p" ]; then
      echo "  ✓ 포트 복구: $p"
      echo ""
      echo "이제 다시 시도하세요:  ./run.sh    또는    idf.py -p $p flash"
      exit 0
    fi
  done
  sleep 0.5
done

echo "⚠ 포트가 다시 나타나지 않았습니다. USB 케이블을 뽑았다 꽂아보세요." >&2
exit 1
