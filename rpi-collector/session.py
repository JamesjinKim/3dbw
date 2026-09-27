#!/usr/bin/env python3
"""수집 채널 — 진동센서 1대 + 포토센서 1개가 한 짝으로 독립 동작한다.

    진동센서 A ─ 포토센서 A(DIN1) ─ 감지 → A만 N분 수집 → A 저장 → A 대기
    진동센서 B ─ 포토센서 B(DIN2) ─ 감지 → B만 N분 수집 → B 저장 → B 대기
                                        두 짝은 완전히 독립이다.

한쪽이 수집 중일 때 다른 쪽은 대기일 수 있고, 두 쪽이 겹쳐 수집할 수도 있다.
그래서 상태·타이머·run 번호·파일을 **채널마다 따로** 둔다.

    <저장폴더>/20260923/A/run_0012.csv   + run_0012.json
    <저장폴더>/20260923/B/run_0007.csv   + run_0007.json

번호가 센서마다 어긋나는 것이 정상이다 — 서로 다른 시각에 서로 다른 횟수로
수집하기 때문이다. 센서별 폴더로 나누면 한 센서의 이력이 연속된 번호로 모인다.

## 공통과 개별

| 항목 | 범위 |
|---|---|
| 수집 길이(분)·저장 형식·저장 폴더 | **공통** (Collector 가 보관) |
| 트리거·상태·타이머·run 번호·파일·통계 | **채널별** |

## 판단 근거

**모르는 슬롯이 있으면 어느 채널도 시작하지 않는다.** 어느 센서인지 모르는
데이터를 'A' 로 기록하면 되돌릴 수 없다.

**수집 중 들어온 추가 트리거는 그 채널에서만 무시하고 센다.** 컨베이어가 연속으로
지나가면 엣지가 여러 번 들어오는데, 진행 중인 수집을 끊거나 겹쳐 시작하면 파일이
깨진다. 무시 횟수를 남겨 "수집 길이가 라인 주기보다 길다" 는 것을 알게 한다.
"""

import json
import os
import queue
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

import sensor_link
import slots
import trigger as trigger_mod
import writer as writer_mod
from iis3dwb_packet import RATE_HZ

# 링버퍼 깊이 — 패킷 단위. 200샘플/패킷이므로 26.6kHz 에서 1초는 약 133패킷.
# 512 면 약 4초를 버틴다. 이보다 키워도 GC 부담만 늘고 도움이 안 된다.
QUEUE_MAXSIZE = 512
# 패킷 없이 이만큼(B/s) 쓰레기가 계속 들어오면 보드레이트 불일치로 본다.
# 1 kHz 스트림을 틀린 속도로 읽으면 약 7.8KB/s 가 들어왔다.
GARBLE_BPS = 1000

IDLE = "idle"
RECORDING = "recording"
FINISHING = "finishing"


class PreflightError(Exception):
    """수집을 시작하면 안 되는 이유. 메시지를 그대로 사용자에게 보인다."""


class RunResult:
    """끝난 수집 하나의 요약."""

    __slots__ = ("sensor", "index", "dir", "file", "started", "ended",
                 "minutes", "fmt", "samples", "packets", "loss_pct",
                 "queue_drops", "bytes", "stop_reason", "ok", "note")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))


# ===================== 채널 =====================

