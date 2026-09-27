#!/usr/bin/env python3
"""수집기 자체 검증 — 센서·GPIO 없이 돌아가는 부분을 전부 확인한다.

    ./selftest.py

실기기가 필요한 것(실제 엣지, 2대 동시 수집)은 여기서 다루지 않는다.
그쪽은 `trigger.py --watch` 와 `collect_cli.py` 로 확인한다.
"""

import contextlib
import io
import json
import queue
import struct
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import bin_to_csv
import collect_cli
import sensor_link
import session as session_mod
import slots
import trigger as trigger_mod
import writer as writer_mod
from iis3dwb_packet import HEADER_V1, HEADER_V2, MAGIC, parse_header

FAILS = []


def check(cond, msg):
    if not cond:
        FAILS.append(msg)


def group(name):
    print("• %s" % name)


# ---------------------------------------------------------------- 슬롯 키
group("슬롯 키 정규화")

check(slots.slot_key("platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.2:1.0")
      == "platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.2",
      "인터페이스 번호(:1.0)가 제거되지 않음")
# 커널이 같은 장치에 usb-/usbv2- 두 링크를 만들어도 한 슬롯으로 세어져야 한다
check(slots.slot_key("platform-x-usb-0:1.2:1.0")
      == slots.slot_key("platform-x-usbv2-0:1.2:1.0"),
      "usb/usbv2 가 같은 슬롯으로 정규화되지 않음")
check(slots.slot_key("platform-x-usb-0:1.3:1.0")
      != slots.slot_key("platform-x-usb-0:1.2:1.0"),
      "서로 다른 물리 포트가 같은 키로 뭉개짐")

# ---------------------------------------------------------------- 매핑 저장
group("슬롯 매핑 저장/조회")

with tempfile.TemporaryDirectory() as td:
    orig_dir, orig_path = slots.CONFIG_DIR, slots.SLOTS_PATH
    slots.CONFIG_DIR = Path(td) / "cfg"
    slots.SLOTS_PATH = slots.CONFIG_DIR / "slots.json"
    try:
        check(slots.load_mapping() == {}, "없는 파일이 빈 사전을 주지 않음")
        slots.save_mapping({"slot-a": "A", "slot-b": "B"})
        check(slots.load_mapping() == {"slot-a": "A", "slot-b": "B"},
              "저장한 매핑이 그대로 읽히지 않음")
        d = json.loads(slots.SLOTS_PATH.read_text(encoding="utf-8"))
        check(d.get("schema") == slots.SCHEMA, "schema 가 기록되지 않음")

        slots.SLOTS_PATH.write_text("{ 깨진 json", encoding="utf-8")
        check(slots.load_mapping() == {}, "깨진 파일에서 예외가 새어나감")
    finally:
        slots.CONFIG_DIR, slots.SLOTS_PATH = orig_dir, orig_path

# ---------------------------------------------------------------- Resolution
group("연결 상태 판정")


class _P:
    def __init__(self, slot, dev):
        self.slot, self.device, self.short_slot = slot, dev, slot
        self.chip, self.vid, self.pid = "test", 0, 0


def _resolution(ports, mapping):
    assigned, unknown, seen = [], [], set()
    for p in ports:
        n = mapping.get(p.slot)
        (assigned.append((n, p)), seen.add(n)) if n else unknown.append(p)
    return slots.Resolution(sorted(assigned, key=lambda t: t[0]), unknown,
                            sorted(v for v in mapping.values() if v not in seen))


r = _resolution([_P("s1", "/dev/ttyACM0")], {"s1": "A"})
check(r.ok and not r.unknown and not r.missing, "정상 1대가 ok 가 아님")

r = _resolution([_P("s1", "/dev/ttyACM0"), _P("s9", "/dev/ttyACM1")], {"s1": "A"})
check(not r.ok, "모르는 슬롯이 있는데 수집 가능으로 판정됨")
check(len(r.unknown) == 1, "모르는 슬롯 개수가 틀림")

