#!/usr/bin/env bash
#
# make-deploy-package.sh — 배포형 설정툴 패키지(tar.gz)를 만든다.
#
# 사용법:
#   ./tools/make-deploy-package.sh --version 1.0.0
#   ./tools/make-deploy-package.sh --version 1.0.0 --build-dir build-deploy
#   ./tools/make-deploy-package.sh --version 1.0.0-rc1 --allow-dirty   # 시험용
#
# 하는 일:
#   1) git 상태·빌드 신선도·배포 빌드 여부를 단정 (여기서 막지 않으면 잘못된 펌웨어가 나간다)
#   2) 오프셋/플래시 인자를 flasher_args.json 에서 읽어옴 (하드코딩 금지)
#   3) 파티션 테이블을 검증 (NVS 소실 위험, 앱 파티션 여유)
#   4) allowlist 로만 staging 에 복사 (old/ 같은 사장된 파일이 새지 않게)
#   5) manifest.json(sha256 포함) 작성
#   6) dist/iis3dwb-setup-<VER>.tar.gz + .sha256 생성
#
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$PROJECT_DIR/sensor-setup-py"
BUILD_DIR="$PROJECT_DIR/build-deploy"
DIST="$PROJECT_DIR/dist"
VERSION=""
ALLOW_DIRTY=0

PRODUCT="IIS3DWB 진동센서"
MODEL="IIS3DWB-VIB-SENSOR"

while [ $# -gt 0 ]; do
  case "$1" in
    --version)     VERSION="${2:-}"; shift 2 ;;
    --build-dir)   BUILD_DIR="$PROJECT_DIR/${2:-}"; shift 2 ;;
    --allow-dirty) ALLOW_DIRTY=1; shift ;;
    -h|--help)     sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "알 수 없는 옵션: $1" >&2; exit 1 ;;
  esac
done

[ -n "$VERSION" ] || { echo "❌ --version 이 필요합니다 (예: --version 1.0.0)" >&2; exit 1; }

PKGNAME="iis3dwb-setup-$VERSION"
STAGE="$DIST/$PKGNAME"

cd "$PROJECT_DIR"
echo "======================================================"
echo " 배포 패키지 생성: $PKGNAME"
echo "   빌드 경로 : $BUILD_DIR"
echo "======================================================"

# ---- 1) git 상태 ----
GITDESC="$(git describe --tags --always --dirty 2>/dev/null || echo 'nogit')"
echo "▶ [1/9] git: $GITDESC"
case "$GITDESC" in
  *-dirty)
    if [ $ALLOW_DIRTY -eq 0 ]; then
      echo "❌ 작업 트리에 커밋되지 않은 변경이 있습니다 ($GITDESC)." >&2
      echo "   배포 펌웨어의 버전 문자열이 '-dirty' 가 되어 추적이 불가능해집니다." >&2
      echo "   커밋 후 다시 빌드하세요. 시험용이라면 --allow-dirty." >&2
      exit 1
    fi
    echo "   ⚠ --allow-dirty — 시험용 패키지입니다. 고객에게 배포하지 마세요."
    ;;
esac

# ---- 2) 빌드 산출물 존재 ----
echo "▶ [2/9] 빌드 산출물 확인..."
[ -f "$BUILD_DIR/flasher_args.json" ] || {
  echo "❌ $BUILD_DIR/flasher_args.json 이 없습니다." >&2
  echo "   먼저 배포 빌드를 하세요:  ./tools/build-deploy.sh" >&2
  exit 1; }

APP_REL="$(python3 -c "
import json,sys
fa=json.load(open('$BUILD_DIR/flasher_args.json'))
by={int(k,16):v for k,v in fa['flash_files'].items()}
print(by[max(by)])")"
APP_BIN="$BUILD_DIR/$APP_REL"
[ -f "$APP_BIN" ] || { echo "❌ 앱 바이너리가 없습니다: $APP_BIN" >&2; exit 1; }
echo "   앱: $APP_REL"

# ---- 3) 신선도 게이트 ----
# 소스가 바이너리보다 새로우면, 빌드하지 않은 변경이 배포될 뻔한 상황이다.
echo "▶ [3/9] 빌드 신선도 확인..."
STALE="$(find main components CMakeLists.txt sdkconfig.defaults sdkconfig.defaults.deploy \
           -type f -newer "$APP_BIN" 2>/dev/null | head -5 || true)"
if [ -n "$STALE" ]; then
  echo "❌ 펌웨어보다 새로운 소스가 있습니다 — 빌드가 오래되었습니다:" >&2
  echo "$STALE" | sed 's/^/     /' >&2
  echo "   ./tools/build-deploy.sh 로 다시 빌드하세요." >&2
  exit 1
