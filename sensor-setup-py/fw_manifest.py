#!/usr/bin/env python3
"""펌웨어 번들 manifest 읽기/쓰기 + 바이너리 파서

패키징 스크립트(tools/make-deploy-package.sh)와 배포된 GUI 가 **같은 코드**로
manifest 를 다루도록 여기에 모은다. 패키징 시점에 기록한 값을 실행 시점에
그대로 검증하므로, 손상된 복사본이 디바이스에 닿기 전에 걸러진다.

단독 실행 시 CLI:
    python3 fw_manifest.py describe <build-dir>     # 빌드 산출물 요약
    python3 fw_manifest.py verify <pkg-dir>         # 배포 패키지 sha256 검증
"""

import hashlib
import json
import os
import struct
import sys

SCHEMA = 1

# esp_app_desc_t — ESP-IDF components/esp_app_format/include/esp_app_desc.h
# 앱 이미지 헤더(24B) + 세그먼트 헤더(8B) 다음, 즉 오프셋 0x20 에 놓인다.
APP_DESC_OFFSET = 0x20
APP_DESC_MAGIC = 0xABCD5432

# 파티션 테이블 엔트리 (32B 고정) — components/partition_table
PART_ENTRY_SIZE = 32
PART_MAGIC = b"\xaaP"

# 파티션 테이블은 0x8000 에 쓰이고, 그 바로 뒤가 NVS(0x9000) 다.
# esptool 은 자신이 쓰는 4KB 섹터를 지우므로, 이 파일이 4096B 를 넘으면
# 0x8000 쓰기가 0x9000 까지 지워 **NVS 설정이 소실된다.**
PART_TABLE_MAX_SIZE = 4096


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ===================== app.bin 파서 =====================
def parse_app_desc(app_bin_path):
    """app.bin 에 박혀 있는 esp_app_desc_t 를 읽어 버전 정보를 돌려준다.

    펌웨어 버전을 손으로 적지 않기 위한 것이다. 사람이 입력하면 반드시 틀어진다.
    """
    with open(app_bin_path, "rb") as f:
        f.seek(APP_DESC_OFFSET)
        blob = f.read(256)
    if len(blob) < 256:
        raise ValueError("app.bin 이 너무 작습니다: %s" % app_bin_path)

    magic, secure_version = struct.unpack_from("<II", blob, 0)
    if magic != APP_DESC_MAGIC:
        raise ValueError(
            "app_desc 매직 불일치 (0x%08X, 기대 0x%08X) — app.bin 이 아닐 수 있습니다: %s"
            % (magic, APP_DESC_MAGIC, app_bin_path)
        )

    def cstr(off, size):
        return blob[off:off + size].split(b"\x00", 1)[0].decode("utf-8", "replace")

    return {
        "secure_version": secure_version,
        "app_version": cstr(0x10, 32),
        "project_name": cstr(0x30, 32),
        "build_time": cstr(0x50, 16),
        "build_date": cstr(0x60, 16),
        "idf_version": cstr(0x70, 32),
    }


# ===================== 파티션 테이블 파서 =====================
def parse_partition_table(bin_path):
    """partition-table.bin 을 파싱해 엔트리 목록을 돌려준다."""
    data = open(bin_path, "rb").read()
    out = []
    for i in range(0, len(data), PART_ENTRY_SIZE):
        e = data[i:i + PART_ENTRY_SIZE]
        if len(e) < PART_ENTRY_SIZE or e[:2] != PART_MAGIC:
            break
        offset = int.from_bytes(e[4:8], "little")
        size = int.from_bytes(e[8:12], "little")
        out.append({
            "label": e[12:28].rstrip(b"\x00").decode("utf-8", "replace"),
            "type": e[2],
            "subtype": e[3],
            "offset": offset,
            "size": size,
        })
    if not out:
        raise ValueError("파티션 엔트리를 찾지 못했습니다: %s" % bin_path)
    return out


def find_partition(parts, label):
    for p in parts:
        if p["label"] == label:
            return p
    return None