r = _resolution([_P("s1", "/dev/ttyACM0")], {"s1": "A", "s2": "B"})
check(r.ok, "일부 미연결은 진행 가능해야 하는데 막힘")
check(r.missing == ["B"], "미연결 센서 목록이 틀림: %r" % r.missing)

r = _resolution([], {})
check(not r.ok, "센서가 없는데 수집 가능으로 판정됨")

# ---------------------------------------------------------------- 용량 추정
group("용량 추정")

b_csv = writer_mod.estimate_bytes("csv", 1, 26667, 1)
b_bin = writer_mod.estimate_bytes("bin", 1, 26667, 1)
# 실측: 30초 802,400샘플 = 56 MB → 1분 1대 약 112 MB
check(100e6 < b_csv < 125e6, "CSV 1분 추정이 실측(112MB)과 동떨어짐: %d" % b_csv)
# 실측: 20초 534,800샘플 = 3.2 MB → 1분 1대 약 9.7 MB
check(9e6 < b_bin < 11e6, "BIN 1분 추정이 실측(9.7MB)과 동떨어짐: %d" % b_bin)
check(writer_mod.estimate_bytes("csv", 1, 26667, 2) == 2 * b_csv,
      "센서 수가 용량에 비례하지 않음")
check(writer_mod.human_bytes(1536) == "2 KB", "human_bytes KB 표기 이상")
check(writer_mod.human_bytes(1 << 30) == "1.0 GB", "human_bytes GB 표기 이상")

# ---------------------------------------------------------------- 헤더 왕복
group("헤더 재조립 (바이너리 저장용)")

for ver, S in ((2, HEADER_V2), (1, HEADER_V1)):
    if ver >= 2:
        raw = S.pack(MAGIC, ver, 4, 200, 1234, 5678, 8, 0)
    else:
        raw = S.pack(MAGIC, ver, 3, 150, 99, 77)
    hdr = parse_header(raw, 0)
    check(writer_mod._rebuild_header(hdr) == raw,
          "v%d 헤더 재조립이 원본과 다름" % ver)

# ---------------------------------------------------------------- Writer
group("Writer")


def _mk(seq, n=4, fs=4, ver=2):
    vals = list(range(n * 3))
    payload = struct.pack("<%dh" % len(vals), *vals)
    raw = (HEADER_V2.pack(MAGIC, 2, 4, n, seq, 100 + seq, fs, 0) if ver >= 2
           else HEADER_V1.pack(MAGIC, 1, 4, n, seq, 100 + seq))
    return parse_header(raw + payload, 0), payload


with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    q = queue.Queue()
    pk = [_mk(i + 1) for i in range(3)]
    for p in pk:
        q.put(p)
    w = writer_mod.Writer(td / "a.csv", q, "csv")
    w.start()
    w.stop(drain_timeout=5)
    lines = (td / "a.csv").read_text(encoding="utf-8").splitlines()
    check(lines[0] + "\n" == writer_mod.CSV_HEADER, "CSV 헤더 줄이 다름")
    check(len(lines) == 1 + 3 * 4, "CSV 줄 수가 틀림: %d" % len(lines))
    check(w.written_samples == 12 and w.written_packets == 3,
          "Writer 집계가 틀림: %d/%d" % (w.written_samples, w.written_packets))
    check(w.error is None, "정상 기록인데 error 가 설정됨: %s" % w.error)

    q2 = queue.Queue()
    for p in pk:
        q2.put(p)
    w2 = writer_mod.Writer(td / "a.bin", q2, "bin", meta={"sensor": "A"})
    w2.start()
    w2.stop(drain_timeout=5)
    data = (td / "a.bin").read_bytes()
    check(len(data) == 3 * (18 + 4 * 6), "바이너리 크기가 틀림: %d" % len(data))
    side = json.loads((td / "a.bin.json").read_text(encoding="utf-8"))
    check(side["samples"] == 12 and side["sensor"] == "A",
          "사이드카 내용이 틀림: %r" % side)

    try:
        writer_mod.Writer(td / "x", queue.Queue(), "parquet")
        FAILS.append("알 수 없는 형식이 거부되지 않음")
    except ValueError:
        pass