class Channel:
    """진동센서 1대 + 포토센서 1개. 자기 트리거만 바라보고 독립 동작한다."""

    def __init__(self, name, port_device, din, collector, udp_port=None):
        self.name = name
        self.din = din
        self.collector = collector
        self.udp_port = udp_port        # None 이면 USB, 숫자면 WiFi(UDP) 수신 포트

        self.queue = queue.Queue(maxsize=QUEUE_MAXSIZE)
        if udp_port is None:
            self.link = sensor_link.SensorLink(name, port_device, self.queue,
                                               baud=collector.baud)
        else:
            self.link = sensor_link.UdpLink(name, udp_port, self.queue)
        self.port = self.link.port      # '/dev/ttyACM0' 또는 'WiFi :9001'
        self.trigger = trigger_mod.PhotoTrigger(
            din=din, active_low=collector.active_low,
            on_trigger=self._on_trigger)

        # garbled 판정용: (측정 시작 시각, 그때의 resync_bytes), 최근 쓰레기 유입률(B/s)
        self._garble_ref = (time.monotonic(), 0)
        self._garble_rate = 0.0

        self.state = IDLE
        self.run_index = None
        self.run_dir = None
        self.writer = None
        self.started_at = None
        self.started_iso = None
        self.duration_s = 0
        self.fmt = "csv"
        self.ignored_triggers = 0
        self.last_result = None

        self._lock = threading.Lock()
        self._timer = None

    # ---------- 수명 ----------
    def open(self):
        """시리얼과 GPIO 를 연다. 시리얼 실패는 예외, GPIO 실패는 경고."""
        self.link.open()
        self.link.start()
        if not self.trigger.start():
            # GPIO 를 못 써도 수동 시작으로 계속 쓸 수 있어야 한다.
            self.collector._emit("warn", "%s 포토센서(DIN%d) 사용 불가 — 수동 시작만 "
                                         "가능합니다\n%s"
                                 % (self.name, self.din, self.trigger.error))

    def set_din(self, din):
        """이 센서가 바라볼 포토센서를 바꾼다. 감시를 다시 건다. 성공하면 True.

        배선에 맞춰 GUI 에서 고른다. 틀리면 1번을 지나간 제품이 2번으로 기록되므로,
        바뀐 값은 화면의 실시간 레벨로 바로 확인할 수 있어야 한다.
        """
        self.trigger.stop()
        self.din = din
        self.trigger = trigger_mod.PhotoTrigger(
            din=din, active_low=self.collector.active_low,
            on_trigger=self._on_trigger)
        if not self.trigger.start():
            self.collector._emit("warn", "%s 포토센서(DIN%d) 사용 불가: %s"
                                 % (self.name, din, self.trigger.error))
            return False
        return True

    def close(self):
        if self.state == RECORDING:
            self.stop(reason="프로그램 종료")
        self.trigger.stop()
        self.link.stop()

    # ---------- 상태 ----------
    @property
    def receiving(self):
        """지금 데이터가 들어오고 있는가."""
        st = self.link.stats
        return bool(st.connected and st.last_seen
                    and (time.monotonic() - st.last_seen) < 3.0)

    @property
    def garbled(self):
        """바이트는 들어오는데 패킷이 아니다 — 보드레이트 불일치.

        ○(데이터 없음)과 같은 모양으로 보이면 '케이블·전원 확인' 으로 헤매게 된다.
        실제로는 개발용 ./run.sh 로 구운 보드(921600 bps)를 2 Mbps 로 읽고 있었다
        (2026-09-27). 부팅 로그(약 3.5KB, 한 번)는 문턱을 넘지 않도록 초당 유입률로 본다.
        """
        st = self.link.stats
        now = time.monotonic()
        t0, r0 = self._garble_ref
        if now - t0 >= 3.0:
            self._garble_rate = (st.resync_bytes - r0) / (now - t0)
            self._garble_ref = (now, st.resync_bytes)
        return (not self.receiving) and self._garble_rate > GARBLE_BPS

    @property
    def elapsed(self):
        if self.state == IDLE or self.started_at is None:
            return 0.0
        return min(time.monotonic() - self.started_at, self.duration_s)

    @property
    def progress(self):
        return (self.elapsed / self.duration_s) if self.duration_s else 0.0

    # ---------- 트리거 ----------
    def _on_trigger(self):
        """**GPIO 감시 스레드에서 호출된다.** 위젯을 만지면 안 된다."""
        with self._lock:
            if self.state != IDLE:
                self.ignored_triggers += 1
                self.collector._emit(
                    "warn", "%s 수집 중 트리거 무시 (%d회째) — 수집 길이가 라인 "
                            "주기보다 깁니다" % (self.name, self.ignored_triggers))
                return
        if not self.collector.auto_start:
            self.collector._emit("info", "%s 포토센서 감지 — 자동 시작이 꺼져 있어 "
                                         "기록하지 않았습니다" % self.name)
            return
        try:
            self.collector.preflight(self)
        except PreflightError as e:
            self.collector._emit("error", "%s 시작 불가:\n%s" % (self.name, e))
            return
        self.start(source="포토센서 DIN%d" % self.din)

    # ---------- 시작 ----------
    def start(self, *, source="수동"):
        """수집 시작. Collector.preflight() 를 통과한 뒤에만 부를 것."""
        with self._lock:
            if self.state != IDLE:
                return False
            self.state = RECORDING

        c = self.collector
        day = datetime.now().strftime("%Y%m%d")
        self.run_dir = c.out_dir / day / self.name
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.run_index = _next_run_index(self.run_dir)

        self.fmt = c.fmt
        self.duration_s = c.minutes * 60
        self.started_at = time.monotonic()
        self.started_iso = datetime.now().isoformat(timespec="seconds")
        self.ignored_triggers = 0

        self.link.flush()                   # OS 버퍼의 과거 데이터를 버린다
        self.link.stats.queue_drops = 0
        self._base_packets = self.link.stats.packets
        self._base_lost = self.link.stats.lost
        self._base_resync = self.link.stats.resync_bytes

        ext = "csv" if self.fmt == "csv" else "bin"
        path = self.run_dir / ("run_%04d.%s" % (self.run_index, ext))
        # sidecar=False: 이 채널이 run_NNNN.json 에 모든 정보를 남기므로
        # Writer 가 따로 .bin.json 을 만들 필요가 없다 (json 두 개는 혼란만 준다).
        self.writer = writer_mod.Writer(path, self.queue, self.fmt, sidecar=False)
        self.writer.start()
        self.link.recording = True

        c._emit("started", "%s run_%04d 시작 — %s · %s · %s"
                % (self.name, self.run_index, source, human_duration(c.minutes),
                   self.fmt.upper()))

        # 시간이 되면 스스로 끝난다. 사용자가 화면을 보지 않아도 저장된다.
        self._timer = threading.Timer(self.duration_s,
                                      lambda: self.stop(reason="시간 완료"))
        self._timer.daemon = True
        self._timer.start()
        return True

    # ---------- 종료 ----------
    def stop(self, reason="중지"):
        with self._lock:
            if self.state != RECORDING:
                return None
            self.state = FINISHING

        if self._timer:
            self._timer.cancel()
            self._timer = None

        # 읽기 스레드가 더 이상 큐에 넣지 않게 먼저 끈다. 그래야 쓰기 스레드가
        # 남은 것만 비우고 끝난다 (끄지 않으면 새 패킷이 계속 들어온다).
        self.link.recording = False

        # **수집 구간의 길이는 여기서 잰다.** 아래 writer.stop() 은 큐를 비우는 데
        # 수 초를 쓰므로, 그 뒤에 재면 실제보다 길게 기록된다.
        elapsed = time.monotonic() - (self.started_at or time.monotonic())

        w = self.writer
        w.stop()
        self.writer = None

        st = self.link.stats
        lost = st.lost - self._base_lost
        pkts = st.packets - self._base_packets
        loss_pct = (100.0 * lost / (pkts + lost)) if (pkts + lost) else 0.0
        size = os.path.getsize(w.path) if os.path.exists(w.path) else 0

        # 기대 샘플 수와 대조한다. **메타를 쓰기 전에** 구해야 파일에 남는다.
        #
        # 유실·드롭만 보면 **한 개도 못 받은 수집이 '정상' 으로 보고된다** —
        # 실제로 겪었다(0샘플·70B 파일에 "판정: 정상"). 센서가 늦게 부팅했거나
        # 케이블이 빠졌을 때가 그렇다. 받은 양 자체를 판정에 넣어야 드러난다.
        expect = int((RATE_HZ.get(st.rate_step) or 0) * elapsed)
        shortfall = None
        if w.written_samples == 0:
            shortfall = "수집된 샘플이 없습니다 (센서에서 데이터가 오지 않았습니다)"
        elif expect and w.written_samples < expect * 0.9:
            shortfall = ("기대 %s샘플 중 %s만 기록됨 (%.0f%%)"
                         % ("{:,}".format(expect), "{:,}".format(w.written_samples),
                            100.0 * w.written_samples / expect))

        meta = {
            "sensor": self.name,
            "run": self.run_index,
            "file": os.path.basename(w.path),
            "format": self.fmt,
            "started": self.started_iso,
            "ended": datetime.now().isoformat(timespec="seconds"),
            "requested_seconds": self.duration_s,
            "actual_seconds": round(elapsed, 1),
            "stop_reason": reason,
            "ignored_triggers": self.ignored_triggers,
            # 아래는 .bin 을 읽는 데 필요한 정보 — bin_to_csv.py 가 참조한다
            "packets": w.written_packets,
            "samples": w.written_samples,
            "rate_step": st.rate_step,
            "rate_hz": RATE_HZ.get(st.rate_step),
            "full_scale_g": st.full_scale_g,
            "proto_version": st.version,
            "lost_packets": lost,
            "loss_pct": round(loss_pct, 4),
            "queue_drops": st.queue_drops,
            "resync_bytes": st.resync_bytes - self._base_resync,
            "bytes": size,
            "port": self.port,
            "source": "wifi" if self.udp_port else "usb",
            "baud": None if self.udp_port else self.collector.baud,
            "photo_din": self.din,
            "photo_gpio": self.trigger.gpio,
            "photo_active_low": self.trigger.active_low,
            "write_error": w.error,
            "shortfall": shortfall,
        }
        try:
            (self.run_dir / ("run_%04d.json" % self.run_index)).write_text(
                json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")
        except Exception as e:
            self.collector._emit("error", "%s 메타데이터 저장 실패: %s"
                                 % (self.name, e))

        bad = bool(lost or st.queue_drops or w.error or shortfall)
        result = RunResult(
            sensor=self.name, index=self.run_index, dir=str(self.run_dir),
            file=os.path.basename(w.path), started=self.started_iso,
            ended=meta["ended"], minutes=self.duration_s / 60.0, fmt=self.fmt,
            samples=w.written_samples, packets=w.written_packets,
            loss_pct=loss_pct, queue_drops=st.queue_drops, bytes=size,
            stop_reason=reason, ok=not bad,
            note=(shortfall or w.error
                  or ("유실 %.2f%%" % loss_pct if lost else
                      ("큐 드롭 %d" % st.queue_drops if st.queue_drops else ""))))
        self.last_result = result
        self.collector._record(result)

        self.state = IDLE
        if bad:
            self.collector._emit("warn", "%s run_%04d 완료 — %s"
                                 % (self.name, self.run_index, result.note))
        else:
            self.collector._emit("finished", "%s run_%04d 완료 — %s샘플 · %s"
                                 % (self.name, self.run_index,
                                    "{:,}".format(w.written_samples),
                                    writer_mod.human_bytes(size)))
        return result


# ===================== 수집기 =====================

class Collector:
    """채널들을 소유하고 공통 설정을 보관한다. 프로그램에 하나만 만든다."""

    def __init__(self, out_dir, *, minutes=5, fmt="csv", auto_start=True,
                 active_low=True, baud=sensor_link.DEFAULT_BAUD):
        self.out_dir = Path(out_dir)
        self.minutes = minutes
        self.fmt = fmt
        self.auto_start = auto_start
        self.active_low = active_low
        self.baud = baud

        self.channels = {}          # 이름 → Channel
        self.history = []           # 최근 RunResult (새 것이 앞)
        self._on_event = None
        self._hist_lock = threading.Lock()

    # ---------- 이벤트 ----------
    def set_event_handler(self, fn):
        """(kind, message) 콜백. kind: info|warn|error|started|finished

        **워커·GPIO 콜백 스레드에서 호출된다.** GUI 는 여기서 위젯을 만지지 말고
        큐에 넣기만 해야 한다.
        """
        self._on_event = fn

    def _emit(self, kind, msg):
        if self._on_event:
            try:
                self._on_event(kind, msg)
            except Exception:
                pass

    def _record(self, result):
        with self._hist_lock:
            self.history.insert(0, result)
            del self.history[40:]

    # ---------- 채널 열기 ----------
    def open(self):
        """매핑된 센서마다 채널을 만들어 연다. Resolution 을 돌려준다."""
        self.close()
        res = slots.resolve()
        wifi = slots.load_wifi_map()
        din_map = slots.load_din_map(sorted(set(res.names) | set(wifi)))
        for name, port in res.assigned:
            if name in wifi:
                # WiFi 로 받는 센서 — USB 는 전원용으로 꽂혀 있을 뿐 데이터가 오지 않는다
                continue
            # open_path: udev 고정 이름(/dev/iis3dwb1)이 있으면 그쪽을 연다.
            # /dev/ttyACM 번호는 재부팅·재연결로 바뀌지만 고정 이름은 그대로다.
            ch = Channel(name, port.open_path, din_map.get(name, 1), self)
            try:
                ch.open()
            except Exception as e:
                self._emit("error", "%s (%s) 열기 실패: %s" % (name, port.device, e))
                continue
            self.channels[name] = ch
        for name, udp_port in sorted(wifi.items()):
            ch = Channel(name, None, din_map.get(name, 1), self, udp_port=udp_port)
            try:
                ch.open()
            except Exception as e:
                self._emit("error", "%s (WiFi 포트 %d) 열기 실패: %s — 다른 프로그램이 "
                                    "같은 포트를 쓰고 있지 않은지 확인하세요"
                           % (name, udp_port, e))
                continue
            self.channels[name] = ch
        # WiFi 센서는 USB 매핑이 없어도 되므로 '연결 안 됨' 목록에서 뺀다
        res.missing = [n for n in res.missing if n not in wifi]

        # 두 상황을 구분해 알린다.
        #
        #  · 일부만 연결 — **정상 운영이다.** 센서 1대만 꽂고 쓰는 경우가 실제로
        #    있으므로 경고로 다루지 않는다. 사실만 조용히 알린다.
        #  · 이름이 지정되지 않은 슬롯 — 이것만 진짜 문제다. 어느 센서인지 모르는
        #    데이터를 'A' 로 기록하면 되돌릴 수 없어 수집 자체를 막는다.
        if res.missing:
            self._emit("info", "센서 %d대(%s)가 인식되었습니다. 이대로 수집할 수 "
                               "있습니다. (%s 는 연결되지 않음)"
                       % (len(self.channels), "+".join(sorted(self.channels)),
                          ", ".join(res.missing)))
        if res.unknown:
            self._emit("warn", "이름이 지정되지 않은 USB 슬롯 %d개 — 이름을 "
                               "지정해야 수집할 수 있습니다" % len(res.unknown))
        return res

    def close(self):
        for ch in self.channels.values():
            ch.close()
        self.channels.clear()

    @property
    def receiving_names(self):
        return [n for n, ch in self.channels.items() if ch.receiving]

    @property
    def any_recording(self):
        return any(ch.state != IDLE for ch in self.channels.values())

    # ---------- 프리플라이트 ----------
    def preflight(self, channel):
        """이 채널이 지금 수집을 시작해도 되는지. 통과하면 예상 용량(B) 반환."""
        if channel.state != IDLE:
            raise PreflightError("이미 수집이 진행 중입니다.")

        res = slots.resolve()
        if res.unknown:
            raise PreflightError(
                "이름이 지정되지 않은 USB 슬롯이 있습니다 (%s).\n"
                "어느 센서인지 모르는 데이터를 기록하지 않기 위해 시작하지 않습니다.\n"
                "'슬롯 지정' 으로 이름을 먼저 정하세요."
                % ", ".join(p.short_slot for p in res.unknown))

        if not channel.receiving:
            if getattr(channel, "udp_port", None):
                ips = sensor_link.local_ips()
                raise PreflightError(
                    "%s 에서 데이터가 들어오지 않습니다 (WiFi 포트 %d).\n"
                    "· 센서 설정의 라즈베리파이 IP 가 이 라즈베리파이(%s)인지\n"
                    "· 센서 설정의 수신 포트가 %d 인지, 센서가 같은 WiFi 에 붙었는지 확인하세요"
                    % (channel.name, channel.udp_port, ", ".join(ips) or "?",
                       channel.udp_port))
            raise PreflightError(
                "%s 에서 데이터가 들어오지 않습니다.\n"
                "· 센서 설정이 USB 직결인지 확인하세요 — WiFi 로 설정한 센서라면\n"
                "  수집기 화면에서 이 센서의 수신을 WiFi 로 바꾸세요\n"
                "· 보드레이트가 %d 로 맞는지 확인하세요"
                % (channel.name, self.baud))

        if self.minutes <= 0:
            raise PreflightError("수집 길이는 0분보다 커야 합니다.")

        return self.check_space()

    def check_space(self):
        """지금 설정으로 수집할 때 필요한 용량을 확인한다.

        **모든 채널이 동시에 수집하는 최악의 경우**로 계산한다. 채널이 독립이라
        겹칠 수 있고, 그때 디스크가 차면 두 파일이 함께 깨진다.
        """
        self.out_dir.mkdir(parents=True, exist_ok=True)
        names = self.receiving_names or list(self.channels)
        n = max(1, len(names))
        hz = max((RATE_HZ.get(self.channels[x].link.stats.rate_step, 26667)
                  for x in names), default=26667)
        need = writer_mod.estimate_bytes(self.fmt, self.minutes, hz, n)
        free = shutil.disk_usage(self.out_dir).free
        # 여유를 조금 남긴다 — 디스크를 정확히 0 으로 채우면 OS 가 불안정해진다
        if need > free * 0.9:
            raise PreflightError(
                "저장 공간이 부족합니다.\n"
                "필요 %s · 여유 %s (%s × %d대 × %s)\n"
                "수집 길이를 줄이거나 바이너리 형식을 쓰세요 (용량 1/10)."
                % (writer_mod.human_bytes(need), writer_mod.human_bytes(free),
                   human_duration(self.minutes), n, self.fmt.upper()))
        return need

    # ---------- 수동 조작 ----------
    def start_manual(self, names=None):
        """수동 시작. 이름을 주지 않으면 **열려 있는 모든 채널**.

        예전에는 `receiving_names`(지금 데이터가 들어오는 채널)를 기본값으로 삼았다.
        그러면 첫 패킷이 몇백 ms 늦은 센서가 **아무 말 없이 수집에서 빠진다.**
        실제로 2대 수집에서 한 대만 5분을 기록하고 판정은 '정상' 이 나왔다.

        대신 모든 채널을 대상으로 두고 `preflight` 가 거르게 한다. 데이터가 안
        들어오는 채널은 "시작 불가" 사유가 화면에 남으므로 놓칠 수 없다.
        """
        targets = names if names else list(self.channels)
        started = []
        for n in targets:
            ch = self.channels.get(n)
            if ch is None:
                continue
            try:
                self.preflight(ch)
            except PreflightError as e:
                self._emit("error", "%s 시작 불가:\n%s" % (n, e))
                continue
            if ch.start(source="수동"):
                started.append(n)
        return started

    def stop_all(self, reason="사용자 중지"):
        return [ch.stop(reason=reason) for ch in self.channels.values()
                if ch.state == RECORDING]

    def set_active_low(self, active_low):
        """트리거 활성 레벨 변경 — 감시를 다시 걸어야 반영된다."""
        self.active_low = active_low
        for ch in self.channels.values():
            ch.trigger.stop()
            ch.trigger.active_low = active_low
            if not ch.trigger.start():
                self._emit("warn", "%s 포토센서 재설정 실패: %s"
                           % (ch.name, ch.trigger.error))


def human_duration(minutes):
    """수집 길이(분)를 사람이 읽는 문자열로. 5 → '5분', 1/12 → '5초', 1.5 → '1분 30초'.

    --seconds 로 짧게 줄 때 '%g분' 으로 찍으면 '0.0833333분' 이 된다.
    """
    s = int(round(minutes * 60))
    if s < 60:
        return "%d초" % s
    m, s = divmod(s, 60)
    return "%d분" % m if s == 0 else "%d분 %d초" % (m, s)


def _next_run_index(sensor_dir):
    """센서 폴더에서 다음 run 번호. 센서마다 독립이며 날짜별로 1부터 시작한다."""
    mx = 0
    try:
        for p in sensor_dir.iterdir():
            if p.name.startswith("run_"):
                try:
                    mx = max(mx, int(p.name[4:8]))
                except ValueError:
                    pass
    except OSError:
        pass
    return mx + 1