# ===================== 빌드 산출물 → manifest =====================
def read_flasher_args(build_dir):
    """빌드가 생성한 flasher_args.json 에서 오프셋·플래시 인자를 읽는다.

    오프셋을 코드에 하드코딩하면 파티션 구성이 바뀔 때 조용히 어긋난다.
    항상 빌드가 알려주는 값을 쓴다.
    """
    path = os.path.join(build_dir, "flasher_args.json")
    with open(path) as f:
        fa = json.load(f)
    return fa


def build_manifest(build_dir, package_version, *, product, model,
                   wifi_prefer_nvs, nvs_offset, nvs_size, nvs_namespace,
                   built_by=None, built_at=None, git_describe=None):
    """빌드 디렉터리에서 manifest dict 를 만든다 (파일 복사는 하지 않음).

    images 는 **쓰기 순서**로 담는다: app → partition-table → bootloader.
    부트로더를 마지막에 두면 플래시가 중간에 끊겨도 부팅 불가 구간이
    ~1초(21KB)로 줄어든다.
    """
    fa = read_flasher_args(build_dir)

    # flash_files: {"0x0": "bootloader/bootloader.bin", ...}
    by_offset = {int(k, 16): v for k, v in fa["flash_files"].items()}

    app_off = max(by_offset)          # 앱이 가장 높은 오프셋
    boot_off = min(by_offset)         # 부트로더가 0x0
    part_off = [o for o in by_offset if o not in (app_off, boot_off)]
    if len(part_off) != 1:
        raise ValueError("flash_files 구성이 예상과 다릅니다: %r" % fa["flash_files"])
    part_off = part_off[0]

    # 배포 패키지에서의 파일명 (빌드 경로 구조를 평평하게)
    dest_name = {app_off: "app.bin",
                 part_off: "partition-table.bin",
                 boot_off: "bootloader.bin"}

    images = []
    for off in (app_off, part_off, boot_off):      # ← 쓰기 순서
        src = os.path.join(build_dir, by_offset[off])
        images.append({
            "name": dest_name[off].replace(".bin", ""),
            "file": dest_name[off],
            "offset": "0x%x" % off,
            "size": os.path.getsize(src),
            "sha256": sha256_file(src),
            "_src": src,                            # 패키징 전용, manifest 기록 시 제거
        })

    app_src = os.path.join(build_dir, by_offset[app_off])
    desc = parse_app_desc(app_src)
    parts = parse_partition_table(os.path.join(build_dir, by_offset[part_off]))

    return {
        "schema": SCHEMA,
        "package_version": package_version,
        "product": product,
        "model": model,
        "chip": fa.get("extra_esptool_args", {}).get("chip", "esp32s3"),
        "app_name": desc["project_name"],
        "app_version": desc["app_version"],
        "idf_version": desc["idf_version"],
        "build_date": "%s %s" % (desc["build_date"], desc["build_time"]),
        "git_describe": git_describe,
        "built_by": built_by,
        "built_at": built_at,
        "wifi_prefer_nvs": bool(wifi_prefer_nvs),
        "write_flash_args": fa.get("write_flash_args", []),
        "esptool_extra": fa.get("extra_esptool_args", {}),
        "images": images,
        "nvs": {"offset": "0x%x" % nvs_offset,
                "size": "0x%x" % nvs_size,
                "namespace": nvs_namespace},
        "partitions": [{k: p[k] for k in ("label", "offset", "size")} for p in parts],
    }


# ===================== 실행 시점 검증 =====================
def load_manifest(pkg_dir):
    path = os.path.join(pkg_dir, "firmware", "manifest.json")
    with open(path) as f:
        m = json.load(f)
    if m.get("schema") != SCHEMA:
        raise ValueError("manifest schema %r 를 지원하지 않습니다 (기대 %d)"
                         % (m.get("schema"), SCHEMA))
    return m


