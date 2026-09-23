#!/usr/bin/env python3
"""바이너리 수집 파일(.bin) → CSV 변환.

    ./bin_to_csv.py run_0001_A.bin                     같은 폴더에 .csv 생성
    ./bin_to_csv.py run_0001_A.bin -o out.csv
    ./bin_to_csv.py run_0001_A.bin --from 10 --to 20    10~20초 구간만
    ./bin_to_csv.py run_0001_A.bin --check              변환 없이 검사만
    ./bin_to_csv.py --selftest                          자체 검증

## 시각 열(recv_iso)에 대하여

CSV 로 직접 수집하면 `recv_iso` 는 **호스트가 그 패킷을 기록한 시각**이다.
바이너리에는 그 값이 없다 (호스트 시각을 패킷마다 저장하면 용량 이득이 사라진다).

그래서 변환 시에는 **디바이스 시계로 재구성한다**:

    recv_iso = 사이드카의 started + (timestamp_ms − 첫 패킷의 timestamp_ms)

디바이스의 `timestamp_ms` 는 부팅 후 경과 시간이라 지터가 없다. 따라서 재구성된
시각은 호스트 기록 시각보다 오히려 **균일하다**. 나머지 모든 열
(`seq`·`timestamp_ms`·`sample_idx`·raw·mg)은 원본과 완전히 같다.
"""

import argparse
import json
import os
import struct
import sys
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from iis3dwb_packet import MAGIC_LE, SENSITIVITY, parse_header
from writer import CSV_HEADER, FILE_BUFSIZE

MAX_SAMPLES = 1024


