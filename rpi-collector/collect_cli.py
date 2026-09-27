#!/usr/bin/env python3
"""수집 CLI — GUI 없이 같은 엔진(session.py)을 돌린다.

진동센서 1대마다 포토센서 1개가 짝을 이루며, 두 짝은 **독립적으로** 수집한다.

    ./collect_cli.py --status                 연결·슬롯·포토센서 배정 확인
    ./collect_cli.py --watch                  기록 없이 실시간 상태만 표시
    ./collect_cli.py --auto --minutes 5       포토센서를 기다려 자동 수집 (상시 운전)
    ./collect_cli.py --manual --seconds 30    지금 바로 전 채널 수집 (시험용)
    ./collect_cli.py --manual --only A --seconds 30

종료 코드: 0=정상, 1=유실/드롭 있음, 2=시작 거부(프리플라이트), 3=실행 오류
"""

import argparse
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import sensor_link
import session as session_mod
import settings as settings_mod
import slots
import writer as writer_mod
from iis3dwb_packet import RATE_HZ

# 커서를 올려 같은 줄을 덮어쓰는 표시는 터미널에서만 의미가 있다.
# 파이프·로그로 받으면 제어문자가 그대로 남아 읽기 어려워지므로 끈다.
LIVE = sys.stdout.isatty()


def render(lines):
    """상태 여러 줄을 표시한다. 터미널이면 제자리에서 갱신한다."""
    if LIVE:
        sys.stdout.write("".join("\033[K" + l + "\n" for l in lines))
        sys.stdout.write("\033[%dA" % len(lines))
        sys.stdout.flush()
    else:
        print(" │ ".join(l.strip() for l in lines), flush=True)


def render_done(n):
    if LIVE:
        sys.stdout.write("\n" * n)
        sys.stdout.flush()


def channel_line(ch):
    """채널 한 줄 — 수신 상태·포토센서 레벨·진행 상황."""
    st = ch.link.stats
    target = RATE_HZ.get(st.rate_step, 0)
    mag = (st.mg[0] ** 2 + st.mg[1] ** 2 + st.mg[2] ** 2) ** 0.5
    garbled = ch.garbled
    live = "●" if ch.receiving else ("✗" if garbled else "○")

    act = ch.trigger.is_active()
    photo = "?" if act is None else ("감지" if act else "대기")

    if ch.state == session_mod.RECORDING:
        prog = "REC %5.1f/%5.1f초 %3.0f%%" % (ch.elapsed, ch.duration_s,
                                              ch.progress * 100)
    elif ch.state == session_mod.FINISHING:
        # 큐에 남은 것을 파일로 비우는 중. 이때 '대기' 로 보이면 이미 끝난 줄 알고
        # 케이블을 뽑는 사람이 생긴다.
        prog = "저장 중..."
    elif garbled:
        prog = "패킷 아님 — 펌웨어 확인"
    else:
        prog = "대기" + ("" if not ch.ignored_triggers
                         else " (무시 %d)" % ch.ignored_triggers)

    return ("  %s %-3s %-13s %6.0f/%d Hz ±%-3s |a|%5.0fmg  DIN%d:%-4s  "
            "유실%5.2f%% 드롭%d  %s"
            % (live, ch.name, ch.port, st.hz, target,
               ("%dg" % st.full_scale_g) if st.full_scale_g else "?",
               mag, ch.din, photo, st.loss_pct, st.queue_drops, prog))


def cmd_status():
    res = slots.resolve()
    dins = slots.load_din_map(res.names)
    print("슬롯 설정: %s" % slots.SLOTS_PATH)
    if res.assigned:
        print("\n지정된 센서  (진동센서 1대 ↔ 포토센서 1개)")
        for name, p in res.assigned:
            print("  %-4s %-14s %-24s  포토센서 DIN%d (GPIO%d)"
                  % (name, p.device, p.short_slot, dins.get(name, 1),
                     {1: 5, 2: 17, 3: 27, 4: 22}[dins.get(name, 1)]))
    if res.unknown:
        print("\n⚠ 이름이 지정되지 않은 슬롯")
        names = slots.suggest_names(res)
        for p in res.unknown:
            print("  %-14s %-24s  지정: bash run.sh slots --assign %s %s"
                  % (p.device, p.short_slot, p.device, names[p.device]))
        print("  한 번에 지정: bash run.sh slots --auto   (USB 구멍 순서대로 1, 2 ...)")
    if res.missing:
        print("\nℹ 센서 %d대가 인식되었습니다. 이대로 수집할 수 있습니다."
          "  (%s 는 연결되지 않음)"
          % (len(res.assigned), ", ".join(res.missing)))
    if not res.assigned and not res.unknown:
        print("\n연결된 센서 포트가 없습니다.")
    print("\n수집 가능 상태: %s" % ("예" if res.ok else "아니오"))
    return 0 if res.ok else 2


