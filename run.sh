#!/usr/bin/env bash
#
# run.sh — IIS3DWB 펌웨어 빌드 → 플래시 → 모니터 자동 실행 (라즈베리파이 / Linux)
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
#   설정 툴(NVS 주입 방식)은 이 스크립트와 별개로 실행한다.
#     · 라즈베리파이:  sensor-setup-py/set_sensor_gui.py  (권장 — 파이썬 무설치)
#     · macOS:         iis_config_tool (Tauri 앱)
#
# 포트 자동 감지(중요):
#   보드 종류나 USB 브리지 칩에 상관없이 연결된 ESP32 포트를 자동으로 찾는다.
#     /dev/ttyACM*   ESP32-S3 내장 USB-JTAG/Serial (대부분의 경우)
#     /dev/ttyUSB*   CP210x/FTDI/CH340 등 USB-UART 브리지 보드
#   라즈베리파이 내장 UART(/dev/ttyAMA*, /dev/serial*)는 IIS3DWB 와 무관하므로
#   자동으로 제외한다.
#
# 사용법:
#   ./run.sh                    # 연결된 포트 자동 감지
#   ./run.sh /dev/ttyACM0       # 포트를 직접 지정 (자동 감지 건너뜀)
#   IDF_EXPORT=~/esp/other/esp-idf/export.sh ./run.sh    # ESP-IDF 경로 지정
#
# 종료: 모니터 화면에서 Ctrl + ]
#
set -euo pipefail

# ---- 설정 ----
# 프로젝트 경로: 스크립트가 있는 위치로 자동 결정 (경로 하드코딩 없음 — 어디로
# 복사해도, 어느 사용자 계정에서도 그대로 동작한다)
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 포트: 첫 인자로 직접 지정 가능 (비우면 아래에서 자동 감지)
PORT="${1:-}"

echo "======================================================"
echo " IIS3DWB 빌드 · 플래시 · 모니터"
echo "   프로젝트 : $PROJECT_DIR"
echo "======================================================"

# ---- 0) 포트 자동 감지 ----
# 연결된 시리얼 포트 중 USB 계열만 골라낸다.
detect_ports() {
  local p
  for p in /dev/ttyACM* /dev/ttyUSB*; do
    # 글롭이 매칭 안 되면 패턴 문자열 그대로 남으므로 실제 존재 여부 확인
    [ -e "$p" ] || continue
    echo "$p"
  done
}

if [ -z "$PORT" ]; then
  echo "▶ 포트 자동 감지 중..."
  FOUND_PORTS=()
  while IFS= read -r line; do
    [ -n "$line" ] && FOUND_PORTS+=("$line")
  done < <(detect_ports)

  if [ "${#FOUND_PORTS[@]}" -eq 0 ]; then
    echo "❌ 연결된 ESP32 포트를 찾지 못했습니다. 현재 tty 목록:" >&2
    ls /dev/ttyACM* /dev/ttyUSB* 2>/dev/null || echo "   (USB 시리얼 포트 없음)" >&2
    echo "   · USB 케이블이 '데이터 전송용'인지 확인하세요 (충전 전용 케이블 불가)" >&2
    echo "   · 보드의 USB 포트를 바꿔 꽂아보세요 (USB / UART 두 개인 보드 있음)" >&2
    echo "   · 'dmesg | tail' 로 USB 인식 여부를 확인할 수 있습니다" >&2
    echo "   · 포트를 직접 지정하려면: ./run.sh /dev/ttyACM0" >&2
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

# ---- 0.5) 포트 접근 권한 확인 ----
# 라즈베리파이/리눅스에서 가장 흔한 실패 원인. 그룹 추가는 재로그인이 필요해
# 스크립트가 대신 해줄 수 없으므로 안내만 한다.
if [ -e "$PORT" ] && [ ! -w "$PORT" ]; then
  echo "❌ $PORT 에 쓰기 권한이 없습니다." >&2
  echo "   sudo usermod -aG dialout $USER   실행 후 재로그인(또는 재부팅)하세요." >&2
  exit 1
fi

# ---- 1) ESP-IDF 환경 활성화 ----
# 환경변수로 지정했으면 그것을 쓰고, 아니면 흔한 설치 경로를 순서대로 찾는다.
find_idf_export() {
  local c
  for c in "${IDF_EXPORT:-}" \
           "${IDF_PATH:-}/export.sh" \
           "$HOME/esp/v5.4.3/esp-idf/export.sh" \
           "$HOME/esp/esp-idf/export.sh" \
           "/opt/esp-idf/export.sh"; do
    [ -n "$c" ] && [ -f "$c" ] && { echo "$c"; return 0; }
  done
  # 그 외 ~/esp/*/esp-idf 형태를 탐색 (버전 폴더명을 모를 때)
  for c in "$HOME"/esp/*/esp-idf/export.sh; do
    [ -f "$c" ] && { echo "$c"; return 0; }
  done
  return 1
}

if ! IDF_EXPORT_SH="$(find_idf_export)"; then
  echo "❌ ESP-IDF export.sh 를 찾을 수 없습니다." >&2
  echo "   설치가 안 되어 있다면:  ./setup-rpi.sh" >&2
  echo "   경로를 직접 지정하려면: IDF_EXPORT=~/esp/.../export.sh ./run.sh" >&2
  exit 1
fi
echo "▶ ESP-IDF 환경 활성화: $IDF_EXPORT_SH"
# export.sh 는 nounset 환경에서 문제될 수 있어 잠시 해제
set +u
# shellcheck disable=SC1090
source "$IDF_EXPORT_SH" >/dev/null
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

# ---- 2.6) 다른 머신에서 만든 빌드 캐시 정리 ----
# mac/Windows 에서 빌드한 build/ 를 그대로 가져오면 CMakeCache 에 그 머신의
# 절대경로가 박혀 있어 빌드가 깨진다. 현재 경로와 다르면 지운다.
if [ -f build/CMakeCache.txt ] && \
   ! grep -q "CMAKE_HOME_DIRECTORY:INTERNAL=$PROJECT_DIR$" build/CMakeCache.txt; then
  echo "⚠ 다른 경로/머신에서 만든 build 캐시가 감지돼 정리합니다."
  rm -rf build
fi

# ---- 3) 포트 존재 최종 확인 ----
if [ ! -e "$PORT" ]; then
  echo "⚠ 포트 $PORT 가 보이지 않습니다. 현재 포트 목록:" >&2
  ls /dev/ttyACM* /dev/ttyUSB* 2>/dev/null || true
  echo "   케이블 연결을 확인하거나 './run.sh <포트>' 로 지정하세요." >&2
  exit 1
fi
echo "   사용 포트 : $PORT"

# ---- 4) 빌드 → 플래시 → 모니터 ----
echo "▶ 빌드 + 플래시 + 모니터 시작 (종료: Ctrl + ])"
idf.py -p "$PORT" flash monitor