fi
echo "   ✓ 최신"

# ---- 4) 배포 빌드 단정 ----
echo "▶ [4/9] 배포용 빌드인지 단정..."
python3 - "$BUILD_DIR/config/sdkconfig.json" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1]))
fail = []
if cfg.get("WIFI_PREFER_NVS") is not True:
    fail.append("CONFIG_WIFI_PREFER_NVS != y — GUI 로 넣은 WiFi 가 무시됩니다")
if cfg.get("IDF_TARGET") != "esp32s3":
    fail.append("IDF_TARGET=%r" % cfg.get("IDF_TARGET"))
for k in ("SECURE_BOOT", "SECURE_FLASH_ENC_ENABLED"):
    if cfg.get(k):
        fail.append("%s 가 켜져 있어 고객 측 플래시가 불가합니다" % k)
if cfg.get("WIFI_SSID") not in ("unconfigured", "", None):
    fail.append("Kconfig WIFI_SSID=%r — 실제 자격증명이 이미지에 들어갑니다"
                % cfg.get("WIFI_SSID"))
if fail:
    for f in fail:
        print("   ·", f, file=sys.stderr)
    sys.exit(1)
print("   ✓ WIFI_PREFER_NVS=y, esp32s3, secure off, 자격증명 미포함")
PY

# ---- 5) 파티션 테이블 검증 ----
echo "▶ [5/9] 파티션 테이블 검증..."
PYTHONPATH="$SRC" python3 - "$BUILD_DIR" <<'PY'
import json, os, sys
import fw_manifest as F
bd = sys.argv[1]
fa = F.read_flasher_args(bd)
by = {int(k, 16): v for k, v in fa["flash_files"].items()}
app_src  = os.path.join(bd, by[max(by)])
part_src = os.path.join(bd, [v for v in by.values() if "partition" in v][0])

pt_size = os.path.getsize(part_src)
if pt_size > F.PART_TABLE_MAX_SIZE:
    print("   ❌ partition-table.bin %d B > %d B" % (pt_size, F.PART_TABLE_MAX_SIZE),
          file=sys.stderr)
    print("      0x8000 쓰기가 0x9000(NVS) 까지 지워 설정이 소실됩니다.", file=sys.stderr)
    sys.exit(1)

parts = F.parse_partition_table(part_src)
nvs = F.find_partition(parts, "nvs")
if not nvs or nvs["offset"] != 0x9000 or nvs["size"] != 0x6000:
    print("   ❌ NVS 파티션이 0x9000/0x6000 이 아닙니다: %r" % nvs, file=sys.stderr)
    print("      설정툴의 NVS_OFFSET/NVS_SIZE 와 어긋납니다.", file=sys.stderr)
    sys.exit(1)

app = F.find_partition(parts, "factory") or F.find_partition(parts, "app0")
used = os.path.getsize(app_src)
if not app or used > app["size"]:
    print("   ❌ 앱이 파티션보다 큽니다 (%d > %s)" % (used, app and app["size"]),
          file=sys.stderr)
    sys.exit(1)
pct = 100.0 * used / app["size"]
print("   ✓ partition-table %d B, NVS 0x9000/0x6000, 앱 %.1f%%" % (pt_size, pct))
if pct > 90:
    print("   ⚠ 앱 파티션 사용률 %.1f%% — 여유가 부족합니다" % pct)
PY

# ---- 6) staging (allowlist) ----
echo "▶ [6/9] staging 구성..."
rm -rf "$STAGE"
mkdir -p "$STAGE/firmware" "$STAGE/udev" "$STAGE/tools"

# 파이썬 모듈 — 여기 적힌 것만 들어간다 (old/ 가 절대 섞이지 않도록 allowlist)
PY_FILES="set_sensor_gui.py nvs_gen.py fw_manifest.py esp_flash.py boot_log.py usb_reset.py"
for f in $PY_FILES; do
  if [ -f "$SRC/$f" ]; then
    cp "$SRC/$f" "$STAGE/"
  else
    echo "   ⚠ 아직 없음(건너뜀): $f"
  fi
done

# 사용자 문서
for f in README.md; do
  [ -f "$SRC/$f" ] && cp "$SRC/$f" "$STAGE/"
done
[ -f "$SRC/사용설명서.html" ] && cp "$SRC/사용설명서.html" "$STAGE/"