def unnamed_guide(res):
    """이름 없는 슬롯이 있을 때 수집 대신 보여 줄 안내. 막아야 하면 True.

    세션을 먼저 열면 '[warn] 이름 없는 슬롯' → '❌ 열 수 있는 센서가 없습니다' →
    상태표가 차례로 찍혀, 처음 쓰는 사람에게는 고장처럼 보였다 (2026-09-27).
    할 일은 하나(번호 지정)뿐이므로 그것만 말하고 끝낸다.

    일부만 이름이 있어도 막는다 — preflight 가 어차피 모든 수집을 거부하는데,
    --auto 에서는 포토센서가 감지된 뒤에야 그 사실이 드러난다.
    """
    if not res.unknown:
        return False
    first = not res.assigned
    print("\n%s\n" % ("센서 번호가 아직 정해지지 않았습니다 — 처음 한 번만 하면 됩니다."
                      if first else
                      "번호가 없는 센서가 있어 수집을 시작하지 않습니다."))
    for name, p in res.assigned:
        print("  센서 %-3s  %-14s USB 구멍 %s" % (name, p.device, p.short_slot))
    for p in res.unknown:
        print("  (번호 없음) %-14s USB 구멍 %s" % (p.device, p.short_slot))
    print("\n  어느 센서의 데이터인지 모른 채 기록하면 되돌릴 수 없어 막아 둔 것입니다.\n")
    print("  1) 센서를 모두 꽂은 상태에서   bash run.sh slots --auto")
    print("     USB 구멍 순서대로 1, 2 … 를 붙이고 포토센서 DIN1, DIN2 … 와 짝짓습니다.")
    print("  2) 고정 장치 이름 (권장)       bash run.sh slots --make-udev")
    print("  3) 다시 수집                   bash run.sh")
    print("\n  자세한 설명: bash run.sh help  (1장 '센서 번호 정하기')")
    return True


def summarize(results, missing=()):
    """결과표와 판정. `missing` 은 열려 있었지만 수집하지 못한 센서 이름이다.

    빠진 센서를 판정에 넣지 않으면 **한 대만 기록하고도 '정상'** 이 나온다.
    실제로 그런 5분 수집이 있었고, 표에 줄이 하나뿐인 것 말고는 단서가 없었다.
    """
    if not results:
        return 3
    print("=" * 74)
    for r in sorted(results, key=lambda x: (x.sensor, x.index)):
        print("  %-3s run_%04d  %-16s %9s샘플  %8s  유실%5.2f%% 드롭%d  %s"
              % (r.sensor, r.index, r.file, "{:,}".format(r.samples),
                 writer_mod.human_bytes(r.bytes), r.loss_pct, r.queue_drops,
                 r.stop_reason))
    bad = [r for r in results if not r.ok]
    reasons = ["%s(%s)" % (r.sensor, r.note) for r in bad]
    reasons += ["%s(수집 못 함)" % n for n in missing]
    print("\n판정: %s" % ("정상" if not reasons else
                          "문제 있음 — %s" % ", ".join(reasons)))
    return 0 if not reasons else 1