def read_sidecar(bin_path):
    """이 .bin 을 읽는 데 필요한 정보를 담은 json 을 찾는다.

    두 위치를 본다:
      · `run_0012.json`      수집 채널이 남기는 run 메타 (기본)
      · `run_0012.bin.json`  Writer 단독 사용 시의 사이드카

    둘 다 없으면 빈 사전 — 변환은 계속 가능하지만 시각 열이 '+경과초' 가 된다.
    """
    p = Path(bin_path)
    for cand in (p.with_suffix(".json"), Path(str(p) + ".json")):
        try:
            d = json.loads(cand.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                return d
        except (OSError, ValueError):
            continue
    return {}


def iter_packets(data, *, default_fs_g=4):
    """바이너리에서 (헤더, 페이로드) 를 순서대로 뽑는다.

    수집기가 쓴 파일은 패킷이 빈틈없이 이어져 있지만, 디스크가 차서 잘린 경우를
    대비해 magic 으로 경계를 확인하며 나아간다. 깨진 구간은 건너뛰고 계속한다.
    """
    off = 0
    n = len(data)
    skipped = 0
    while off < n:
        if data[off:off + 4] != MAGIC_LE:
            i = data.find(MAGIC_LE, off)
            if i < 0:
                skipped += n - off
                break
            skipped += i - off
            off = i
        hdr = parse_header(data, off, default_fs_g=default_fs_g)
        if hdr is None or hdr.count == 0 or hdr.count > MAX_SAMPLES:
            skipped += 4
            off += 4
            continue
        end = off + hdr.total_bytes
        if end > n:
            skipped += n - off          # 마지막 패킷이 잘렸다
            break
        yield hdr, data[off + hdr.size:end]
        off = end
    iter_packets.skipped = skipped


def convert(bin_path, out_path=None, *, t_from=None, t_to=None, quiet=False):
    """변환. 통계 사전을 돌려준다."""
    bin_path = Path(bin_path)
    data = bin_path.read_bytes()
    side = read_sidecar(bin_path)
    default_fs = int(side.get("full_scale_g") or 4)

    started = None
    if side.get("started"):
        try:
            started = datetime.fromisoformat(side["started"])
        except ValueError:
            started = None

    if out_path is None:
        out_path = bin_path.with_suffix(".csv")
    out_path = Path(out_path)

    packets = samples = 0
    first_ts = None
    seqs = []
    fs_seen = set()

    with open(out_path, "w", encoding="utf-8", newline="",
              buffering=FILE_BUFSIZE) as f:
        f.write(CSV_HEADER)
        for hdr, payload in iter_packets(data, default_fs_g=default_fs):
            if first_ts is None:
                first_ts = hdr.timestamp_ms
            rel_s = (hdr.timestamp_ms - first_ts) / 1000.0
            if t_from is not None and rel_s < t_from:
                continue
            if t_to is not None and rel_s > t_to:
                break

            if started is not None:
                iso = (started + timedelta(seconds=rel_s)).strftime(
                    "%Y-%m-%dT%H:%M:%S.%f")[:-3]
            else:
                # 사이드카가 없으면 디바이스 경과시간만 남긴다 — 비워 두는 것보다
                # 낫고, 시각을 꾸며내지도 않는다.
                iso = "+%.3fs" % rel_s

            sens = hdr.sensitivity
            fs_seen.add(hdr.full_scale_g)
            vals = struct.unpack("<%dh" % (hdr.count * 3), payload)
            f.write("".join(
                "%s,%d,%d,%d,%d,%d,%d,%.2f,%.2f,%.2f\n" % (
                    iso, hdr.seq, hdr.timestamp_ms, k,
                    vals[k * 3], vals[k * 3 + 1], vals[k * 3 + 2],
                    vals[k * 3] * sens, vals[k * 3 + 1] * sens,
                    vals[k * 3 + 2] * sens)
                for k in range(hdr.count)))
            packets += 1
            samples += hdr.count
            seqs.append(hdr.seq)

    gaps = sum((b - a - 1) for a, b in zip(seqs, seqs[1:])
               if 0 < (b - a - 1) < 1_000_000)
    stats = {
        "in": str(bin_path), "out": str(out_path),
        "in_bytes": len(data), "out_bytes": out_path.stat().st_size,
        "packets": packets, "samples": samples,
        "seq_gaps": gaps, "skipped_bytes": getattr(iter_packets, "skipped", 0),
        "full_scale_g": sorted(fs_seen),
        "sidecar": bool(side),
    }
    if not quiet:
        print("변환 완료")
        print("  입력   %s (%s)" % (stats["in"], _hb(stats["in_bytes"])))
        print("  출력   %s (%s)" % (stats["out"], _hb(stats["out_bytes"])))
        print("  패킷   %d · 샘플 %d" % (packets, samples))
        print("  풀스케일 ±%sg" % "/".join(str(x) for x in stats["full_scale_g"]))
        if gaps:
            print("  ⚠ seq 공백 %d패킷 — 수집 당시 유실이 있었습니다" % gaps)
        if stats["skipped_bytes"]:
            print("  ⚠ 건너뛴 바이트 %d — 파일이 잘렸거나 손상됐습니다"
                  % stats["skipped_bytes"])
        if not side:
            print("  ℹ 사이드카(.json)가 없어 시각 열이 '+경과초' 로 기록됩니다")
    return stats


def check(bin_path):
    """변환하지 않고 무결성만 본다."""
    bin_path = Path(bin_path)
    data = bin_path.read_bytes()
    side = read_sidecar(bin_path)
    packets = samples = 0
    seqs = []
    for hdr, _ in iter_packets(data, default_fs_g=int(side.get("full_scale_g") or 4)):
        packets += 1
        samples += hdr.count
        seqs.append(hdr.seq)
    gaps = sum((b - a - 1) for a, b in zip(seqs, seqs[1:])
               if 0 < (b - a - 1) < 1_000_000)
    skipped = getattr(iter_packets, "skipped", 0)
    print("검사: %s (%s)" % (bin_path, _hb(len(data))))
    print("  패킷 %d · 샘플 %d · seq 공백 %d · 건너뜀 %dB" % (packets, samples, gaps, skipped))
    if side:
        ok = (side.get("packets") == packets and side.get("samples") == samples)
        print("  사이드카 대조: %s (기록 %s패킷/%s샘플)"
              % ("일치 ✓" if ok else "불일치 ✗",
                 side.get("packets"), side.get("samples")))
        if not ok:
            return 1
    return 0 if (gaps == 0 and skipped == 0) else 1


def _hb(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return "%.1f %s" % (n, u)
        n /= 1024.0


# ===================== 자체 검증 =====================

def selftest():
    """CSV 직접 기록과 바이너리→CSV 변환이 같은 값을 내는지 확인한다.

    `recv_iso` 열만 정의상 다르다 (위 docstring 참조). 나머지 9개 열이
    한 글자도 다르지 않아야 한다 — 이것이 "바이너리로 저장해도 정보 손실이
    없다" 는 주장의 근거다.
    """
    import queue
    import tempfile
    import writer as writer_mod
    from iis3dwb_packet import HEADER_V2, MAGIC

    fails = []
    n_pkt, n_samp, fs = 7, 200, 8
    rng = 12345

    def make(seq):
        nonlocal rng
        vals = []
        for _ in range(n_samp * 3):
            rng = (rng * 1103515245 + 12345) & 0x7FFFFFFF
            vals.append((rng % 60000) - 30000)
        payload = struct.pack("<%dh" % len(vals), *vals)
        raw = HEADER_V2.pack(MAGIC, 2, 4, n_samp, seq, 1000 + seq * 7, fs, 0)
        hdr = parse_header(raw + payload, 0)
        return hdr, payload

    pkts = [make(i + 1) for i in range(n_pkt)]

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        # 같은 패킷을 CSV 와 BIN 두 경로로 각각 기록
        outs = {}
        for fmt in ("csv", "bin"):
            q = queue.Queue()
            for p in pkts:
                q.put(p)
            w = writer_mod.Writer(td / ("t." + fmt), q, fmt,
                                  meta={"full_scale_g": fs,
                                        "started": "2026-09-23T10:00:00"})
            w.start()
            w.stop(drain_timeout=5)
            if w.error:
                fails.append("%s 기록 오류: %s" % (fmt, w.error))
            outs[fmt] = td / ("t." + fmt)

        st = convert(outs["bin"], td / "converted.csv", quiet=True)
        if st["packets"] != n_pkt or st["samples"] != n_pkt * n_samp:
            fails.append("변환 개수 불일치: %r" % st)
        if st["seq_gaps"] or st["skipped_bytes"]:
            fails.append("변환 중 공백/건너뜀 발생: %r" % st)

        direct = (td / "t.csv").read_text(encoding="utf-8").splitlines()
        conv = (td / "converted.csv").read_text(encoding="utf-8").splitlines()
        if len(direct) != len(conv):
            fails.append("줄 수 불일치: 직접 %d ≠ 변환 %d" % (len(direct), len(conv)))
        else:
            diff = 0
            for a, b in zip(direct[1:], conv[1:]):
                # recv_iso(첫 열)만 정의상 다르다 — 나머지는 완전히 같아야 한다
                if a.split(",", 1)[1] != b.split(",", 1)[1]:
                    diff += 1
            if diff:
                fails.append("데이터 열 불일치 %d줄" % diff)
            if direct[0] != conv[0]:
                fails.append("헤더 줄 불일치")

        # 사이드카가 없을 때도 변환이 되는지
        nos = td / "nosidecar.bin"
        nos.write_bytes(outs["bin"].read_bytes())
        st2 = convert(nos, td / "nos.csv", quiet=True)
        if st2["samples"] != n_pkt * n_samp:
            fails.append("사이드카 없는 변환 실패: %r" % st2)

        # 구간 자르기
        st3 = convert(outs["bin"], td / "cut.csv", t_from=0.0, t_to=0.02, quiet=True)
        if not (0 < st3["packets"] < n_pkt):
            fails.append("--from/--to 구간 자르기가 동작하지 않음: %r" % st3)

    if fails:
        print("FAIL")
        for f in fails:
            print("  ·", f)
        return 1
    print("PASS — CSV 직접기록과 바이너리→CSV 변환이 데이터 열에서 완전히 일치")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="IIS3DWB 바이너리 수집 파일 → CSV")
    ap.add_argument("bin", nargs="?", help="변환할 .bin 파일")
    ap.add_argument("-o", "--out", help="출력 CSV (기본: 같은 이름 .csv)")
    ap.add_argument("--from", dest="t_from", type=float,
                    help="시작 시각 (수집 시작 후 초)")
    ap.add_argument("--to", dest="t_to", type=float, help="종료 시각 (초)")
    ap.add_argument("--check", action="store_true", help="변환 없이 무결성 검사")
    ap.add_argument("--selftest", action="store_true", help="자체 검증")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()
    if not args.bin:
        ap.error("변환할 .bin 파일을 지정하세요 (또는 --selftest)")
    if not os.path.exists(args.bin):
        print("❌ 파일이 없습니다: %s" % args.bin, file=sys.stderr)
        return 2
    if args.check:
        return check(args.bin)
    convert(args.bin, args.out, t_from=args.t_from, t_to=args.t_to)
    return 0


if __name__ == "__main__":
    sys.exit(main())
