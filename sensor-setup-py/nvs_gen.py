# -*- coding: utf-8 -*-
"""
ESP-IDF NVS 파티션 바이너리 생성기 (V2 포맷) — 순수 파이썬

외부 도구(nvs_partition_gen.py) 없이 NVS 바이너리를 직접 만든다.
표준 라이브러리 zlib 만 사용 → ESP-IDF 활성화 여부와 무관하게 동작.

검증된 Rust 구현(config-tool/src-tauri/src/nvs.rs)을 1:1 포팅한 것으로,
ESP-IDF nvs_partition_gen.py 가 만드는 바이너리와 동일한 구조를 재현한다.

엔트리(32B): ns(1) type(1) span(1) chunk_idx(1) crc32(4) key(16) data(8)
페이지(4096B): 헤더 32B + 상태 비트맵 32B + 엔트리 126개 × 32B
"""

import zlib

PAGE_SIZE = 4096
ENTRY_SIZE = 32
ENTRIES_PER_PAGE = 126  # (4096 - 32 - 32) / 32
NS_INDEX = 1            # devcfg 네임스페이스 인덱스

TYPE_U8 = 0x01
TYPE_U16 = 0x02
TYPE_STR = 0x21


def _nvs_crc(data, init=0xFFFFFFFF):
    """zlib.crc32(data, init) — Rust zlib_crc32 와 동일.
    파이썬 zlib.crc32 는 두 번째 인자로 이어받을 init 을 받는다."""
    return zlib.crc32(data, init) & 0xFFFFFFFF


def _entry_crc(entry):
    """엔트리 1개 CRC32. crc 필드[4..8] 제외하고 [0..4]+[8..32] 연속 계산."""
    c = zlib.crc32(entry[0:4], 0xFFFFFFFF)
    c = zlib.crc32(entry[8:32], c)
    return c & 0xFFFFFFFF


def _write_key(entry, key):
    """16바이트 key 필드(offset 8..24)에 기록, NULL 패딩."""
    kb = key.encode("utf-8")
    n = min(len(kb), 15)  # 최대 15 + NULL
    entry[8:8 + n] = kb[:n]
    for i in range(8 + n, 24):
        entry[i] = 0


