#!/usr/bin/env python3
"""센서 1대의 읽기 스레드 — 시리얼에서 패킷을 뽑아 큐에 넣는다.

## 설계 의도

**읽기 스레드는 읽고 자르기만 한다.** 형식화(CSV 문자열 만들기)는 쓰기 스레드가
맡는다. 두 일을 한 스레드에서 하면 CSV 를 만드는 동안 시리얼 버퍼가 넘쳐 유실된다.

**대기 중에도 계속 읽는다.** 디바이스는 부팅 후 무조건 단방향 송신하며 수신기
상태와 무관하다 (설계문서 7.5). 읽지 않으면 OS 버퍼에 과거 데이터가 쌓여, 수집을
시작하는 순간 오래된 데이터부터 나온다. 계속 읽어 버리면 동기가 유지되고,
덤으로 대기 중에도 실시간 Hz·중력 1g 를 화면에 보여줄 수 있다.

**유실을 조용히 만들지 않는다.** 큐가 가득 차면 버리되 **센다**. 무한히 쌓게 두면
메모리가 늘어나다 GC 정지가 생기고, 그 정지 동안 진짜 유실이 발생한다
(과거 26.6kHz 측정에서 476 ms 공백 → 63패킷 유실을 이렇게 겪었다).
"""

import struct
import threading
import time

import vendor_path  # noqa: F401  — 배포 패키지의 vendor/pyserial 을 먼저 잡는다
import serial

from iis3dwb_packet import MAGIC_LE, RATE_HZ, parse_header, SeqTracker


def _tail_mg(payload, sensitivity):
    """패킷의 마지막 샘플을 mg 로. 화면의 중력 1g 확인에 쓴다."""
    if len(payload) < 6:
        return (0.0, 0.0, 0.0)
    x, y, z = struct.unpack_from("<3h", payload, len(payload) - 6)
    return (x * sensitivity, y * sensitivity, z * sensitivity)

# 헤더의 sample_count 가 이 값을 넘으면 샘플 데이터에 우연히 나타난 가짜 magic 으로 본다.
# (펌웨어 STREAM_SAMPLES_PER_PACKET = 200)
MAX_SAMPLES = 1024

DEFAULT_BAUD = 2000000      # 펌웨어 CONFIG_STREAM_SERIAL_UART_BAUD 와 같아야 한다


class LinkStats:
    """센서 하나의 누적 상태. 읽기 스레드가 쓰고 GUI 가 읽는다.

    파이썬 정수 대입은 원자적이라 잠금 없이 읽어도 찢어진 값이 나오지 않는다.
    화면 표시용이므로 순간적으로 몇 개 어긋나는 것은 문제가 되지 않는다.
    """

    __slots__ = ("packets", "samples", "lost", "queue_drops", "resync_bytes",
                 "read_errors", "last_seen", "rate_step", "full_scale_g",
                 "version", "hz", "mg", "connected")

    def __init__(self):
        self.packets = 0
        self.samples = 0
        self.lost = 0            # seq 불연속으로 추정한 유실 패킷 수
        self.queue_drops = 0     # 쓰기가 밀려 버린 패킷 수
        self.resync_bytes = 0    # magic 을 찾으며 버린 바이트 (부팅 로그 등)
        self.read_errors = 0
        self.last_seen = 0.0     # 마지막으로 패킷을 받은 시각 (time.monotonic)
        self.rate_step = None
        self.full_scale_g = None
        self.version = None
        self.hz = 0.0            # 최근 1초 실효 샘플레이트
        self.mg = (0.0, 0.0, 0.0)  # 최근 샘플 (mg) — 1g 확인용
        self.connected = False

    @property
    def loss_pct(self):
        # lost 는 패킷 단위, samples 는 샘플 단위라 패킷 수로 맞춰 비교한다.
        total = self.packets + self.lost
        return (100.0 * self.lost / total) if total else 0.0


