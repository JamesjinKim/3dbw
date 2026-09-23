#!/usr/bin/env bash
#
# build.sh — 보드 검사 펌웨어를 빌드하고 dist/ 에 배포용 이미지를 모은다.
#
# 하드웨어 담당자에게는 boardcheck/ 폴더째 주면 된다. 담당자 쪽에는
# ESP-IDF 가 필요 없고 esptool 만 있으면 된다 (dist/ 의 .bin 을 굽는다).
#
# 사용법:
#   ./build.sh          # 빌드 + dist/ 갱신
#   ./build.sh clean    # build/ 를 지우고 처음부터
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="$HERE/build"
DIST_DIR="$HERE/dist"

cd "$HERE"

if [ "${1:-}" = "clean" ]; then
  echo "▶ $BUILD_DIR 정리..."
  rm -rf "$BUILD_DIR"
fi

# ---- ESP-IDF 환경 활성화 (tools/build-deploy.sh 와 동일한 탐색 순서) ----
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
  echo "❌ ESP-IDF export.sh 를 찾을 수 없습니다. 상위 폴더의 ./setup-rpi.sh 를 먼저 실행하세요." >&2
  exit 1
fi
set +u
# shellcheck disable=SC1090
source "$IDF_EXPORT_SH" >/dev/null
set -u

echo "======================================================"
echo " IIS3DWB 보드 검사 펌웨어 빌드"
echo "   프로젝트 : $HERE"
echo "   빌드 경로 : $BUILD_DIR"
echo "======================================================"

idf.py -B "$BUILD_DIR" build

# ---- 검사 펌웨어가 맞는지 단정 ----
# 운영 펌웨어를 잘못 담아 배포하면 담당자가 "검사가 안 끝난다" 로 막힌다.
SDKJSON="$BUILD_DIR/config/sdkconfig.json"
python3 - "$SDKJSON" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1]))
fail = []
if cfg.get("IDF_TARGET") != "esp32s3":
    fail.append("IDF_TARGET=%r (esp32s3 여야 합니다)" % cfg.get("IDF_TARGET"))
for k in ("SECURE_BOOT", "SECURE_FLASH_ENC_ENABLED"):
    if cfg.get(k):
        fail.append("%s 가 켜져 있습니다 (담당자 측 esptool 쓰기가 불가)" % k)
if fail:
    print("❌ 검사 빌드 검증 실패:", file=sys.stderr)
    for f in fail:
        print("   ·", f, file=sys.stderr)
    sys.exit(1)
print("")
print("▶ 빌드된 핀맵 (이 값으로 검사합니다)")
for k in ("IIS3DWB_SPI_HOST", "IIS3DWB_SPI_MOSI_GPIO", "IIS3DWB_SPI_MISO_GPIO",
          "IIS3DWB_SPI_SCLK_GPIO", "IIS3DWB_SPI_CS_GPIO",
          "IIS3DWB_INT1_GPIO", "IIS3DWB_INT2_GPIO", "IIS3DWB_SPI_FREQ_HZ"):
    print("   %-24s %s" % (k, cfg.get(k)))
PY

# ---- dist/ 로 이미지 + flasher_args 복사 ----
echo ""
echo "▶ dist/ 갱신..."
rm -rf "$DIST_DIR"
mkdir -p "$DIST_DIR"

python3 - "$BUILD_DIR" "$DIST_DIR" <<'PY'
import hashlib, json, os, shutil, sys, datetime
bd, dd = sys.argv[1], sys.argv[2]
fa = json.load(open(os.path.join(bd, "flasher_args.json")))
cfg = json.load(open(os.path.join(bd, "config", "sdkconfig.json")))

manifest = {
    "kind": "iis3dwb-boardcheck",
    "built_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    "chip": fa["extra_esptool_args"]["chip"],
    "write_flash_args": fa["write_flash_args"],
    "pinmap": {k: cfg.get(k) for k in (
        "IIS3DWB_SPI_HOST", "IIS3DWB_SPI_MOSI_GPIO", "IIS3DWB_SPI_MISO_GPIO",
        "IIS3DWB_SPI_SCLK_GPIO", "IIS3DWB_SPI_CS_GPIO",
        "IIS3DWB_INT1_GPIO", "IIS3DWB_INT2_GPIO", "IIS3DWB_SPI_FREQ_HZ")},
    "images": [],
}

# 쓰기 순서는 app → partition-table → bootloader.
# 부트로더를 마지막에 두면 중간에 끊겨도 부팅 불가 구간이 가장 짧다.
order = ["app", "partition-table", "bootloader"]
for key in order:
    ent = fa[key]
    src = os.path.join(bd, ent["file"])
    name = os.path.basename(ent["file"])
    shutil.copy2(src, os.path.join(dd, name))
    with open(src, "rb") as f:
        sha = hashlib.sha256(f.read()).hexdigest()
    manifest["images"].append({
        "offset": ent["offset"], "file": name,
        "size": os.path.getsize(src), "sha256": sha})

with open(os.path.join(dd, "manifest.json"), "w") as f:
    json.dump(manifest, f, indent=2)
    f.write("\n")

for im in manifest["images"]:
    print("   %-10s %-28s %8d B" % (im["offset"], im["file"], im["size"]))
PY

echo ""
echo "완료. 검사 실행:   ./boardcheck.py"
echo "담당자 배포:       boardcheck/ 폴더를 통째로 전달 (README.md 참고)"
