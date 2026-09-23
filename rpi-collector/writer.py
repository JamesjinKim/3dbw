#!/usr/bin/env python3
"""쓰기 스레드 — 큐에서 패킷을 꺼내 CSV 또는 바이너리로 저장한다.

## 두 형식

| 형식 | 내용 | 26.6kHz 1대 기준 |
|---|---|---|
| CSV | 사람이 바로 열어 보는 형식. raw(LSB)와 mg 를 함께 기록 | 약 96 MB/분 |
| 바이너리 | 헤더+페이로드를 받은 그대로 append. 옆에 `.json` 메타 | 약 9.7 MB/분 |

바이너리는 용량이 1/10 이고 쓰기 부담이 사실상 없다. 나중에 `bin_to_csv.py` 로
같은 CSV 를 만들 수 있으므로 정보 손실은 없다.

## 성능

실측(라즈베리파이 4, 1코어): CSV 형식화 **181,566 샘플/s**. 26.6kHz × 2대에
필요한 53,334 샘플/s 의 3.4배라 형식화는 병목이 아니다.

과거 "26.6kHz 에서 1.5% 유실" 의 원인은 형식화 속도가 아니라 **샘플을 리스트에
누적한 탓의 GC 정지**였다. 그래서 이 구현은 큐에서 꺼낸 패킷을 **즉시 문자열로
바꿔 파일 버퍼로 넘기고 참조를 놓는다.** 어떤 것도 누적하지 않는다.
"""

import json
import os
import struct
import threading
import time
from datetime import datetime

CSV_HEADER = ("recv_iso,seq,timestamp_ms,sample_idx,"
              "x_raw,y_raw,z_raw,x_mg,y_mg,z_mg\n")

# 파일 쓰기 버퍼. 크게 잡아 write() 호출 수를 줄인다 (1 MB ≈ CSV 0.6초 분량).
FILE_BUFSIZE = 1 << 20

# 샘플당 CSV 바이트 수 — 용량 프리플라이트에 쓴다.
# 실측: 26.6kHz·±8g 30초 수집이 802,400샘플 / 56 MB → 샘플당 약 70 B.
# 과소 추정하면 프리플라이트를 통과한 뒤 디스크가 차서 수집이 깨지므로,
# 실측값을 그대로 쓴다.
CSV_BYTES_PER_SAMPLE = 70
BIN_BYTES_PER_SAMPLE = 6 * 1.015     # 페이로드 6B + 200샘플당 헤더 18B(≈1.5%)


def estimate_bytes(fmt, minutes, hz, sensors):
    """수집 예상 용량(바이트). GUI 프리플라이트용."""
    per = CSV_BYTES_PER_SAMPLE if fmt == "csv" else BIN_BYTES_PER_SAMPLE
    return int(minutes * 60 * hz * per * sensors)