class SensorLink:
    """센서 한 대의 시리얼 연결 + 읽기 스레드.

    `recording` 을 True 로 두면 읽은 패킷을 `queue` 에 넣는다. False 면 파싱만
    하고 버린다 (동기 유지 + 화면 표시용 통계는 계속 갱신).
    """

    def __init__(self, name, port, queue, *, baud=DEFAULT_BAUD, default_fs_g=4):
        self.name = name
        self.port = port
        self.queue = queue
        self.baud = baud
        self.default_fs_g = default_fs_g

        self.stats = LinkStats()
        self.recording = False          # 세션이 켜고 끈다
        self._ser = None
        self._thread = None
        self._stop = threading.Event()
        self._error = None              # 치명적 오류 메시지 (케이블 분리 등)
        # flush() 는 요청만 하고 실제 비우기는 읽기 스레드가 한다 (flush 참고)
        self._resync_req = threading.Event()
        self._resync_done = threading.Event()

    # ---------- 수명 ----------
    def open(self):
        """포트를 연다. 실패하면 예외를 올린다 (호출자가 사용자에게 알린다)."""
        self._ser = serial.Serial(self.port, self.baud, timeout=0.2)
        # 중단 후 재개 시 OS 버퍼의 과거 데이터를 버리고 시작한다 (설계문서 7.6)
        self._ser.reset_input_buffer()
        self.stats.connected = True

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="link-" + self.name,
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout=2.0):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)
        if self._ser:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None
        self.stats.connected = False

    @property
    def error(self):
        return self._error

    def flush(self, timeout=1.0):
        """수집 시작 직전에 호출 — OS 버퍼의 과거 데이터를 버린다.

        **비우기는 읽기 스레드가 한다.** 호출자 스레드에서 직접
        `reset_input_buffer()` 를 하면 읽기 스레드가 조립 중이던 패킷의 뒷부분이
        사라지고, 그 자리가 seq 불연속으로 보여 **우리가 만든 끊김이 '유실'로
        기록된다.** (실제로 2대 동시 수집에서 한쪽만 유실 1패킷으로 잡혔고,
        원인은 이 flush 였다)

        읽기 스레드는 자기 조립 버퍼까지 같이 비우고 seq 기준점을 새로 잡으므로
        경계가 어디서 잘리든 유실로 세지 않는다. 비우기가 끝날 때까지 기다렸다가
        돌아오므로, 호출자는 이 함수가 반환한 뒤에 `recording = True` 로 두면
        과거 데이터가 섞이지 않는다.
        """
        if not self._ser:
            return
        if not (self._thread and self._thread.is_alive()):
            # 읽기 스레드가 없으면 직접 — 잘릴 패킷도 없다
            try:
                self._ser.reset_input_buffer()
            except Exception:
                pass
            return
        self._resync_done.clear()
        self._resync_req.set()
        # 초당 수십 회 도는 루프라 보통 수 ms 안에 끝난다. 타임아웃은 스레드가
        # 멈춘 이상 상황에서 영원히 매달리지 않기 위한 안전장치일 뿐이다.
        self._resync_done.wait(timeout)

    # ---------- 읽기 루프 ----------
    def _loop(self):
        buf = bytearray()
        seqt = SeqTracker()
        st = self.stats
        win_samples = 0
        win_t0 = time.monotonic()
        last_mg = (0.0, 0.0, 0.0)       # 가장 최근 샘플 (1g 확인용)

        while not self._stop.is_set():
            if self._resync_req.is_set():
                # flush() 요청. OS 버퍼와 조립 버퍼를 함께 비우고 seq 기준점을
                # 새로 잡는다. 여기서 끊은 자리는 우리가 일부러 끊은 것이므로
                # 유실로도, 재동기 쓰레기로도 세지 않는다.
                self._resync_req.clear()
                try:
                    self._ser.reset_input_buffer()
                except Exception:
                    pass
                del buf[:]
                seqt.last = None
                self._resync_done.set()
            try:
                chunk = self._ser.read(max(self._ser.in_waiting, 1))
            except Exception as e:
                # 케이블이 빠지면 여기로 온다. 조용히 멈추지 않고 이유를 남긴다.
                self._error = "%s 읽기 실패: %s" % (self.port, e)
                st.read_errors += 1
                st.connected = False
                return
            if chunk:
                buf += chunk

            # 버퍼에서 꺼낼 수 있는 패킷을 모두 처리한다
            while True:
                i = buf.find(MAGIC_LE)
                if i < 0:
                    # magic 없음 — 경계에 걸친 부분 magic(최대 3B)만 남기고 버린다
                    if len(buf) > 3:
                        st.resync_bytes += len(buf) - 3
                        del buf[:-3]
                    break
                if i > 0:
                    st.resync_bytes += i        # magic 앞의 쓰레기(부팅 로그 등)
                    del buf[:i]

                hdr = parse_header(buf, 0, default_fs_g=self.default_fs_g)
                if hdr is None:
                    break                       # 헤더가 아직 덜 들어옴
                if hdr.count == 0 or hdr.count > MAX_SAMPLES:
                    # 샘플에 우연히 나타난 가짜 magic — 4B 건너뛰고 재탐색
                    st.resync_bytes += 4
                    del buf[:4]
                    continue
                if len(buf) < hdr.total_bytes:
                    break                       # 페이로드가 아직 덜 들어옴

                payload = bytes(buf[hdr.size:hdr.total_bytes])
                del buf[:hdr.total_bytes]
                last_mg = _tail_mg(payload, hdr.sensitivity)

                seqt.update(hdr.seq)
                st.lost = seqt.lost
                st.packets += 1
                st.samples += hdr.count
                st.rate_step = hdr.rate_step
                st.full_scale_g = hdr.full_scale_g
                st.version = hdr.version
                st.last_seen = time.monotonic()
                win_samples += hdr.count

                if self.recording:
                    try:
                        self.queue.put_nowait((hdr, payload))
                    except Exception:
                        # 쓰기가 밀렸다. 버리되 반드시 센다 — 조용한 유실 금지.
                        st.queue_drops += 1

            # 1초 창으로 실효 레이트와 최근 샘플(mg) 갱신
            now = time.monotonic()
            if now - win_t0 >= 1.0:
                st.hz = win_samples / (now - win_t0)
                st.mg = last_mg
                win_samples = 0
                win_t0 = now


def target_hz(stats):
    """이 센서가 목표로 하는 샘플레이트 (rate_step 기준). 모르면 0."""
    return RATE_HZ.get(stats.rate_step, 0) if stats.rate_step is not None else 0