# ---------------------------------------------------------------- 잘린 파일
group("잘린 바이너리 복구")

with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    q = queue.Queue()
    for i in range(5):
        q.put(_mk(i + 1))
    w = writer_mod.Writer(td / "t.bin", q, "bin")
    w.start()
    w.stop(drain_timeout=5)
    full = (td / "t.bin").read_bytes()

    # 마지막 패킷을 중간에서 자른다 (디스크가 차서 끊긴 상황)
    cut = td / "cut.bin"
    cut.write_bytes(full[:-10])
    got = list(bin_to_csv.iter_packets(cut.read_bytes()))
    check(len(got) == 4, "잘린 파일에서 온전한 패킷 4개를 못 읽음: %d" % len(got))
    # 42B 패킷(18+24)에서 10B 를 잘랐으니 남은 32B 가 건너뛴 바이트로 보고돼야 한다
    check(bin_to_csv.iter_packets.skipped == 32,
          "잘린 바이트 보고가 32가 아님: %s" % bin_to_csv.iter_packets.skipped)

    # 앞에 부팅 로그 같은 쓰레기가 붙은 경우
    pre = td / "pre.bin"
    pre.write_bytes(b"I (123) BOOT: hello\r\n" + full)
    got = list(bin_to_csv.iter_packets(pre.read_bytes()))
    check(len(got) == 5, "선두 쓰레기가 있는 파일에서 재동기 실패: %d" % len(got))

# ---------------------------------------------------------------- 트리거 로직
group("트리거 로직 (GPIO 없이)")

fired = []
t = trigger_mod.PhotoTrigger(gpio=5, on_trigger=lambda: fired.append(1))
t._on_edge(0, 5, 0, 0)
t._on_edge(0, 5, 0, 0)
check(t.edges == 2 and len(fired) == 2, "엣지 카운트/콜백이 맞지 않음")

# 콜백이 예외를 던져도 감시가 죽지 않아야 한다 (죽으면 이후 전부 놓친다)
bad = trigger_mod.PhotoTrigger(gpio=5, on_trigger=lambda: 1 / 0)
bad._on_edge(0, 5, 0, 0)
check(bad.edges == 1, "콜백 예외가 엣지 카운트를 막음")

check(trigger_mod.DIN_PINS[1] == 5, "DIN1 이 GPIO5 가 아님")
check(trigger_mod.DIN_PINS == {1: 5, 2: 17, 3: 27, 4: 22}, "DIN 핀맵이 틀림")

t2 = trigger_mod.PhotoTrigger(gpio=5, active_low=True)
check(t2.level() is None and t2.is_active() is None,
      "start() 하지 않은 상태에서 레벨이 None 이 아님")

check([session_mod.human_duration(m) for m in (5, 5 / 60, 1.5, 0.25)]
      == ["5분", "5초", "1분 30초", "15초"], "수집 길이 표시가 틀림")

group("GPIO 백엔드 (cdev · 하드웨어 없이)")

import gpio_cdev
# 커널 구조체 크기가 어긋나면 ioctl 이 엉뚱한 메모리를 읽는다 (32/64비트 공통이어야 함)
check(len(gpio_cdev._config(0, 0)) == 272, "gpio_v2_line_config 크기 ≠ 272")
check(len(gpio_cdev._config(0, 50000)) == 272, "디바운스 속성 포함 시 크기 ≠ 272")
check(gpio_cdev._GET_LINE == 0xC250B407, "GPIO_V2_GET_LINE_IOCTL 번호가 틀림")
check(gpio_cdev._SET_CONFIG == 0xC110B40D, "GPIO_V2_LINE_SET_CONFIG_IOCTL 번호가 틀림")
check(gpio_cdev._GET_VALUES == 0xC010B40E, "GPIO_V2_LINE_GET_VALUES_IOCTL 번호가 틀림")