# 런처/설치 스크립트 (패키지 루트에 두는 원본)
for f in install.sh run_gui.sh; do
  [ -f "$PROJECT_DIR/deploy/$f" ] && cp "$PROJECT_DIR/deploy/$f" "$STAGE/"
done
[ -f "$PROJECT_DIR/deploy/99-iis3dwb.rules" ] && \
  cp "$PROJECT_DIR/deploy/99-iis3dwb.rules" "$STAGE/udev/"

# sudo 탈출구
[ -f "$PROJECT_DIR/tools/usb-recover.sh" ] && \
  cp "$PROJECT_DIR/tools/usb-recover.sh" "$STAGE/tools/"

echo "$VERSION" > "$STAGE/VERSION"

# 펌웨어 3종 (평평한 이름으로)
PYTHONPATH="$SRC" python3 - "$BUILD_DIR" "$STAGE" <<'PY'
import os, shutil, sys
import fw_manifest as F
bd, stage = sys.argv[1], sys.argv[2]
fa = F.read_flasher_args(bd)
by = {int(k, 16): v for k, v in fa["flash_files"].items()}
app_off, boot_off = max(by), min(by)
part_off = [o for o in by if o not in (app_off, boot_off)][0]
dest = {app_off: "app.bin", part_off: "partition-table.bin", boot_off: "bootloader.bin"}
for off, rel in by.items():
    shutil.copy2(os.path.join(bd, rel), os.path.join(stage, "firmware", dest[off]))
    print("   %-10s %-28s → %s" % ("0x%x" % off, os.path.basename(rel), dest[off]))
PY

# ---- 7) staging 자체 점검 ----
echo "▶ [7/9] staging 점검..."
( cd "$STAGE" && for f in *.py; do python3 -m py_compile "$f" || exit 1; done ) \
  && echo "   ✓ py_compile 통과"
# nvs_gen 자가점검 — Rust 툴과의 byte-exact 보장을 배포 직전에 재확인
( cd "$STAGE" && python3 nvs_gen.py >/dev/null ) && echo "   ✓ nvs_gen 자가점검 통과"
rm -rf "$STAGE/__pycache__"

# ---- 8) manifest ----
echo "▶ [8/9] manifest.json 작성..."
PYTHONPATH="$SRC" python3 - "$BUILD_DIR" "$STAGE" "$VERSION" "$PRODUCT" "$MODEL" "$GITDESC" <<'PY'
import getpass, json, os, socket, sys, datetime
import fw_manifest as F
bd, stage, ver, product, model, gitdesc = sys.argv[1:7]
prefer = json.load(open(os.path.join(bd, "config", "sdkconfig.json")))["WIFI_PREFER_NVS"]

m = F.build_manifest(
    bd, ver, product=product, model=model,
    wifi_prefer_nvs=prefer,
    nvs_offset=0x9000, nvs_size=0x6000, nvs_namespace="devcfg",
    built_by="%s@%s" % (getpass.getuser(), socket.gethostname()),
    built_at=datetime.datetime.now().isoformat(timespec="seconds"),
    git_describe=gitdesc,
)
for img in m["images"]:
    img.pop("_src", None)          # 빌드 경로는 배포물에 남기지 않는다
out = os.path.join(stage, "firmware", "manifest.json")
with open(out, "w") as f:
    json.dump(m, f, indent=2, ensure_ascii=False)
    f.write("\n")
print("   ✓", F.fw_summary(m))
print("   ✓ 쓰기 순서:", " → ".join(i["name"] for i in m["images"]))
PY

# 방금 만든 manifest 로 즉시 역검증
PYTHONPATH="$SRC" python3 "$SRC/fw_manifest.py" verify "$STAGE" >/dev/null \
  && echo "   ✓ sha256 역검증 통과"

# ---- 9) tar.gz ----
echo "▶ [9/9] tar.gz 생성..."
TARBALL="$DIST/$PKGNAME.tar.gz"
tar -czf "$TARBALL" -C "$DIST" "$PKGNAME"
( cd "$DIST" && sha256sum "$PKGNAME.tar.gz" > "$PKGNAME.tar.gz.sha256" )

echo ""
echo "======================================================"
echo " 완료"
echo "======================================================"
echo "  패키지 : $TARBALL"
echo "  크기   : $(du -h "$TARBALL" | cut -f1)"
echo "  sha256 : $(cut -d' ' -f1 "$DIST/$PKGNAME.tar.gz.sha256")"
echo ""
echo "사용자 안내:"
echo "  tar -xzf $PKGNAME.tar.gz"
echo "  cd $PKGNAME && ./install.sh     # 최초 1회 (재로그인 필요할 수 있음)"
echo "  ./run_gui.sh"
