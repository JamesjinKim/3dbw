#!/usr/bin/env python3
"""IIS3DWB 스트리밍 패킷 파서 (UDP·시리얼 공용)

펌웨어 `components/sensor_streamer/sensor_streamer.h` 의 패킷 계약을 한 곳에
모아둔다. UDP 수신기와 시리얼 수신기가 같은 파서를 쓰므로, 펌웨어 헤더가
바뀌면 이 파일만 고치면 된다.

헤더 레이아웃 (packed, little-endian):

  v1 (16B): magic(4) version(1) rate_step(1) sample_count(2) seq(4) timestamp_ms(4)
  v2 (18B): v1 + full_scale_g(1) reserved(1)

v2 는 v1 뒤에 2바이트를 덧붙인 것이라 앞 16바이트 레이아웃이 같다. 따라서
`version` 을 읽어 헤더 크기를 정해야 한다. v2 패킷을 v1 으로 파싱하면 샘플
시작 오프셋이 2바이트 밀려 **X/Y/Z 축이 한 칸씩 어긋난 채 조용히 기록된다**
(길이 검사도 통과하므로 오류가 드러나지 않는다).
"""

import struct

MAGIC = 0x49495333          # "IIS3"
MAGIC_LE = MAGIC.to_bytes(4, "little")   # 스트림에서 경계를 찾을 때 쓰는 바이트열

HEADER_V1 = struct.Struct("<IBBHII")     # 16 bytes
HEADER_V2 = struct.Struct("<IBBHIIBB")   # 18 bytes

# rate_step → Hz (펌웨어 sensor_streamer_rate_hz() 와 일치)
RATE_HZ = {0: 1000, 1: 3333, 2: 6667, 3: 13333, 4: 26667}

# 풀스케일별 감도 (mg/LSB)
SENSITIVITY = {2: 0.061, 4: 0.122, 8: 0.244, 16: 0.488}

SAMPLE_BYTES = 6            # int16 × 3축


class Header:
    """파싱된 패킷 헤더. `size` 는 이 패킷의 실제 헤더 바이트 수."""

    __slots__ = ("version", "rate_step", "count", "seq", "timestamp_ms",
                 "full_scale_g", "size")

    def __init__(self, version, rate_step, count, seq, timestamp_ms,
                 full_scale_g, size):
        self.version = version
        self.rate_step = rate_step
        self.count = count
        self.seq = seq
        self.timestamp_ms = timestamp_ms
        self.full_scale_g = full_scale_g
        self.size = size

    @property
    def payload_bytes(self):
        return self.count * SAMPLE_BYTES

    @property
    def total_bytes(self):
        return self.size + self.payload_bytes

    @property
    def sensitivity(self):
        """mg/LSB. 풀스케일이 표에 없으면(손상·미래 값) ±4g 로 폴백."""
        return SENSITIVITY.get(self.full_scale_g, SENSITIVITY[4])


def parse_header(buf, offset=0, default_fs_g=4):
    """buf[offset:] 에서 헤더를 파싱해 Header 를 돌려준다.

    magic 불일치나 길이 부족이면 None. `default_fs_g` 는 v1 패킷에만 쓰인다
    (v1 헤더에는 풀스케일 정보가 없으므로 사용자가 지정해야 한다).
    """
    if len(buf) - offset < HEADER_V1.size:
        return None

    magic, version = struct.unpack_from("<IB", buf, offset)
    if magic != MAGIC:
        return None

    # v2 이상은 앞부분 레이아웃을 유지하므로 `>= 2` 로 비교한다.
    # (v3 가 나와도 뒤에만 덧붙는 한 이 코드가 계속 동작한다)
    if version >= 2:
        if len(buf) - offset < HEADER_V2.size:
            return None
        _, _, rate_step, count, seq, ts, fs_g, _ = HEADER_V2.unpack_from(buf, offset)
        size = HEADER_V2.size
    else:
        _, _, rate_step, count, seq, ts = HEADER_V1.unpack_from(buf, offset)
        fs_g = default_fs_g
        size = HEADER_V1.size

    return Header(version, rate_step, count, seq, ts, fs_g, size)


def parse_samples(buf, hdr, offset=0):
    """헤더 뒤의 샘플 배열을 (x0,y0,z0, x1,y1,z1, …) 튜플로 돌려준다.

    바이트가 부족하면 None (시리얼에서 패킷이 아직 덜 들어온 경우).
    """
    start = offset + hdr.size
    if len(buf) - start < hdr.payload_bytes:
        return None
    return struct.unpack_from("<%dh" % (hdr.count * 3), buf, start)


class SeqTracker:
    """seq 불연속으로 유실 패킷 수를 센다 (32비트 랩어라운드 고려)."""

    def __init__(self):
        self.last = None
        self.lost = 0

    def update(self, seq):
        if self.last is not None:
            gap = (seq - self.last - 1) & 0xFFFFFFFF
            # 디바이스 재부팅 등으로 seq 가 크게 튀면 유실로 세지 않는다
            if 0 < gap < 1_000_000:
                self.lost += gap
        self.last = seq
