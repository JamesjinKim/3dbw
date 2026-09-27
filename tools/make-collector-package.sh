#!/usr/bin/env bash
#
# make-collector-package.sh — 현장 RPi 용 수집기 패키지(tar.gz)를 만든다.
#
# 사용법:
#   ./tools/make-collector-package.sh --version 1.0.0
#   ./tools/make-collector-package.sh --version 1.0.0-rc1 --allow-dirty   # 시험용
#
# 폐쇄망 현장을 전제로 한다. pyserial 을 vendor/ 에 소스째 넣고, 설치는
# run.sh 가 처음 실행될 때 install.sh 로 인터넷 없이 한다 (udev 규칙 + 사용자 그룹).
#
# 산출물: dist-collector/iis3dwb-collector-<VER>/ (+ .tar.gz, .sha256)
#
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$PROJECT_DIR/rpi-collector"
# 설정툴 배포 폴더(dist/)와 섞이지 않게 따로 둔다 — dist/ 는 그대로 zip 해 사용자에게 준다
DIST="$PROJECT_DIR/dist-collector"
VERSION=""
ALLOW_DIRTY=0

while [ $# -gt 0 ]; do
  case "$1" in
    --version)     VERSION="${2:-}"; shift 2 ;;
    --allow-dirty) ALLOW_DIRTY=1; shift ;;
    -h|--help)     sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "알 수 없는 옵션: $1" >&2; exit 1 ;;
  esac
done
[ -n "$VERSION" ] || { echo "❌ --version 이 필요합니다 (예: --version 1.0.0)" >&2; exit 1; }

PKGNAME="iis3dwb-collector-$VERSION"
STAGE="$DIST/$PKGNAME"

cd "$PROJECT_DIR"
GITDESC="$(git describe --tags --always --dirty 2>/dev/null || echo 'nogit')"
echo "▶ [1/5] git 상태: $GITDESC"
case "$GITDESC" in
  *-dirty)
    if [ $ALLOW_DIRTY = 0 ]; then
      echo "❌ 커밋되지 않은 변경이 있습니다. 커밋 후 다시 하거나, 시험용이면 --allow-dirty." >&2
      exit 1
    fi
    echo "   ⚠ --allow-dirty — 시험용 패키지입니다. 고객에게 배포하지 마세요." ;;
esac

# ---- staging (allowlist) ----
echo "▶ [2/5] staging 구성..."
mkdir -p "$DIST"
rm -rf "$STAGE"
mkdir -p "$STAGE/udev"

# 여기 적힌 것만 들어간다. selftest.py 는 현장 점검용으로 넣는다.
EXCLUDED=""
PY_FILES="collector_gui.py settings.py collect_cli.py session.py sensor_link.py writer.py
          trigger.py slots.py iis3dwb_packet.py bin_to_csv.py udp_receiver.py
          helpdoc.py vendor_path.py gpio_cdev.py selftest.py"
for f in $PY_FILES; do
  cp "$SRC/$f" "$STAGE/" || { echo "❌ 모듈 없음: $f" >&2; exit 1; }
done
cp "$SRC/help.html" "$STAGE/"
# 진입점/설치 스크립트 (설정툴·수집기 공용 원본). 작업자는 run.sh 만 안다.
for f in run.sh install.sh; do
  cp "$PROJECT_DIR/deploy/$f" "$STAGE/" && chmod +x "$STAGE/$f" \
    || { echo "❌ deploy/$f 없음" >&2; exit 1; }
done
cp "$PROJECT_DIR/deploy/70-iis3dwb.rules" "$STAGE/udev/"
chmod +x "$STAGE/collect_cli.py"

# 이 폴더에 없는 .py 가 있으면 allowlist 누락을 의심한다
for f in "$SRC"/*.py; do
  b="$(basename "$f")"
  echo " $PY_FILES $EXCLUDED " | tr -s ' \n' ' ' | grep -q " $b " \
    || echo "   ⚠ allowlist 에 없는 모듈(패키지에서 빠짐): $b"
done

python3 "$PROJECT_DIR/tools/vendor_extract.py" "$STAGE/vendor" serial \
  || { echo "❌ vendor 추출 실패" >&2; exit 1; }

echo "$VERSION ($GITDESC)" > "$STAGE/VERSION"

# ---- 점검 ----
echo "▶ [3/5] staging 점검..."
( cd "$STAGE" && for f in *.py; do python3 -m py_compile "$f" || exit 1; done ) \
  && echo "   ✓ py_compile 통과"
( cd "$STAGE" && bash -n run.sh && bash -n install.sh ) \
  || { echo "❌ run.sh / install.sh 문법 오류" >&2; exit 1; }
# 시스템 site-packages 를 끄고(-S) 패키지 안의 것만으로 import 되는지
( cd "$STAGE" && python3 -S -c "
import sys; sys.path.insert(0, '.')
import collect_cli, sensor_link, slots, serial, trigger, gpio_cdev, settings, collector_gui
assert '/vendor/' in serial.__file__, serial.__file__
assert trigger.lgpio is None   # -S 에서는 시스템 lgpio 가 없어야 정상 (cdev 로 동작)
" ) || { echo "❌ 패키지 내장 라이브러리만으로 import 되지 않습니다" >&2; exit 1; }
echo "   ✓ 내장 라이브러리 점검 통과 (시스템 패키지 없이)"
( cd "$STAGE" && python3 -S selftest.py >/dev/null ) \
  || { echo "❌ selftest 실패 — 패키지 상태로 돌려 확인하세요" >&2; exit 1; }
echo "   ✓ selftest 통과 (패키지 상태, 시스템 패키지 없이)"
find "$STAGE" -name __pycache__ -type d -prune -exec rm -rf {} +

# ---- 파일 목록 + 해시 ----
echo "▶ [4/5] SHA256SUMS 작성..."
( cd "$STAGE" && find . -type f ! -name SHA256SUMS | sort | xargs sha256sum > SHA256SUMS )
echo "   ✓ $(wc -l < "$STAGE/SHA256SUMS") 파일"

# ---- tar.gz ----
echo "▶ [5/5] tar.gz 생성..."
( cd "$DIST" && tar -czf "$PKGNAME.tar.gz" "$PKGNAME" && sha256sum "$PKGNAME.tar.gz" > "$PKGNAME.tar.gz.sha256" )

echo ""
echo "======================================================"
echo " 완료"
echo "======================================================"
echo "  패키지 : $DIST/$PKGNAME.tar.gz"
echo "  크기   : $(du -h "$DIST/$PKGNAME.tar.gz" | cut -f1)"
echo "  sha256 : $(cut -d' ' -f1 "$DIST/$PKGNAME.tar.gz.sha256")"
echo ""
echo "사용자 안내:"
echo "  tar -xzf $PKGNAME.tar.gz"
echo "  cd $PKGNAME && bash run.sh --status   # 처음엔 USB 권한 설정까지 (인터넷 불필요)"
echo "  bash run.sh                            # 자동 수집"
