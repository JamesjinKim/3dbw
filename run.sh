#!/usr/bin/env bash
#
# run.sh — IIS3DWB 펌웨어 빌드 → 플래시 → 모니터 자동 실행
#
# 하는 일:
#   1) ESP-IDF 환경 활성화 (export.sh)
#   2) idf.py build      (펌웨어 빌드)
#   3) idf.py flash      (디바이스에 굽기)
#   4) idf.py monitor    (시리얼 로그 관찰; 종료 Ctrl+])
#
# WiFi 연결 방식(중요):
#   이 펌웨어는 menuconfig의 CONFIG_WIFI_SSID/PASSWORD 값으로 곧바로 연결한다.
#   (NVS/config-tool 과 무관하게 독립 동작 — flash 시 NVS가 초기화돼도 문제 없음)
#   WiFi 값을 바꾸려면: idf.py menuconfig → WiFi Configuration → SSID/Password.
#   config-tool(NVS 주입 방식)은 이 스크립트와 별개의 GUI 앱으로 실행한다.
#
# 포트 자동 감지(중요):
#   USB 케이블 종류(A-to-B, C-to-C)나 보드 종류에 상관없이 연결된 ESP32 포트를
#   자동으로 찾는다. 지원 패턴:
#     /dev/cu.usbmodem*      (ESP32-S3 내장 USB-JTAG/Serial — C-to-C 등)
#     /dev/cu.usbserial*     (CP210x/FTDI 등 USB-UART 브리지 — A-to-B 등)
#     /dev/cu.wchusbserial*  (CH340/CH9102 브리지)
#     /dev/cu.SLAB_USBtoUART (구형 Silicon Labs 드라이버)
#   Bluetooth 계열 가상 포트(cu.BLTH 등)는 자동으로 제외한다.
#
# 사용법:
#   ./run.sh                 # 연결된 포트 자동 감지
#   ./run.sh /dev/cu.xxxxx   # 포트를 직접 지정 (자동 감지 건너뜀)
#
# 종료: 모니터 화면에서 Ctrl + ]
#
set -euo pipefail

# ---- 설정 ----
IDF_EXPORT="$HOME/esp/v5.4.3/esp-idf/export.sh"
PROJECT_DIR="/Users/kimkookjin/Projects/ESP-IDF/IIS3DWB"

# 포트: 첫 인자로 직접 지정 가능 (비우면 아래에서 자동 감지)
PORT="${1:-}"

echo "======================================================"
echo " IIS3DWB 빌드 · 플래시 · 모니터"
echo "   프로젝트 : $PROJECT_DIR"
echo "======================================================"

# ---- 0) 포트 자동 감지 ----
# 연결된 시리얼 포트 중 ESP32 계열만 골라낸다.
# (케이블 타입 A-to-B / C-to-C, 보드의 UART 브리지 칩 종류 무관)
detect_ports() {
  local p
  for p in /dev/cu.usbmodem* /dev/cu.usbserial* /dev/cu.wchusbserial* \
           /dev/cu.SLAB_USBtoUART* /dev/cu.usbmodemserial*; do
    # 글롭이 매칭 안 되면 패턴 문자열 그대로 남으므로 실제 존재 여부 확인
    [ -e "$p" ] || continue
    # 블루투스 등 가상 포트 제외
    case "$p" in
      *BLTH*|*Bluetooth*) continue ;;
    esac
    echo "$p"
  done
}

if [ -z "$PORT" ]; then
  echo "▶ 포트 자동 감지 중..."
  # macOS 기본 bash 3.2 에는 mapfile 이 없으므로 while-read 로 배열을 채운다
  FOUND_PORTS=()
  while IFS= read -r line; do
    [ -n "$line" ] && FOUND_PORTS+=("$line")
  done < <(detect_ports)

  if [ "${#FOUND_PORTS[@]}" -eq 0 ]; then
    echo "❌ 연결된 ESP32 포트를 찾지 못했습니다. 현재 포트 목록:" >&2
    ls /dev/cu.* 2>/dev/null || true
    echo "   · USB 케이블이 '데이터 전송용'인지 확인하세요 (충전 전용 케이블 불가)" >&2
    echo "   · 보드의 USB 포트를 바꿔 꽂아보세요 (USB / UART 두 개인 보드 있음)" >&2
    echo "   · 포트를 직접 지정하려면: ./run.sh /dev/cu.XXXX" >&2
    exit 1
  elif [ "${#FOUND_PORTS[@]}" -eq 1 ]; then
    PORT="${FOUND_PORTS[0]}"
    echo "  ✓ 포트 자동 감지: $PORT"
  else
    echo "  여러 개의 포트가 감지되었습니다:"
    i=1
    for p in "${FOUND_PORTS[@]}"; do
      echo "    $i) $p"
      i=$((i + 1))
    done
    # 대화형 터미널이면 선택, 아니면 첫 번째 사용
    if [ -t 0 ]; then
      read -r -p "  사용할 포트 번호 [1]: " sel
      sel="${sel:-1}"
    else
      sel=1
    fi
    if ! [ "$sel" -ge 1 ] 2>/dev/null || [ "$sel" -gt "${#FOUND_PORTS[@]}" ]; then
      echo "❌ 잘못된 선택입니다: $sel" >&2
      exit 1
    fi
    PORT="${FOUND_PORTS[$((sel - 1))]}"
    echo "  ✓ 선택한 포트: $PORT"
  fi
else
  echo "▶ 지정된 포트 사용: $PORT"
fi

# ---- 1) ESP-IDF 환경 활성화 ----
if [ ! -f "$IDF_EXPORT" ]; then
  echo "❌ ESP-IDF export.sh 를 찾을 수 없습니다: $IDF_EXPORT" >&2
  exit 1
fi
echo "▶ ESP-IDF 환경 활성화..."
# export.sh 는 nounset 환경에서 문제될 수 있어 잠시 해제
set +u
# shellcheck disable=SC1090
source "$IDF_EXPORT" >/dev/null
set -u

# ---- 2) 프로젝트 폴더로 이동 ----
cd "$PROJECT_DIR"

# ---- 2.5) build generator 충돌 방어 ----
# VSCode CMake 확장이 build 를 'Unix Makefiles'로 만들어두면 idf.py(Ninja)와
# 충돌해 "generator does not match" 에러가 난다. Ninja가 아니면 build를 지운다.
if [ -f build/CMakeCache.txt ] && ! grep -q "CMAKE_GENERATOR:INTERNAL=Ninja" build/CMakeCache.txt; then
  echo "⚠ build 가 Ninja가 아닌 generator로 생성돼 있어 정리합니다 (VSCode CMake 확장 충돌 방지)."
  rm -rf build
fi

# ---- 3) 포트 존재 최종 확인 ----
if [ ! -e "$PORT" ]; then
  echo "⚠ 포트 $PORT 가 보이지 않습니다. 현재 포트 목록:" >&2
  ls /dev/cu.* 2>/dev/null || true
  echo "   케이블 연결을 확인하거나 './run.sh <포트>' 로 지정하세요." >&2
  exit 1
fi
echo "   사용 포트 : $PORT"

# ---- 4) 빌드 → 플래시 → 모니터 ----
echo "▶ 빌드 + 플래시 + 모니터 시작 (종료: Ctrl + ])"
idf.py -p "$PORT" flash monitor