# 커널이 v2 를 모르면 lgpio 로 물러서고, lgpio 도 없으면 이유를 담아 실패해야 한다
_real_cdev, _real_lgpio = trigger_mod._CdevHandle, trigger_mod.lgpio
def _unsupported(*a, **k):
    raise gpio_cdev.Unsupported(25, "v2 미지원")
trigger_mod._CdevHandle = _unsupported
trigger_mod.lgpio = None
t3 = trigger_mod.PhotoTrigger(gpio=5)
check(not t3.start() and "v2 미지원" in (t3.error or ""),
      "cdev·lgpio 모두 불가인데 이유가 남지 않음: %r" % t3.error)
trigger_mod._CdevHandle, trigger_mod.lgpio = _real_cdev, _real_lgpio

# ---------------------------------------------------------------- 세션
group("포토센서 배정 (1센서 : 1포토센서)")

with tempfile.TemporaryDirectory() as td:
    orig_dir, orig_path = slots.CONFIG_DIR, slots.SLOTS_PATH
    slots.CONFIG_DIR = Path(td) / "cfg"
    slots.SLOTS_PATH = slots.CONFIG_DIR / "slots.json"
    try:
        # 설정이 없으면 이름 순서대로 DIN1, DIN2 를 준다
        check(slots.load_din_map(["A", "B"]) == {"A": 1, "B": 2},
              "기본 DIN 배정이 A→1, B→2 가 아님: %r" % slots.load_din_map(["A", "B"]))
        check(slots.load_din_map(["B", "A"]) == {"A": 1, "B": 2},
              "이름 순서가 아니라 인자 순서를 따름")

        # 명시 배정은 보존되고, 남은 이름은 겹치지 않는 번호를 받는다
        slots.save_mapping({"s1": "A"}, {"A": 3})
        m = slots.load_din_map(["A", "B"])
        check(m["A"] == 3, "저장한 DIN 배정이 무시됨: %r" % m)
        check(m["B"] != 3, "다른 센서에 같은 DIN 이 배정됨: %r" % m)

        # DIN 만 갱신해도 슬롯 매핑이 살아 있어야 한다
        slots.save_din_map({"A": 2, "B": 1})
        check(slots.load_mapping() == {"s1": "A"},
              "DIN 저장이 슬롯 매핑을 지움: %r" % slots.load_mapping())
        check(slots.load_din_map() == {"A": 2, "B": 1}, "DIN 갱신이 반영되지 않음")
    finally:
        slots.CONFIG_DIR, slots.SLOTS_PATH = orig_dir, orig_path

group("채널 독립 동작")

with tempfile.TemporaryDirectory() as td:
    col = session_mod.Collector(td, minutes=1, fmt="csv")
    events = []
    col.set_event_handler(lambda k, m: events.append((k, m)))

    # 수집 중 트리거는 그 채널에서만 무시되고 세어져야 한다
    ch = session_mod.Channel.__new__(session_mod.Channel)
    ch.name, ch.din, ch.collector = "A", 1, col
    ch.state = session_mod.RECORDING
    ch.ignored_triggers = 0
    ch._lock = __import__("threading").Lock()
    ch._on_trigger()
    ch._on_trigger()
    check(ch.ignored_triggers == 2, "무시 횟수가 세어지지 않음: %d" % ch.ignored_triggers)
    check(sum(1 for k, m in events if "무시" in m) == 2,
          "무시 사실이 이벤트로 알려지지 않음")
    check(all("A" in m for k, m in events if "무시" in m),
          "어느 채널의 무시인지 메시지에 없음")

    # 자동 시작이 꺼져 있으면 트리거가 와도 수집하지 않는다
    col.auto_start = False
    ch.state = session_mod.IDLE
    events.clear()
    ch._on_trigger()
    check(any("자동 시작이 꺼져" in m for _, m in events),
          "자동 시작 꺼짐 안내가 없음: %r" % events)

group("용량 프리플라이트")