def main(argv=None):
    # 기본값은 GUI 가 저장한 설정(settings.json). 옵션을 주면 이번 실행만 옵션이 이긴다.
    cfg = settings_mod.load()
    ap = argparse.ArgumentParser(description="SHT 진동센서 수집 (글자 화면)")
    ap.add_argument("--help-doc", action="store_true",
                    help="사용설명서(help.html)를 브라우저로 연다")
    ap.add_argument("--status", action="store_true", help="상태만 확인하고 종료")
    ap.add_argument("--watch", action="store_true",
                    help="기록하지 않고 실시간 상태만 표시 (Ctrl+C 종료)")
    ap.add_argument("--auto", action="store_true",
                    help="포토센서 신호를 기다려 자동 수집 (Ctrl+C 종료)")
    ap.add_argument("--manual", action="store_true", help="지금 바로 수집 시작")
    ap.add_argument("--only", metavar="이름", help="--manual 대상 센서 하나만")
    ap.add_argument("--minutes", type=float, default=cfg["minutes"],
                    help="수집 길이 (분, 기본: 저장된 설정 %g)" % cfg["minutes"])
    ap.add_argument("--seconds", type=float, default=0,
                    help="수집 길이 (초) — 시험용, --minutes 보다 우선")
    ap.add_argument("--fmt", choices=["csv", "bin"], default=cfg["fmt"])
    ap.add_argument("--out", default=cfg["out"], help="저장 폴더")
    ap.add_argument("--active", choices=["low", "high"],
                    default="low" if cfg["active_low"] else "high",
                    help="포토센서 활성 레벨 (기본: 저장된 설정)")
    ap.add_argument("--baud", type=int, default=sensor_link.DEFAULT_BAUD,
                    help="펌웨어 CONFIG_STREAM_SERIAL_UART_BAUD 와 같아야 한다")
    args = ap.parse_args(argv)

    if args.help_doc:
        import helpdoc
        try:
            helpdoc.open_help()
            print("사용설명서를 열었습니다: %s" % helpdoc.HELP_FILE)
            return 0
        except helpdoc.HelpError as e:
            print("❌ %s" % e, file=sys.stderr)
            return 3

    if args.status:
        return cmd_status()

    # --watch 는 기록하지 않으니, 이름 있는 센서가 하나라도 있으면 보여 준다
    res = slots.resolve()
    if (args.auto or args.manual or not res.assigned) and unnamed_guide(res):
        return 2

    minutes = (args.seconds / 60.0) if args.seconds else args.minutes
    col = session_mod.Collector(
        args.out, minutes=minutes, fmt=args.fmt,
        auto_start=args.auto, active_low=(args.active == "low"), baud=args.baud)
    col.set_event_handler(lambda kind, msg: print("\n[%s] %s" % (kind, msg),
                                                 flush=True))

    col.open()
    if not col.channels:
        print("❌ 열 수 있는 센서가 없습니다.", file=sys.stderr)
        cmd_status()
        return 2

    print("센서를 여는 중... 첫 패킷을 기다립니다 (최대 5초)")
    # 한 대라도 들어오면 끝내면 안 된다. 늦게 깨는 센서가 수집에서 빠진다.
    t0 = time.monotonic()
    while (time.monotonic() - t0 < 5.0
           and len(col.receiving_names) < len(col.channels)):
        time.sleep(0.2)

    dins = slots.load_din_map(list(col.channels))
    for n, ch in sorted(col.channels.items()):
        print("  %s ← 포토센서 DIN%d (GPIO%d) · 활성 %s · %s"
              % (n, ch.din, ch.trigger.gpio, args.active.upper(),
                 "GPIO " + ch.trigger.backend if ch.trigger.backend else "GPIO 사용 불가"))
    for n, ch in sorted(col.channels.items()):
        if ch.garbled:
            print("\n⚠ 센서 %s (%s): 데이터는 들어오지만 IIS3DWB 패킷이 아닙니다.\n"
                  "   통신 속도가 맞지 않는 펌웨어입니다 — 대개 개발용으로 구운 보드입니다.\n"
                  "   설정툴(iis3dwb-setup)로 ① 펌웨어 굽기 → ② 설정 주입을 다시 하세요."
                  % (n, ch.port), flush=True)

    try:
        if args.watch:
            print("\n실시간 상태 — 기록하지 않습니다. Ctrl+C 로 종료.\n")
            while True:
                time.sleep(1.0)
                render([time.strftime("%H:%M:%S")] +
                       [channel_line(col.channels[n]) for n in sorted(col.channels)])

        if args.manual:
            try:
                col.check_space()
            except session_mod.PreflightError as e:
                print("\n❌ 시작할 수 없습니다:\n%s" % e, file=sys.stderr)
                return 2
            names = [args.only] if args.only else None
            started = col.start_manual(names)
            if not started:
                print("❌ 시작된 채널이 없습니다.", file=sys.stderr)
                return 2
            wanted = [args.only] if args.only else list(col.channels)
            missing = [n for n in wanted if n not in started]
            if missing:
                print("\n⚠ 수집을 시작하지 못한 센서: %s\n"
                      "   위의 '시작 불가' 사유를 확인하세요. 나머지는 계속합니다."
                      % ", ".join(sorted(missing)), file=sys.stderr)
            while col.any_recording:
                time.sleep(1.0)
                render([time.strftime("%H:%M:%S")] +
                       [channel_line(col.channels[n]) for n in sorted(col.channels)])
            render_done(len(col.channels) + 1)
            return summarize([ch.last_result for ch in col.channels.values()
                              if ch.last_result], missing=sorted(missing))

        if args.auto:
            try:
                col.check_space()
            except session_mod.PreflightError as e:
                print("\n⚠ %s" % e, file=sys.stderr)
            print("\n자동 수집 대기 — 포토센서가 감지되면 %s씩 기록합니다. "
                  "Ctrl+C 로 종료.\n" % session_mod.human_duration(minutes))
            while True:
                time.sleep(1.0)
                render([time.strftime("%H:%M:%S")] +
                       [channel_line(col.channels[n]) for n in sorted(col.channels)])

        print("❌ --watch / --auto / --manual 중 하나를 지정하세요.", file=sys.stderr)
        return 3

    except KeyboardInterrupt:
        render_done(len(col.channels) + 1)
        print("\n[중단 요청]")
        col.stop_all(reason="사용자 중단")
        results = [ch.last_result for ch in col.channels.values() if ch.last_result]
        return summarize(results) if results else 0
    finally:
        render_done(len(col.channels) + 1)
        col.close()


if __name__ == "__main__":
    sys.exit(main())