def verify_images(pkg_dir, manifest):
    """번들된 .bin 들이 manifest 의 sha256 과 일치하는지 확인.

    일치하지 않는 파일은 (파일명, 이유) 로 돌려준다. 빈 리스트면 정상.
    복사/전송 중 손상된 이미지가 디바이스에 닿는 것을 막는 마지막 관문이다.
    """
    fwdir = os.path.join(pkg_dir, "firmware")
    bad = []
    for img in manifest["images"]:
        p = os.path.join(fwdir, img["file"])
        if not os.path.exists(p):
            bad.append((img["file"], "파일이 없습니다"))
            continue
        actual_size = os.path.getsize(p)
        if actual_size != img["size"]:
            bad.append((img["file"], "크기 불일치 (%d != %d)" % (actual_size, img["size"])))
            continue
        if sha256_file(p) != img["sha256"]:
            bad.append((img["file"], "sha256 불일치 — 파일이 손상되었습니다"))
    return bad


def image_paths_in_write_order(pkg_dir, manifest):
    """esptool write_flash 에 넘길 [(offset, path), ...] 를 쓰기 순서로 돌려준다."""
    fwdir = os.path.join(pkg_dir, "firmware")
    return [(img["offset"], os.path.join(fwdir, img["file"]))
            for img in manifest["images"]]


def fw_summary(manifest):
    """GUI 헤더에 쓸 한 줄 요약."""
    return "%s · FW %s (%s) · %s · IDF %s" % (
        manifest.get("product", "?"),
        manifest.get("app_version", "?"),
        manifest.get("build_date", "?"),
        manifest.get("chip", "?"),
        manifest.get("idf_version", "?"),
    )


# ===================== CLI =====================
def _cmd_describe(build_dir):
    fa = read_flasher_args(build_dir)
    by_offset = {int(k, 16): v for k, v in fa["flash_files"].items()}
    app_src = os.path.join(build_dir, by_offset[max(by_offset)])
    part_src = [os.path.join(build_dir, v) for k, v in by_offset.items()
                if "partition" in v][0]

    desc = parse_app_desc(app_src)
    print("app_desc:")
    for k in ("project_name", "app_version", "idf_version", "build_date", "build_time"):
        print("   %-14s %s" % (k, desc[k]))

    print("\nwrite_flash_args:", " ".join(fa.get("write_flash_args", [])))
    print("esptool_extra   :", fa.get("extra_esptool_args"))

    print("\n파티션 테이블:")
    parts = parse_partition_table(part_src)
    for p in parts:
        print("   %-12s off=0x%06x size=0x%06x (%4dK)"
              % (p["label"], p["offset"], p["size"], p["size"] // 1024))

    pt_size = os.path.getsize(part_src)
    print("\npartition-table.bin 크기: %d B (한계 %d B) %s"
          % (pt_size, PART_TABLE_MAX_SIZE,
             "✅" if pt_size <= PART_TABLE_MAX_SIZE else "❌ NVS 를 지웁니다!"))

    app = find_partition(parts, "factory") or find_partition(parts, "app0")
    if app:
        used = os.path.getsize(app_src)
        pct = 100.0 * used / app["size"]
        print("앱 파티션 사용률: %d / %d B (%.1f%%) %s"
              % (used, app["size"], pct,
                 "⚠ 90%% 초과" if pct > 90 else "✅"))
    nvs = find_partition(parts, "nvs")
    if nvs:
        print("NVS: off=0x%x size=0x%x" % (nvs["offset"], nvs["size"]))
    return 0


def _cmd_verify(pkg_dir):
    m = load_manifest(pkg_dir)
    print(fw_summary(m))
    print("패키지 버전:", m.get("package_version"))
    print("WIFI_PREFER_NVS:", m.get("wifi_prefer_nvs"),
          "✅" if m.get("wifi_prefer_nvs") else "❌ 배포용 빌드가 아닙니다")
    bad = verify_images(pkg_dir, m)
    if bad:
        print("\n❌ 이미지 검증 실패:")
        for f, why in bad:
            print("   %-24s %s" % (f, why))
        return 1
    print("\n✅ 이미지 %d개 sha256 검증 통과" % len(m["images"]))
    print("쓰기 순서:")
    for off, p in image_paths_in_write_order(pkg_dir, m):
        print("   %-10s %s" % (off, os.path.basename(p)))
    return 0


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 2
    cmd, target = argv[1], argv[2]
    if cmd == "describe":
        return _cmd_describe(target)
    if cmd == "verify":
        return _cmd_verify(target)
    print("알 수 없는 명령: %s" % cmd, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
