#!/usr/bin/env bash
#
# build-deploy.sh — 배포(고객 출하)용 펌웨어를 build-deploy/ 에 빌드한다.
#
# 개발 빌드(build/, sdkconfig)는 건드리지 않는다. 개발 빌드는
# CONFIG_WIFI_PREFER_NVS=n 이라 NVS 의 WiFi 를 무시하므로 배포에 쓸 수 없다.
#
# 사용법:
#   ./tools/build-deploy.sh            # 빌드
#   ./tools/build-deploy.sh clean      # build-deploy/ 를 지우고 처음부터
#
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="$PROJECT_DIR/build-deploy"
OVERLAY="sdkconfig.defaults;sdkconfig.defaults.deploy"

cd "$PROJECT_DIR"

if [ "${1:-}" = "clean" ]; then
  echo "▶ $BUILD_DIR 정리..."
  rm -rf "$BUILD_DIR"
fi

# ---- ESP-IDF 환경 활성화 (run.sh 와 동일한 탐색 순서) ----
find_idf_export() {
  local c
  for c in "${IDF_EXPORT:-}" \
           "${IDF_PATH:-}/export.sh" \
           "$HOME/esp/v5.4.3/esp-idf/export.sh" \
           "$HOME/esp/esp-idf/export.sh" \
           "/opt/esp-idf/export.sh"; do
    [ -n "$c" ] && [ -f "$c" ] && { echo "$c"; return 0; }
  done
  for c in "$HOME"/esp/*/esp-idf/export.sh; do
    [ -f "$c" ] && { echo "$c"; return 0; }
  done
  return 1
}

if ! IDF_EXPORT_SH="$(find_idf_export)"; then
  echo "❌ ESP-IDF export.sh 를 찾을 수 없습니다. ./setup-rpi.sh 를 먼저 실행하세요." >&2
  exit 1
fi
set +u
# shellcheck disable=SC1090
source "$IDF_EXPORT_SH" >/dev/null
set -u

echo "======================================================"
echo " 배포용 펌웨어 빌드"
echo "   프로젝트 : $PROJECT_DIR"
echo "   빌드 경로 : $BUILD_DIR"
echo "   오버레이  : $OVERLAY"
echo "======================================================"

idf.py -B "$BUILD_DIR" \
  -D SDKCONFIG_DEFAULTS="$OVERLAY" \
  -D SDKCONFIG="$BUILD_DIR/sdkconfig" \
  build

# ---- 배포 빌드가 맞는지 즉시 단정 ----
# 여기서 막지 않으면 =n 펌웨어가 그대로 패키징되어 GUI 의 WiFi 입력이 무시된다.
echo ""
echo "▶ 배포 빌드 검증..."

SDKJSON="$BUILD_DIR/config/sdkconfig.json"
if [ ! -f "$SDKJSON" ]; then
  echo "❌ $SDKJSON 이 없습니다 — 빌드가 정상 완료되지 않았습니다." >&2
  exit 1
fi

python3 - "$SDKJSON" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1]))
fail = []
if cfg.get("WIFI_PREFER_NVS") is not True:
    fail.append("CONFIG_WIFI_PREFER_NVS 가 y 가 아닙니다 (NVS WiFi 가 무시됩니다)")
if cfg.get("IDF_TARGET") != "esp32s3":
    fail.append("IDF_TARGET=%r (esp32s3 여야 합니다)" % cfg.get("IDF_TARGET"))
# 보안 부트/플래시 암호화가 켜져 있으면 고객 측 esptool 쓰기가 실패한다
for k in ("SECURE_BOOT", "SECURE_FLASH_ENC_ENABLED"):
    if cfg.get(k):
        fail.append("%s 가 켜져 있습니다 (배포툴의 플래시가 불가)" % k)
if fail:
    print("❌ 배포 빌드 검증 실패:", file=sys.stderr)
    for f in fail:
        print("   ·", f, file=sys.stderr)
    sys.exit(1)
print("  ✓ CONFIG_WIFI_PREFER_NVS=y")
print("  ✓ IDF_TARGET=esp32s3")
print("  ✓ secure boot / flash encryption off")
PY

echo ""
echo "▶ 산출물:"
python3 - "$BUILD_DIR" <<'PY'
import json, os, sys
bd = sys.argv[1]
fa = json.load(open(os.path.join(bd, "flasher_args.json")))
for off, rel in sorted(fa["flash_files"].items(), key=lambda kv: int(kv[0], 16)):
    p = os.path.join(bd, rel)
    print("   %-10s %-40s %8d B" % (off, rel, os.path.getsize(p)))
PY

echo ""
echo "다음 단계:  ./tools/make-deploy-package.sh --version X.Y.Z"
