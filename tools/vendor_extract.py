#!/usr/bin/env python3
"""vendor_extract.py — 저장소의 vendor/ 에서 배포 패키지용 파이썬 라이브러리를 꺼낸다.

폐쇄망 현장에서는 apt / pip 를 쓸 수 없으므로, 배포 패키지에 순수 파이썬
라이브러리를 소스째 넣는다. 원본은 vendor/ 에 커밋돼 있어 패키지를 만들 때도
인터넷이 필요 없다.

  vendor/wheels/  pyserial, intelhex (PyPI 공식 휠)
  vendor/sdist/   esptool (PyPI 는 sdist 만 배포 — 소스에서 패키지 폴더만 꺼낸다)
  vendor/SHA256SUMS  위 파일들의 해시. 하나라도 어긋나면 중단한다.

사용법:
  python3 tools/vendor_extract.py <대상폴더> serial [intelhex esptool ...]
"""

import hashlib
import shutil
import sys
import tarfile
import zipfile
from pathlib import Path

VENDOR = Path(__file__).resolve().parent.parent / "vendor"

# 이름 → (원본 파일, 꺼낼 최상위 패키지 폴더)
SOURCES = {
    "serial":   ("wheels/pyserial-3.5-py2.py3-none-any.whl", "serial"),
    "intelhex": ("wheels/intelhex-2.3.0-py2.py3-none-any.whl", "intelhex"),
    "esptool":  ("sdist/esptool-4.8.1.tar.gz", "esptool"),
}


def die(msg):
    print("❌ " + msg, file=sys.stderr)
    sys.exit(1)


def verify(rel):
    want = {}
    for line in (VENDOR / "SHA256SUMS").read_text().splitlines():
        if line.strip():
            h, name = line.split(None, 1)
            want[name.strip()] = h
    if rel not in want:
        die("SHA256SUMS 에 없음: " + rel)
    got = hashlib.sha256((VENDOR / rel).read_bytes()).hexdigest()
    if got != want[rel]:
        die("해시 불일치: %s\n   기대 %s\n   실제 %s" % (rel, want[rel], got))


def extract(rel, pkg, dest):
    src = VENDOR / rel
    out = dest / pkg
    if out.exists():
        shutil.rmtree(out)
    n = 0
    if rel.endswith(".whl"):
        with zipfile.ZipFile(src) as z:
            for m in z.namelist():
                if m.startswith(pkg + "/"):
                    z.extract(m, dest)
                    n += 1
    else:
        with tarfile.open(src) as t:
            root = t.getnames()[0].split("/")[0]
            prefix = "%s/%s/" % (root, pkg)
            for m in t.getmembers():
                if m.isfile() and m.name.startswith(prefix):
                    target = dest / m.name[len(root) + 1:]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with t.extractfile(m) as f:
                        target.write_bytes(f.read())
                    n += 1
    if n == 0:
        die("%s 안에 %s/ 가 없습니다" % (rel, pkg))
    for c in out.rglob("__pycache__"):
        shutil.rmtree(c)
    return n


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 2
    dest = Path(argv[1])
    dest.mkdir(parents=True, exist_ok=True)
    for name in argv[2:]:
        if name not in SOURCES:
            die("모르는 라이브러리: %s (가능: %s)" % (name, ", ".join(SOURCES)))
        rel, pkg = SOURCES[name]
        verify(rel)
        n = extract(rel, pkg, dest)
        print("   ✓ %-9s %-45s %4d 파일" % (name, rel, n))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