with tempfile.TemporaryDirectory() as td:
    col = session_mod.Collector(td, minutes=100000, fmt="csv")
    col.channels = {}
    try:
        col.check_space()
        FAILS.append("디스크 여유보다 큰 요청이 통과됨")
    except session_mod.PreflightError as e:
        check("공간이 부족" in str(e), "공간 부족 메시지가 아님: %s" % e)

    col.minutes = 1
    need = col.check_space()
    check(need > 0, "정상 요청의 예상 용량이 0")

group("run 번호 매기기")

with tempfile.TemporaryDirectory() as td:
    d = Path(td)
    check(session_mod._next_run_index(d) == 1, "빈 폴더의 첫 번호가 1이 아님")
    (d / "run_0001_A.csv").touch()
    (d / "run_0001.json").touch()
    check(session_mod._next_run_index(d) == 2, "기존 run 다음 번호가 틀림")
    (d / "run_0007_B.bin").touch()
    check(session_mod._next_run_index(d) == 8, "가장 큰 번호 다음이 아님")
    (d / "run_abcd_A.csv").touch()          # 이상한 이름이 섞여도 죽지 않아야 한다
    check(session_mod._next_run_index(d) == 8, "형식이 다른 이름에 흔들림")
    check(session_mod._next_run_index(d / "없는폴더") == 1,
          "없는 폴더에서 예외가 새어나감")
    # 센서별 폴더라 A 와 B 의 번호는 서로 영향을 주지 않아야 한다
    (d / "A").mkdir(); (d / "B").mkdir()
    (d / "A" / "run_0012.csv").touch()
    check(session_mod._next_run_index(d / "A") == 13, "A 폴더 번호가 틀림")
    check(session_mod._next_run_index(d / "B") == 1,
          "B 폴더가 A 의 번호에 영향을 받음")

group("빠진 센서를 조용히 넘기지 않는다")

# 2대 수집에서 한 대만 5분을 기록하고 판정이 '정상' 으로 나온 적이 있다.
# 원인 두 가지를 각각 막는다.
# preflight 는 실제 USB 슬롯 상태(slots.resolve)를 본다. 슬롯을 지정하기 전인
# 새 RPi 에 센서가 꽂혀 있으면 모든 채널이 "이름 없는 슬롯" 으로 거부돼 이 검사가
# 헛되이 실패한다. 여기서는 채널 선택 논리만 보므로 슬롯은 모두 지정된 것으로 둔다.
_real_resolve = slots.resolve
slots.resolve = lambda mapping=None: slots.Resolution([], [], [])
with tempfile.TemporaryDirectory() as td:
    col = session_mod.Collector(td, minutes=1, fmt="csv")

    class _FakeLink:
        def __init__(self, receiving):
            self.stats = sensor_link.LinkStats()
            self.stats.connected = True
            self.stats.last_seen = time.monotonic() if receiving else 0.0

    def _fake_ch(name, receiving):
        ch = session_mod.Channel.__new__(session_mod.Channel)
        ch.name, ch.din, ch.collector = name, 1, col
        ch.state = session_mod.IDLE
        ch.link = _FakeLink(receiving)
        ch.started = False
        ch.start = lambda source="수동", _c=ch: (setattr(_c, "started", True), True)[1]
        return ch

    # ① 첫 패킷이 늦은 센서도 대상에 들어가야 한다 — 빠지면 사유가 남는다
    col.channels = {"1": _fake_ch("1", True), "2": _fake_ch("2", False)}
    events = []
    col.set_event_handler(lambda k, m: events.append((k, m)))
    started = col.start_manual()
    check(started == ["1"], "데이터가 없는 채널이 시작됨: %r" % started)
    check(any(k == "error" and "2" in m for k, m in events),
          "빠진 센서 2의 사유가 남지 않음: %r" % events)

    # ② 둘 다 들어오면 둘 다 시작한다
    col.channels = {"1": _fake_ch("1", True), "2": _fake_ch("2", True)}
    check(sorted(col.start_manual()) == ["1", "2"],
          "데이터가 들어오는 2채널이 모두 시작되지 않음")
slots.resolve = _real_resolve