class NvsPage:
    def __init__(self):
        self.entries = []   # 각 항목 bytearray(32)
        self.states = []    # written 여부

    def add_namespace(self, name, ns_index):
        e = bytearray(b"\xff" * ENTRY_SIZE)
        e[0] = 0           # ns_index 0 = namespace 엔트리 자체
        e[1] = TYPE_U8
        e[2] = 1           # span
        e[3] = 0xFF        # chunk_idx
        _write_key(e, name)
        e[24] = ns_index   # data[0] = ns_index
        crc = _entry_crc(e)
        e[4:8] = crc.to_bytes(4, "little")
        self.entries.append(e)
        self.states.append(True)

    def add_string(self, key, value):
        data = bytearray(value.encode("utf-8"))
        data.append(0)     # NULL 종료 포함
        data_len = len(data)

        data_entries = (len(data) + ENTRY_SIZE - 1) // ENTRY_SIZE
        span = 1 + data_entries
        data_crc = _nvs_crc(bytes(data))

        # 1) 헤더 엔트리
        head = bytearray(b"\xff" * ENTRY_SIZE)
        head[0] = NS_INDEX
        head[1] = TYPE_STR
        head[2] = span
        head[3] = 0xFF
        _write_key(head, key)
        head[24:26] = data_len.to_bytes(2, "little")  # size
        head[26] = 0xFF
        head[27] = 0xFF
        head[28:32] = data_crc.to_bytes(4, "little")  # data crc32
        crc = _entry_crc(head)
        head[4:8] = crc.to_bytes(4, "little")
        self.entries.append(head)
        self.states.append(True)

        # 2) 데이터 엔트리들 (32B 단위, 남는 부분 0xFF 패딩)
        padded = bytearray(data)
        while len(padded) % ENTRY_SIZE != 0:
            padded.append(0xFF)
        for i in range(0, len(padded), ENTRY_SIZE):
            self.entries.append(bytearray(padded[i:i + ENTRY_SIZE]))
            self.states.append(True)

    def _add_primitive(self, key, ty, le_bytes):
        e = bytearray(b"\xff" * ENTRY_SIZE)
        e[0] = NS_INDEX
        e[1] = ty
        e[2] = 1
        e[3] = 0xFF
        _write_key(e, key)
        e[24:24 + len(le_bytes)] = le_bytes
        crc = _entry_crc(e)
        e[4:8] = crc.to_bytes(4, "little")
        self.entries.append(e)
        self.states.append(True)

    def add_u8(self, key, val):
        self._add_primitive(key, TYPE_U8, bytes([val & 0xFF]))

    def add_u16(self, key, val):
        self._add_primitive(key, TYPE_U16, (val & 0xFFFF).to_bytes(2, "little"))

    def serialize(self):
        page = bytearray(b"\xff" * PAGE_SIZE)

        # 페이지 헤더 (32B)
        page[0:4] = bytes([0xFE, 0xFF, 0xFF, 0xFF])  # state = ACTIVE
        page[4:8] = (0).to_bytes(4, "little")        # seqno = 0
        page[8] = 0xFE                               # version = V2
        # [9..28] 0xFF 유지
        hdr_crc = _nvs_crc(bytes(page[4:28]))        # 헤더 CRC 대상 [4..28]
        page[28:32] = hdr_crc.to_bytes(4, "little")

        # 엔트리 상태 비트맵 (32B @ offset 32): written=10, empty=11
        for i in range(ENTRIES_PER_PAGE):
            if i < len(self.states) and self.states[i]:
                byte_idx = 32 + (i // 4)
                bit_pos = (i % 4) * 2
                page[byte_idx] &= ~(1 << bit_pos) & 0xFF

        # 엔트리들 (@ offset 64)
        for i, entry in enumerate(self.entries):
            off = 64 + i * ENTRY_SIZE
            page[off:off + ENTRY_SIZE] = entry

        return bytes(page)


def generate_full_nvs(namespace, ssid, password, srv_ip, srv_port,
                      stream_rate, transport, read_mode,
                      partition_size=0x6000):
    """WiFi + 서버 스트리밍 설정을 담은 NVS 파티션 바이너리 생성.

    엔트리 순서(펌웨어/Rust툴과 동일):
      namespace → wifi_ssid → wifi_pass → srv_ip → srv_port
      → stream_rate → transport → read_mode
    """
    if partition_size < PAGE_SIZE * 2:
        raise ValueError("NVS 파티션 크기가 너무 작습니다 (최소 8KB)")
    if partition_size % PAGE_SIZE != 0:
        raise ValueError("NVS 파티션 크기는 4096의 배수여야 합니다")
    if not ssid or len(ssid.encode("utf-8")) > 32:
        raise ValueError("SSID 길이가 올바르지 않습니다 (1~32 바이트)")
    if len(password.encode("utf-8")) > 64:
        raise ValueError("비밀번호가 너무 깁니다 (최대 64)")
    if not srv_ip or len(srv_ip) > 15:
        raise ValueError("서버 IP 형식이 올바르지 않습니다")

    page = NvsPage()
    page.add_namespace(namespace, NS_INDEX)
    page.add_string("wifi_ssid", ssid)
    page.add_string("wifi_pass", password)
    page.add_string("srv_ip", srv_ip)
    page.add_u16("srv_port", srv_port)
    page.add_u8("stream_rate", stream_rate)
    page.add_u8("transport", transport)
    page.add_u8("read_mode", read_mode)

    first = page.serialize()
    out = bytearray(b"\xff" * partition_size)
    out[0:PAGE_SIZE] = first
    return bytes(out)


if __name__ == "__main__":
    # 간단 자가 점검: 바이너리 생성 + 구조 검증
    b = generate_full_nvs(
        "devcfg", "example2.4G", "example1234", "192.168.0.37",
        9000, 1, 0, 1, 0x6000,
    )
    assert len(b) == 0x6000, "파티션 크기 불일치"
    assert b[0:4] == bytes([0xFE, 0xFF, 0xFF, 0xFF]), "페이지 state 불일치"
    assert b[8] == 0xFE, "NVS 버전(V2) 불일치"
    print("✅ nvs_gen 자가 점검 통과: %d 바이트, V2 ACTIVE 페이지" % len(b))
