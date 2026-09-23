#!/usr/bin/env bash
#
# setup-rpi.sh — 새 라즈베리파이에 이 프로젝트의 개발/실행 환경을 한 번에 구성
#
# 하는 일:
#   1) apt 의존 패키지 설치 (ESP-IDF 빌드 + 파이썬 수신기/설정툴 실행용)
#   2) ESP-IDF v5.4.3 클론 + 툴체인 설치 (esp32s3)
#   3) 시리얼 포트 접근 권한(dialout) 확인·추가
#   4) 편의 alias(get_idf) 등록
#
# 펌웨어를 빌드하지 않고 **수신기만** 돌릴 라즈베리파이라면 ESP-IDF 가 필요 없다:
#   ./setup-rpi.sh --receiver-only
#
# 사용법:
#   ./setup-rpi.sh                  # 전체 (빌드 환경 포함)
#   ./setup-rpi.sh --receiver-only  # 수신기 실행에 필요한 것만 (가볍고 빠름)
#
# 소요 시간(라즈베리파이 4 기준): 전체 약 30~60분 / --receiver-only 약 1분
#
set -euo pipefail

IDF_VERSION="v5.4.3"
IDF_DIR="$HOME/esp/$IDF_VERSION/esp-idf"
IDF_TARGET="esp32s3"
RECEIVER_ONLY=0

for arg in "$@"; do
  case "$arg" in
    --receiver-only) RECEIVER_ONLY=1 ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "알 수 없는 옵션: $arg" >&2; exit 1 ;;
  esac
done

echo "======================================================"
echo " IIS3DWB 라즈베리파이 환경 설정"
echo "   모드 : $([ $RECEIVER_ONLY -eq 1 ] && echo '수신기만' || echo '전체(빌드 포함)')"
echo "======================================================"

# ---- 1) apt 패키지 ----
echo "▶ [1/4] apt 패키지 설치..."
sudo apt-get update

# 수신기·설정툴 공통:
#   python3-tk      설정 GUI(tkinter)
#   python3-serial  포트 자동 감지 + 시리얼 수신기
#   esptool         NVS 주입 (Debian 패키지 — pip 없이 설치되어 PATH 에 잡힌다.
#                   ESP-IDF 를 설치하면 자체 venv 의 esptool 도 함께 쓸 수 있다)
COMMON_PKGS=(python3 python3-pip python3-venv python3-tk python3-serial esptool)

# ESP-IDF 빌드용
BUILD_PKGS=(git wget flex bison gperf cmake ninja-build ccache
            libffi-dev libssl-dev dfu-util libusb-1.0-0)

if [ $RECEIVER_ONLY -eq 1 ]; then
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "${COMMON_PKGS[@]}"
else
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "${COMMON_PKGS[@]}" "${BUILD_PKGS[@]}"
fi

# ---- 2) ESP-IDF ----
if [ $RECEIVER_ONLY -eq 1 ]; then
  echo "▶ [2/4] ESP-IDF 건너뜀 (--receiver-only)"
else
  echo "▶ [2/4] ESP-IDF $IDF_VERSION 설치..."
  if [ -d "$IDF_DIR/.git" ]; then
    echo "  이미 있습니다: $IDF_DIR (건너뜀)"
  else
    mkdir -p "$(dirname "$IDF_DIR")"
    # --depth 1 로 해당 태그만 받아 용량·시간을 줄인다 (전체 히스토리 불필요)
    git clone -b "$IDF_VERSION" --depth 1 --recursive --shallow-submodules \
      https://github.com/espressif/esp-idf.git "$IDF_DIR"
  fi
  echo "  툴체인 설치 (esp32s3)..."
  (cd "$IDF_DIR" && ./install.sh "$IDF_TARGET")
fi

# ---- 3) 시리얼 포트 권한 ----
echo "▶ [3/4] 시리얼 포트 권한 확인..."
if id -nG "$USER" | tr ' ' '\n' | grep -qx dialout; then
  echo "  ✓ 이미 dialout 그룹에 속해 있습니다."
else
  sudo usermod -aG dialout "$USER"
  echo "  ✓ dialout 그룹에 추가했습니다."
  echo "  ⚠ 적용을 위해 재로그인(또는 재부팅)이 필요합니다."
fi

# ---- 3.5) ModemManager 간섭 차단 (udev) ----
# ModemManager 가 새로 나타난 ttyACM 에 AT 명령을 쏘면 USB-UART 브리지의 bulk OUT
# 엔드포인트가 스톨되어 esptool 쓰기가 영구 블로킹된다. 미리 제외해 둔다.
echo "▶ [3.5/4] ModemManager 제외 규칙 설치..."
sudo tee /etc/udev/rules.d/99-iis3dwb-no-modemmanager.rules > /dev/null <<'RULES'
# IIS3DWB 센서 보드의 USB 시리얼 포트를 ModemManager 가 건드리지 않게 한다.
# 복구 방법: tools/usb-recover.sh
SUBSYSTEM=="usb", ATTRS{idVendor}=="04b4", ATTRS{idProduct}=="0003", ENV{ID_MM_DEVICE_IGNORE}="1"
SUBSYSTEM=="tty", ATTRS{idVendor}=="04b4", ATTRS{idProduct}=="0003", ENV{ID_MM_DEVICE_IGNORE}="1"
SUBSYSTEM=="usb", ATTRS{idVendor}=="303a", ENV{ID_MM_DEVICE_IGNORE}="1"
SUBSYSTEM=="tty", ATTRS{idVendor}=="303a", ENV{ID_MM_DEVICE_IGNORE}="1"
SUBSYSTEM=="tty", ATTRS{idVendor}=="10c4", ENV{ID_MM_DEVICE_IGNORE}="1"
SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", ENV{ID_MM_DEVICE_IGNORE}="1"
SUBSYSTEM=="tty", ATTRS{idVendor}=="0403", ENV{ID_MM_DEVICE_IGNORE}="1"
RULES
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=tty --subsystem-match=usb
echo "  ✓ 설치 완료 (ID_MM_DEVICE_IGNORE=1)"

# ---- 4) alias ----
echo "▶ [4/4] 편의 alias 등록..."
if [ $RECEIVER_ONLY -eq 1 ]; then
  echo "  건너뜀 (--receiver-only)"
elif grep -q "alias get_idf=" "$HOME/.bashrc" 2>/dev/null; then
  echo "  ✓ get_idf alias 가 이미 있습니다."
else
  echo "alias get_idf='. $IDF_DIR/export.sh'" >> "$HOME/.bashrc"
  echo "  ✓ get_idf alias 를 ~/.bashrc 에 추가했습니다."
fi

echo ""
echo "======================================================"
echo " 완료"
echo "======================================================"
if [ $RECEIVER_ONLY -eq 1 ]; then
  cat <<'EOF'
수신기 실행:
  cd rpi-receiver
  ./run.sh            # WiFi(UDP) 수신
  ./run.sh serial     # USB 직결(시리얼) 수신

센서 설정(WiFi/서버IP/속도) 주입:
  cd sensor-setup-py && python3 set_sensor_gui.py
EOF
else
  cat <<EOF
새 터미널에서 ESP-IDF 활성화:
  get_idf          # 또는  . $IDF_DIR/export.sh

펌웨어 빌드·플래시·모니터:
  ./run.sh         # 포트 자동 감지 후 build+flash+monitor

수신기 실행:
  cd rpi-receiver && ./run.sh
EOF
fi