# ④ 이름 없는 슬롯 안내가 모두 같은 이름('A')을 제안해 따라 하면 덮어썼다
class _P:
    def __init__(self, dev): self.device = dev
_sug = slots.suggest_names(slots.Resolution(
    [("1", _P("/dev/ttyACM9"))], [_P("/dev/ttyACM0"), _P("/dev/ttyACM1")], ["3"]))
check(sorted(_sug.values()) == ["2", "4"],
      "이름 제안이 겹치거나 이미 쓰인 이름을 줌: %r" % _sug)

# ③ 표에 줄이 하나뿐인데 '정상' 이 나오면 안 된다
res = session_mod.RunResult(
    sensor="1", index=1, file="run_0001.csv", samples=100, packets=1,
    loss_pct=0.0, queue_drops=0, bytes=100, stop_reason="시간 완료",
    ok=True, note=None)
_buf = io.StringIO()
with contextlib.redirect_stdout(_buf):
    rc = collect_cli.summarize([res], missing=["2"])
check(rc != 0, "수집 못 한 센서가 있는데 종료코드가 0")
check("2(수집 못 함)" in _buf.getvalue(),
      "판정에 빠진 센서가 적히지 않음: %r" % _buf.getvalue())

# ---------------------------------------------------------------- 공통 설정
group("공통 설정 저장/조회 (GUI ↔ 글자 화면 공유)")
import settings as settings_mod
with tempfile.TemporaryDirectory() as td:
    _orig = settings_mod.SETTINGS_PATH
    settings_mod.SETTINGS_PATH = Path(td) / "settings.json"
    try:
        check(settings_mod.load() == settings_mod.DEFAULTS, "설정 파일이 없을 때 기본값이 아님")
        settings_mod.save({"minutes": 0.5, "fmt": "bin", "모르는키": 1})
        d = settings_mod.load()
        check(d["minutes"] == 0.5 and d["fmt"] == "bin", "저장한 설정이 읽히지 않음: %r" % d)
        check("모르는키" not in json.loads(settings_mod.SETTINGS_PATH.read_text()),
              "모르는 키가 저장됨")
        settings_mod.save({"active_low": False})
        check(settings_mod.load()["minutes"] == 0.5, "다른 항목 저장이 기존 값을 지움")
        # 손으로 고쳐 깨진 값 — 그 항목만 기본값으로 (수집 시간 0·음수는 막는다)
        settings_mod.SETTINGS_PATH.write_text(
            json.dumps({"minutes": -3, "fmt": "xls", "active_low": "yes"}))
        d = settings_mod.load()
        check(d["minutes"] == settings_mod.DEFAULTS["minutes"] and d["fmt"] == "csv"
              and d["active_low"] is True, "잘못된 값을 걸러내지 못함: %r" % d)
        settings_mod.SETTINGS_PATH.write_text("{ 깨진 json")
        check(settings_mod.load() == settings_mod.DEFAULTS, "깨진 파일에서 기본값으로 돌아가지 않음")
    finally:
        settings_mod.SETTINGS_PATH = _orig

# 글자 화면의 기본값이 저장된 설정을 따르는지 (GUI 에서 바꾼 수집 시간이 Lite 에서도 적용)
with tempfile.TemporaryDirectory() as td:
    _orig = settings_mod.SETTINGS_PATH
    settings_mod.SETTINGS_PATH = Path(td) / "settings.json"
    try:
        settings_mod.save({"minutes": 2.5})
        _seen = {}
        _orig_resolve = slots.resolve
        slots.resolve = lambda mapping=None: slots.Resolution([], [], [])
        try:
            _orig_cmd = collect_cli.cmd_status
            collect_cli.cmd_status = lambda: 0
            with contextlib.redirect_stdout(io.StringIO()):
                import argparse
                _orig_parse = argparse.ArgumentParser.parse_args
                def _spy(self, argv=None, ns=None):
                    r = _orig_parse(self, argv, ns); _seen["m"] = r.minutes; return r
                argparse.ArgumentParser.parse_args = _spy
                try:
                    collect_cli.main(["--status"])
                finally:
                    argparse.ArgumentParser.parse_args = _orig_parse
            collect_cli.cmd_status = _orig_cmd
        finally:
            slots.resolve = _orig_resolve
        check(_seen.get("m") == 2.5, "글자 화면이 저장된 수집 시간을 쓰지 않음: %r" % _seen)
    finally:
        settings_mod.SETTINGS_PATH = _orig