def human_bytes(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%.0f %s" % (n, unit) if unit in ("B", "KB") else "%.1f %s" % (n, unit)
        n /= 1024.0


class Writer:
    """큐 → 파일. 세션 하나당 하나씩 만든다.

    `stop()` 이 돌아온 뒤에는 파일이 닫혀 있고 `written_samples` 가 확정된다.
    """

    def __init__(self, path, queue, fmt="csv", *, meta=None, sidecar=True):
        """sidecar=False 면 `.bin.json` 을 만들지 않는다.

        수집 채널은 `run_NNNN.json` 에 모든 정보를 남기므로 사이드카가 중복이다.
        한 수집에 json 이 두 개 생기면 어느 것을 봐야 하는지 헷갈린다.
        """
        if fmt not in ("csv", "bin"):
            raise ValueError("fmt 는 'csv' 또는 'bin' 이어야 합니다: %r" % fmt)
        self.path = str(path)
        self.queue = queue
        self.fmt = fmt
        self.meta = dict(meta or {})
        self.sidecar = sidecar

        self.written_samples = 0
        self.written_packets = 0
        self.error = None

        self._f = None
        self._thread = None
        self._stop = threading.Event()

    # ---------- 수명 ----------
    def start(self):
        mode = "w" if self.fmt == "csv" else "wb"
        kw = {"buffering": FILE_BUFSIZE}
        if self.fmt == "csv":
            kw["encoding"] = "utf-8"
            kw["newline"] = ""
        self._f = open(self.path, mode, **kw)
        if self.fmt == "csv":
            self._f.write(CSV_HEADER)

        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="writer",
                                        daemon=True)
        self._thread.start()

    def stop(self, drain_timeout=10.0):
        """수집 종료. 큐에 남은 것을 모두 쓴 뒤 닫는다.

        여기서 서두르면 마지막 몇 초가 잘린다. 큐가 빌 때까지 기다리되,
        상한을 둬서 영원히 멈추지 않게 한다.
        """
        deadline = time.monotonic() + drain_timeout
        while not self.queue.empty() and time.monotonic() < deadline:
            time.sleep(0.05)
        self._stop.set()
        if self._thread:
            self._thread.join(max(1.0, drain_timeout))
        if self._f:
            try:
                self._f.flush()
                os.fsync(self._f.fileno())
            except Exception:
                pass
            self._f.close()
            self._f = None
        if self.fmt == "bin" and self.sidecar:
            self._write_sidecar()

    # ---------- 루프 ----------
    def _loop(self):
        try:
            if self.fmt == "csv":
                self._loop_csv()
            else:
                self._loop_bin()
        except Exception as e:       # 디스크 가득참 등 — 조용히 끝내지 않는다
            self.error = "%s 쓰기 실패: %s" % (self.path, e)

    def _loop_csv(self):
        write = self._f.write
        while not self._stop.is_set() or not self.queue.empty():
            try:
                hdr, payload = self.queue.get(timeout=0.2)
            except Exception:
                continue
            recv_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
            sens = hdr.sensitivity
            seq = hdr.seq
            ts = hdr.timestamp_ms
            vals = struct.unpack("<%dh" % (hdr.count * 3), payload)
            # 한 패킷(200샘플)을 한 문자열로 모아 한 번에 넘긴다.
            # 줄마다 write() 를 부르면 호출 비용이 형식화 비용을 넘어선다.
            write("".join(
                "%s,%d,%d,%d,%d,%d,%d,%.2f,%.2f,%.2f\n" % (
                    recv_iso, seq, ts, k,
                    vals[k * 3], vals[k * 3 + 1], vals[k * 3 + 2],
                    vals[k * 3] * sens, vals[k * 3 + 1] * sens,
                    vals[k * 3 + 2] * sens)
                for k in range(hdr.count)))
            self.written_packets += 1
            self.written_samples += hdr.count

    def _loop_bin(self):
        write = self._f.write
        while not self._stop.is_set() or not self.queue.empty():
            try:
                hdr, payload = self.queue.get(timeout=0.2)
            except Exception:
                continue
            # 받은 그대로 다시 조립해 저장한다. 헤더를 버리면 seq·timestamp 가
            # 사라져 유실 분석이 불가능해진다.
            write(_rebuild_header(hdr))
            write(payload)
            self.written_packets += 1
            self.written_samples += hdr.count

    def _write_sidecar(self):
        """바이너리 옆의 `.json` — 이 파일을 읽는 데 필요한 정보."""
        side = dict(self.meta)
        side.update({
            "format": "iis3dwb-raw-packets",
            "note": "패킷(헤더+페이로드)이 받은 순서대로 이어져 있습니다. "
                    "bin_to_csv.py 로 CSV 변환.",
            "packets": self.written_packets,
            "samples": self.written_samples,
        })
        try:
            with open(self.path + ".json", "w", encoding="utf-8") as f:
                json.dump(side, f, ensure_ascii=False, indent=2)
                f.write("\n")
        except Exception as e:
            self.error = self.error or ("사이드카 기록 실패: %s" % e)


def _rebuild_header(hdr):
    """파싱된 헤더를 원래 바이트로 되돌린다 (v1/v2 모두)."""
    from iis3dwb_packet import MAGIC, HEADER_V1, HEADER_V2
    if hdr.version >= 2:
        return HEADER_V2.pack(MAGIC, hdr.version, hdr.rate_step, hdr.count,
                              hdr.seq, hdr.timestamp_ms, hdr.full_scale_g, 0)
    return HEADER_V1.pack(MAGIC, hdr.version, hdr.rate_step, hdr.count,
                          hdr.seq, hdr.timestamp_ms)