# ---------------------------------------------------------------- WiFi 센서
group("WiFi 센서 (UDP 수신 · 매핑 · 포토센서 기본값)")
with tempfile.TemporaryDirectory() as td:
    orig_dir, orig_path = slots.CONFIG_DIR, slots.SLOTS_PATH
    slots.CONFIG_DIR = Path(td)
    slots.SLOTS_PATH = slots.CONFIG_DIR / "slots.json"
    try:
        slots.save_wifi_map({"2": 9001})
        slots.save_din_map({"1": 1})            # 다른 저장이 WiFi 목록을 지우면 안 된다
        check(slots.load_wifi_map() == {"2": 9001}, "포토센서 저장이 WiFi 목록을 지움")
        slots.save_mapping({"slot-a": "1"})
        check(slots.load_wifi_map() == {"2": 9001}, "슬롯 저장이 WiFi 목록을 지움")
        # WiFi 센서 2 만 있어도 DIN2 를 봐야 한다 (예전에는 DIN1)
        check(slots.load_din_map(["2"]) == {"2": 2}, "센서 2 의 기본 포토센서가 DIN2 가 아님")
    finally:
        slots.CONFIG_DIR, slots.SLOTS_PATH = orig_dir, orig_path

import socket as _socket
_q = queue.Queue()
_probe = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM); _probe.bind(("127.0.0.1", 0))
_port = _probe.getsockname()[1]; _probe.close()
_ul = sensor_link.UdpLink("W", _port, _q)
_ul.open(); _ul.start()
_tx = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
_pay = struct.pack("<3h", 0, 0, 8197) * 200             # ±4g 에서 Z ≈ 1000 mg
for _seq in (1, 2, 4):                                  # 3 이 빠짐 → 유실 1
    _tx.sendto(HEADER_V2.pack(MAGIC, 2, 4, 200, _seq, _seq, 4, 0) + _pay, ("127.0.0.1", _port))
_tx.sendto(b"not a packet", ("127.0.0.1", _port))
_ul.recording = True
_tx.sendto(HEADER_V2.pack(MAGIC, 2, 4, 200, 5, 5, 4, 0) + _pay, ("127.0.0.1", _port))
time.sleep(0.5)
_ul.stop(); _tx.close()
check(_ul.stats.packets == 4, "UDP 패킷 수가 다름: %d" % _ul.stats.packets)
check(_ul.stats.lost == 1, "UDP seq 끊김을 유실로 세지 않음: %d" % _ul.stats.lost)
check(_ul.stats.resync_bytes == len(b"not a packet"), "형식이 아닌 데이터그램을 세지 않음")
check(_ul.peer == "127.0.0.1", "보낸 곳 IP 를 기록하지 않음")
check(_q.qsize() == 1, "recording 일 때만 큐에 넣어야 함: %d" % _q.qsize())
check(_ul.port == "WiFi :%d" % _port, "WiFi 표기가 다름")

# 수집기 화면은 tkinter 가 있을 때만 import 한다 (Lite 이미지에는 없다)
try:
    import tkinter  # noqa: F401
    _has_tk = True
except ImportError:
    _has_tk = False
if _has_tk:
    import collector_gui
    check(collector_gui.APP_NAME == "SHT 진동센서 수집", "수집기 공식 명칭이 다름")

# ---------------------------------------------------------------- 결과
print("")
if FAILS:
    print("FAIL — %d건" % len(FAILS))
    for f in FAILS:
        print("  ·", f)
    sys.exit(1)
print("PASS — 모든 항목 통과")
