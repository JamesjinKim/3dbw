#!/usr/bin/env python3
"""SHT 진동센서 수집 — 수집기 화면 (tkinter GUI)

진동센서 1대마다 포토센서 1개가 짝을 이룬다. 포토센서가 켜지면 **그 센서만**
설정한 시간 동안 기록하고 스스로 멈춘다. 두 짝은 서로 독립이다.

    포토센서 1(DIN1) 켜짐 → 센서 1 만 N분 수집 → 저장 → 대기
    포토센서 2(DIN2) 켜짐 → 센서 2 만 N분 수집 → 저장 → 대기

수집 엔진은 글자 화면(collect_cli.py)과 같은 session.py 다. 이 파일은 화면만 맡는다.
화면에서 바꾼 공통 설정(수집 시간·활성 레벨·형식·폴더·자동 시작)은 settings.json 에
저장돼 다음 실행과 글자 화면에도 그대로 쓰인다.

## 스레드

GPIO 감시·시리얼 읽기·파일 쓰기는 모두 다른 스레드에서 돈다. 그 스레드들이 보내는
이벤트는 **큐에 넣기만 하고**, 위젯은 메인 스레드의 주기 갱신(after)에서만 만진다.
"""

import json
import os
import queue
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError as e:            # Raspberry Pi OS Lite 등
    print("GUI 를 띄울 수 없습니다 (tkinter 없음: %s)\n"
          "글자 화면으로 수집하려면:  bash run.sh --auto" % e, file=sys.stderr)
    sys.exit(3)

import helpdoc
import session as session_mod
import settings as settings_mod
import slots
import trigger as trigger_mod
import writer as writer_mod
from iis3dwb_packet import RATE_HZ

APP_NAME = "SHT 진동센서 수집"

ACTIVE_LEVELS = [("LOW (NPN)", True), ("HIGH (PNP)", False)]
DIN_CHOICES = [("DIN%d (GPIO%d)" % (d, g), d) for d, g in sorted(trigger_mod.DIN_PINS.items())]
FORMATS = [("CSV", "csv"), ("바이너리", "bin")]

REFRESH_MS = 300            # 센서 칸 갱신 주기 — 포토센서 켜짐을 눈으로 따라갈 수 있게
SLOW_REFRESH_MS = 2000      # 용량 예상·최근 수집 목록

GREEN, AMBER, RED, GREY = "#2a7", "#b36b00", "#b33", "#888"
REC_BG, REC_BG2 = "#d22", "#f07070"     # 수집 중 배지 — 두 색을 번갈아 깜빡인다
IDLE_BG = "#e4e4e4"


def _mmss(sec):
    """남은 시간 표기 — 125 → '2:05', 3725 → '1:02:05'."""
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return ("%d:%02d:%02d" % (h, m, s)) if h else ("%d:%02d" % (m, s))


def human_minutes_field(m):
    """수집 길이 입력칸 표기 — 5.0 → '5', 0.5 → '0.5'."""
    return ("%g" % m)


class SensorCard:
    """센서 1대 칸 — 수신 상태 · 자기 포토센서 · 자기 진행률."""

    def __init__(self, app, parent, row, ch):
        self.app = app
        self.ch = ch
        self.frame = ttk.LabelFrame(parent, text=" 센서 %s " % ch.name)
        self.frame.grid(row=row, column=0, sticky="ew", padx=12, pady=3)

        # 1행: 상태 배지 + 수신 상태
        # 배지는 **수집 중인지 한눈에** 보이게 하는 자리다. 로그는 지나가 버리고 진행
        # 막대는 작아서, 수집 중인데도 모르겠다는 피드백이 있었다 (2026-09-27).
        l1 = ttk.Frame(self.frame)
        l1.grid(row=0, column=0, sticky="w", padx=10, pady=(6, 2))
        self.badge = tk.Label(l1, text="", width=16, font=("", 11, "bold"),
                              padx=6, pady=2)
        self.badge.pack(side="left", padx=(0, 10))
        self.dot = tk.Label(l1, text="○", fg=GREY, font=("", 12))
        self.dot.pack(side="left", padx=(0, 6))
        tk.Label(l1, text=ch.port, width=15, anchor="w",
                 font=("monospace", 9)).pack(side="left")
        self.hz = tk.Label(l1, text="", width=18, anchor="w")
        self.hz.pack(side="left")
        self.fs = tk.Label(l1, text="", width=5, anchor="w")
        self.fs.pack(side="left")
        self.mg = tk.Label(l1, text="", anchor="w")
        self.mg.pack(side="left")

        # 2행: 이 센서의 포토센서 + 수동 조작
        l2 = ttk.Frame(self.frame)
        l2.grid(row=1, column=0, sticky="w", padx=10, pady=2)
        ttk.Label(l2, text="포토센서").pack(side="left")
        self.din_cb = ttk.Combobox(l2, width=15, state="readonly",
                                   values=[l for l, _ in DIN_CHOICES])
        self.din_cb.current([d for _, d in DIN_CHOICES].index(ch.din))
        self.din_cb.bind("<<ComboboxSelected>>", lambda e: self._on_din())
        self.din_cb.pack(side="left", padx=6)
        self.photo = tk.Label(l2, text="", width=14, anchor="w",
                              font=("", 10, "bold"))
        self.photo.pack(side="left", padx=8)
        self.start_btn = ttk.Button(l2, text="수동 시작", width=10,
                                    command=lambda: app.on_start([ch.name]))
        self.start_btn.pack(side="left", padx=(8, 4))
        self.stop_btn = ttk.Button(l2, text="중지", width=7,
                                   command=lambda: app.on_stop(ch))
        self.stop_btn.pack(side="left")

        # 3행: 진행 상황
        self.pb = ttk.Progressbar(self.frame, length=640, mode="determinate",
                                  style="Rec.Horizontal.TProgressbar")
        self.pb.grid(row=2, column=0, sticky="w", padx=10, pady=(3, 1))
        # 높이 1줄 고정 — 수집 시작/종료로 창 높이가 뛰지 않게 한다
        self.note = tk.Label(self.frame, text="", height=1, anchor="w",
                             justify="left", font=("monospace", 9))
        self.note.grid(row=3, column=0, sticky="w", padx=10, pady=(0, 8))

    def _on_din(self):
        din = DIN_CHOICES[self.din_cb.current()][1]
        if din == self.ch.din:
            return
        if self.ch.state != session_mod.IDLE:
            messagebox.showwarning(APP_NAME, "수집 중에는 포토센서를 바꿀 수 없습니다.")
            self.din_cb.current([d for _, d in DIN_CHOICES].index(self.ch.din))
            return
        self.app.change_din(self.ch, din)

    def refresh(self):
        ch = self.ch
        st = ch.link.stats
        rec = ch.state == session_mod.RECORDING

        garbled = ch.garbled
        if ch.receiving:
            self.dot.configure(text="●", fg=GREEN)
        elif garbled:
            self.dot.configure(text="✗", fg=RED)
        else:
            self.dot.configure(text="○", fg=GREY)

        target = RATE_HZ.get(st.rate_step, 0)
        self.hz.configure(text="%s / %s Hz" % ("{:,}".format(int(st.hz)),
                                               "{:,}".format(target) if target else "?"))
        self.fs.configure(text=("±%dg" % st.full_scale_g) if st.full_scale_g else "")
        if ch.receiving:
            mag = (st.mg[0] ** 2 + st.mg[1] ** 2 + st.mg[2] ** 2) ** 0.5
            okg = 900 <= mag <= 1100    # 가만히 둔 센서는 중력 1g ≈ 1000 mg
            self.mg.configure(text="|a| %d mg %s" % (mag, "✓" if okg else "⚠"),
                              fg=GREEN if okg else AMBER)
        else:
            self.mg.configure(text="")

        act = ch.trigger.is_active()
        lv = ch.trigger.level()
        if act is None:
            self.photo.configure(text="? 사용 불가", fg=RED)
        else:
            self.photo.configure(text="● %s — %s" % ("LOW" if lv == 0 else "HIGH",
                                                     "감지" if act else "대기"),
                                 fg=AMBER if act else GREEN)

        idle = ch.state == session_mod.IDLE
        self.start_btn.configure(state="normal" if idle else "disabled")
        self.stop_btn.configure(state="normal" if rec else "disabled")
        self.din_cb.configure(state="readonly" if idle else "disabled")

        if rec:
            # 수집 중 — 빨간 배지가 깜빡이고 남은 시간을 보여 준다
            left = max(0.0, ch.duration_s - ch.elapsed)
            on = self.app.blink
            self.badge.configure(text="● 수집 중  %s" % _mmss(left),
                                 bg=REC_BG if on else REC_BG2, fg="white")
            self.frame.configure(style="Rec.TLabelframe")
            self.pb.configure(value=ch.progress * 100)
            w = ch.writer
            samples = w.written_samples if w else 0
            self.note.configure(
                fg="#333",
                text="run_%04d   %s / %s   %s샘플   유실 %.2f%%%s"
                     % (ch.run_index, session_mod.human_duration(ch.elapsed / 60.0),
                        session_mod.human_duration(ch.duration_s / 60.0),
                        "{:,}".format(samples), st.loss_pct,
                        ("   무시 %d회" % ch.ignored_triggers) if ch.ignored_triggers else ""))
            return

        self.frame.configure(style="TLabelframe")
        self.pb.configure(value=0)
        if ch.state == session_mod.FINISHING:
            self.badge.configure(text="저장 중…", bg=AMBER, fg="white")
            self.note.configure(fg="#333", text="")
        elif garbled:
            self.badge.configure(text="✗ 읽을 수 없음", bg=RED, fg="white")
            self.note.configure(fg=RED, text="펌웨어 확인")
        elif not ch.receiving:
            self.badge.configure(text="○ 데이터 없음", bg="#ddd", fg=RED)
            self.note.configure(fg=RED, text="")
        else:
            auto = self.app.collector.auto_start
            self.badge.configure(text="대기" if auto else "대기 (자동 꺼짐)",
                                 bg=IDLE_BG, fg="#333")
            r = ch.last_result
            self.note.configure(
                fg="#555" if (not r or r.ok) else AMBER,
                text=("직전 run_%04d  %s  %s" % (r.index, (r.ended or "")[11:16],
                                               "정상" if r.ok else "⚠ " + (r.note or "문제"))
                      if r else ""))


class CollectorApp(tk.Tk):
    # 라즈베리파이 데스크톱은 labwc(Wayland)라 창 위치 요청이 무시된다. 제목줄이
    # 화면 밖으로 숨지 않게 **크기로** 보장한다 (gui 시안에서 겪은 문제).
    MARGIN_TOP = 96
    MARGIN_BOTTOM = 64

    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        for seq in ("<Control-q>", "<Control-w>"):
            self.bind(seq, lambda _e: self.on_close())

        self.cfg = settings_mod.load()
        self.events = queue.Queue()
        self.blink = False
        self._ticks = 0
        st = ttk.Style(self)
        # 수집 중인 센서 칸 — 테두리 제목을 빨갛게, 진행 막대를 빨갛게
        st.configure("Rec.TLabelframe.Label", foreground=REC_BG, font=("", 10, "bold"))
        st.configure("Rec.Horizontal.TProgressbar", background=REC_BG, thickness=14)
        self.cards = []
        self.collector = session_mod.Collector(
            self.cfg["out"], minutes=self.cfg["minutes"], fmt=self.cfg["fmt"],
            auto_start=self.cfg["auto_start"], active_low=self.cfg["active_low"])
        self.collector.set_event_handler(lambda k, m: self.events.put((k, m)))

        self._build()
        self.reopen(first=True)
        self._autosize()
        self.after(REFRESH_MS, self._tick)
        self.after(500, self._slow_tick)

    # ================================================================
    # 화면 구성
    # ================================================================
    def _build(self):
        outer = ttk.Frame(self)
        outer.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(outer, highlightthickness=0)
        self.vsb = ttk.Scrollbar(outer, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self._on_yscroll)
        self.canvas.pack(side="left", fill="both", expand=True)
        body = ttk.Frame(self.canvas)
        self.body = body
        self._body_id = self.canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", self._on_body_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        for seq, d in (("<Button-4>", -2), ("<Button-5>", 2)):
            self.canvas.bind_all(seq, lambda e, dd=d: self._wheel(dd))

        r = 0
        hdr = ttk.Frame(body)
        hdr.grid(row=r, column=0, sticky="ew", padx=12, pady=(8, 2)); r += 1
        ttk.Label(hdr, text=APP_NAME, font=("", 15, "bold")).pack(side="left")
        # 전체 상태 — 어느 센서가 수집 중인지 창 맨 위에서 바로 보인다
        self.overall = tk.Label(hdr, text="", font=("", 12, "bold"), padx=10, pady=3)
        self.overall.pack(side="right")
        ttk.Separator(body, orient="horizontal").grid(
            row=r, column=0, sticky="ew", pady=5); r += 1

        top = ttk.Frame(body)
        top.grid(row=r, column=0, sticky="ew", padx=12, pady=(0, 2)); r += 1
        self.assign_btn = ttk.Button(top, text="센서 번호 지정", command=self.on_assign)
        self.assign_btn.pack(side="left")
        self.reopen_btn = ttk.Button(top, text="다시 연결", width=9,
                                     command=lambda: self.reopen())
        self.reopen_btn.pack(side="left", padx=6)

        # 연결 상황 안내 (번호 없는 슬롯 · 일부만 연결 · 센서 없음)
        self.banner = tk.Label(body, text="", anchor="w", justify="left")
        self.banner.grid(row=r, column=0, sticky="ew", padx=14); r += 1

        self.cards_frame = ttk.Frame(body)
        self.cards_frame.grid(row=r, column=0, sticky="ew"); r += 1

        # ===== 공통 설정 =====
        cf = ttk.LabelFrame(body, text=" 공통 설정 ")
        cf.grid(row=r, column=0, sticky="ew", padx=12, pady=4); r += 1
        ttk.Label(cf, text="수집 시간").grid(row=0, column=0, sticky="w", padx=10, pady=3)
        df = ttk.Frame(cf); df.grid(row=0, column=1, sticky="w", padx=10, pady=3)
        self.minutes_var = tk.StringVar(value=human_minutes_field(self.cfg["minutes"]))
        me = ttk.Entry(df, width=6, textvariable=self.minutes_var)
        me.pack(side="left")
        me.bind("<Return>", lambda e: self.apply_minutes())
        me.bind("<FocusOut>", lambda e: self.apply_minutes())
        ttk.Label(df, text=" 분").pack(side="left")

        ttk.Label(cf, text="포토센서 활성 레벨").grid(row=1, column=0, sticky="w",
                                                padx=10, pady=3)
        self.active_cb = ttk.Combobox(cf, width=18, state="readonly",
                                      values=[l for l, _ in ACTIVE_LEVELS])
        self.active_cb.current(0 if self.cfg["active_low"] else 1)
        self.active_cb.bind("<<ComboboxSelected>>", lambda e: self.on_active())
        self.active_cb.grid(row=1, column=1, sticky="w", padx=10, pady=3)

        af = ttk.Frame(cf)
        af.grid(row=2, column=0, columnspan=2, sticky="w", padx=10, pady=(4, 8))
        self.auto_var = tk.BooleanVar(value=self.cfg["auto_start"])
        ttk.Checkbutton(af, text="포토센서로 자동 시작", variable=self.auto_var,
                        command=self.on_auto).pack(side="left")
        self.all_start_btn = ttk.Button(af, text="전체 시작", width=10,
                                        command=lambda: self.on_start(None))
        self.all_start_btn.pack(side="left", padx=(16, 4))
        self.all_stop_btn = ttk.Button(af, text="전체 중지", width=10,
                                       command=self.on_stop_all)
        self.all_stop_btn.pack(side="left")

        # ===== 저장 =====
        of = ttk.LabelFrame(body, text=" 저장 ")
        of.grid(row=r, column=0, sticky="ew", padx=12, pady=4); r += 1
        ttk.Label(of, text="폴더").grid(row=0, column=0, sticky="w", padx=10, pady=3)
        pf = ttk.Frame(of); pf.grid(row=0, column=1, sticky="w", padx=10, pady=3)
        self.out_var = tk.StringVar(value=self.cfg["out"])
        ttk.Entry(pf, width=34, textvariable=self.out_var,
                  state="readonly").pack(side="left")
        self.out_btn = ttk.Button(pf, text="변경", width=6, command=self.on_out)
        self.out_btn.pack(side="left", padx=4)
        ttk.Label(of, text="형식").grid(row=1, column=0, sticky="nw", padx=10, pady=3)
        ff = ttk.Frame(of); ff.grid(row=1, column=1, sticky="w", padx=10, pady=3)
        self.fmt_var = tk.StringVar(value=self.cfg["fmt"])
        for label, val in FORMATS:
            ttk.Radiobutton(ff, text=label, value=val, variable=self.fmt_var,
                            command=self.on_fmt).pack(anchor="w")
        self.space = tk.Label(of, text="", anchor="w")
        self.space.grid(row=2, column=0, columnspan=2, sticky="w", padx=10, pady=(0, 6))

        # ===== 최근 수집 =====
        rf = ttk.LabelFrame(body, text=" 최근 수집 ")
        rf.grid(row=r, column=0, sticky="ew", padx=12, pady=4); r += 1
        self.recent = tk.Listbox(rf, height=4, width=74, font=("monospace", 9))
        self.recent.grid(row=0, column=0, sticky="ew", padx=10, pady=(6, 4))
        self._recent_dirs = []
        bf = ttk.Frame(rf); bf.grid(row=1, column=0, sticky="w", padx=10, pady=(0, 8))
        ttk.Button(bf, text="폴더 열기", command=self.on_open_folder).pack(side="left")

        # ===== 로그 =====
        self.log = tk.Text(body, height=4, width=76, wrap="word", state="disabled")
        self.log.grid(row=r, column=0, padx=12, pady=(6, 4)); r += 1
        for kind, color in (("warn", AMBER), ("error", RED), ("started", "#246"),
                            ("finished", GREEN)):
            self.log.tag_configure(kind, foreground=color)

        foot = ttk.Frame(body)
        foot.grid(row=r, column=0, sticky="ew", padx=12, pady=(0, 10))
        ttk.Button(foot, text="도움말", command=self.on_help).pack(side="left")
        ttk.Button(foot, text="로그 저장", command=self.on_save_log).pack(side="left", padx=6)

    # ================================================================
    # 연결
    # ================================================================
    def reopen(self, first=False):
        """센서를 다시 찾아 연다. 수집 중에는 하지 않는다."""
        if not first and self.collector.any_recording:
            messagebox.showwarning(APP_NAME, "수집 중에는 다시 연결할 수 없습니다.")
            return
        for c in self.cards:
            c.frame.destroy()
        self.cards = []
        res = self.collector.open()
        for i, name in enumerate(sorted(self.collector.channels)):
            self.cards.append(SensorCard(self, self.cards_frame, i,
                                         self.collector.channels[name]))
        self._show_banner(res)
        self.logln("info", "센서 %d대 연결: %s" % (
            len(self.collector.channels),
            ", ".join("센서 %s" % n for n in sorted(self.collector.channels)) or "없음"))
        self._refresh_recent()
        if not first:
            self._autosize()

    def _show_banner(self, res):
        if res.unknown:
            self.banner.configure(
                fg=RED, text="⚠ 번호 없는 센서: %s — [센서 번호 지정] 필요"
                             % ", ".join(os.path.basename(p.device) for p in res.unknown))
        elif not res.assigned:
            self.banner.configure(fg=RED, text="⚠ 연결된 진동센서 없음")
        elif res.missing:
            self.banner.configure(fg="#555", text="ℹ 센서 %s 미연결"
                                  % ", ".join(res.missing))
        else:
            self.banner.configure(text="")

    def on_assign(self):
        if self.collector.any_recording:
            messagebox.showwarning(APP_NAME, "수집 중에는 번호를 바꿀 수 없습니다.")
            return
        ports = slots.list_ports()
        if not ports:
            messagebox.showwarning(APP_NAME, "연결된 진동센서가 없습니다.")
            return
        lines = "\n".join("  센서 %d  ←  USB 구멍 %s (%s)  ·  포토센서 DIN%d"
                          % (i + 1, p.short_slot, os.path.basename(p.device), i + 1)
                          for i, p in enumerate(ports))
        if not messagebox.askyesno(
                "센서 번호 지정",
                "USB 구멍 순서대로 번호를 붙입니다.\n\n%s\n\n"
                "진동센서 1번이 위 첫 줄의 구멍에 꽂혀 있어야 포토센서 1번과 짝이 맞습니다.\n"
                "이대로 지정할까요?" % lines):
            return
        slots.auto_assign()
        self.logln("info", "센서 번호 지정: " + ", ".join(
            "센서 %d = %s" % (i + 1, p.short_slot) for i, p in enumerate(ports)))
        self.reopen()

    def change_din(self, ch, din):
        others = [c.ch.name for c in self.cards if c.ch is not ch and c.ch.din == din]
        ch.set_din(din)
        dm = slots.load_din_map(list(self.collector.channels))
        dm[ch.name] = din
        slots.save_din_map(dm)
        self.logln("info", "센서 %s ← 포토센서 DIN%d (GPIO%d)"
                   % (ch.name, din, trigger_mod.DIN_PINS[din]))
        if others:
            self.logln("warn", "센서 %s 도 DIN%d 을 보고 있습니다 — 포토센서 하나가 두 센서를 "
                               "함께 시작시킵니다" % (", ".join(others), din))

    # ================================================================
    # 설정 변경
    # ================================================================
    def apply_minutes(self):
        """입력칸의 값을 적용한다. 잘못된 값이면 되돌리고 False."""
        try:
            m = float(self.minutes_var.get().strip())
            if not (0 < m <= 24 * 60):
                raise ValueError
        except ValueError:
            messagebox.showwarning(APP_NAME, "수집 시간은 0보다 크고 1440분(24시간) 이하인 "
                                             "숫자로 입력하세요.")
            self.minutes_var.set(human_minutes_field(self.collector.minutes))
            return False
        if m != self.collector.minutes:
            self.collector.minutes = m
            settings_mod.save({"minutes": m})
            self.logln("info", "수집 시간 %s (다음 수집부터)" % session_mod.human_duration(m))
        return True

    def on_active(self):
        low = ACTIVE_LEVELS[self.active_cb.current()][1]
        if low == self.collector.active_low:
            return
        self.collector.set_active_low(low)
        settings_mod.save({"active_low": low})
        self.logln("info", "포토센서 활성 레벨: %s" % ("LOW (NPN)" if low else "HIGH (PNP)"))

    def on_auto(self):
        v = bool(self.auto_var.get())
        self.collector.auto_start = v
        settings_mod.save({"auto_start": v})
        self.logln("info" if v else "warn",
                   "포토센서 자동 시작 %s" % ("켜짐" if v else "꺼짐 — 포토센서가 켜져도 "
                                                      "기록하지 않습니다"))

    def on_fmt(self):
        v = self.fmt_var.get()
        self.collector.fmt = v
        settings_mod.save({"fmt": v})
        self.logln("info", "저장 형식 %s (다음 수집부터)" % v.upper())

    def on_out(self):
        if self.collector.any_recording:
            messagebox.showwarning(APP_NAME, "수집 중에는 폴더를 바꿀 수 없습니다.")
            return
        d = filedialog.askdirectory(initialdir=self.out_var.get(), title="저장 폴더")
        if not d:
            return
        self.out_var.set(d)
        self.collector.out_dir = Path(d)
        settings_mod.save({"out": d})
        self.logln("info", "저장 폴더: %s" % d)
        self._refresh_recent()

    # ================================================================
    # 수집 조작
    # ================================================================
    def on_start(self, names):
        if not self.apply_minutes():
            return
        started = self.collector.start_manual(names)
        wanted = names or sorted(self.collector.channels)
        missing = [n for n in wanted if n not in started]
        if missing:
            # 이유는 엔진이 로그에 남긴다. 누른 사람이 놓치지 않게 창으로도 알린다.
            messagebox.showwarning(APP_NAME, "시작하지 못한 센서: %s\n\n이유는 아래 기록을 "
                                             "확인하세요." % ", ".join(missing))

    def on_stop(self, ch):
        self.logln("info", "센서 %s 중지 요청 — 저장 중..." % ch.name)
        self.update_idletasks()
        ch.stop(reason="사용자 중지")

    def on_stop_all(self):
        self.update_idletasks()
        self.collector.stop_all()

    # ================================================================
    # 주기 갱신
    # ================================================================
    def _tick(self):
        self._ticks += 1
        self.blink = (self._ticks // 2) % 2 == 0     # 약 0.6초마다 깜빡임
        try:
            refresh_recent = False
            while True:
                kind, msg = self.events.get_nowait()
                self.logln(kind, msg)
                if "완료" in msg:
                    refresh_recent = True
        except queue.Empty:
            pass
        for c in self.cards:
            try:
                c.refresh()
            except tk.TclError:
                pass
        rec = self.collector.any_recording
        chans = bool(self.collector.channels)
        recs = sorted(n for n, ch in self.collector.channels.items()
                      if ch.state != session_mod.IDLE)
        if recs:
            self.overall.configure(text="● 수집 중 — 센서 %s" % ", ".join(recs),
                                   bg=REC_BG if self.blink else REC_BG2, fg="white")
        elif chans:
            self.overall.configure(text="대기 중" if self.collector.auto_start
                                   else "대기 중 (자동 시작 꺼짐)", bg=IDLE_BG, fg="#333")
        else:
            self.overall.configure(text="센서 없음", bg=IDLE_BG, fg=RED)
        self.all_start_btn.configure(state="normal" if chans else "disabled")
        self.all_stop_btn.configure(state="normal" if rec else "disabled")
        for b in (self.assign_btn, self.reopen_btn, self.out_btn):
            b.configure(state="disabled" if rec else "normal")
        if refresh_recent:
            self._refresh_recent()
        self.after(REFRESH_MS, self._tick)

    def _slow_tick(self):
        self._update_space()
        self.after(SLOW_REFRESH_MS, self._slow_tick)

    def _update_space(self):
        c = self.collector
        n = max(1, len(c.channels))
        hz = max((RATE_HZ.get(ch.link.stats.rate_step) or 0
                  for ch in c.channels.values()), default=0) or 26667
        need = writer_mod.estimate_bytes(c.fmt, c.minutes, hz, n)
        p = Path(c.out_dir)
        while not p.exists() and p != p.parent:
            p = p.parent
        try:
            free = shutil.disk_usage(str(p)).free
        except OSError:
            self.space.configure(text="여유 확인 불가", fg=AMBER)
            return
        ok = need <= free * 0.9
        self.space.configure(
            fg=GREEN if ok else RED,
            text="1회 %s  ·  여유 %s%s" % (writer_mod.human_bytes(need),
                                          writer_mod.human_bytes(free),
                                          "" if ok else "  ⚠ 부족"))

    def _refresh_recent(self):
        """저장 폴더의 최근 run_*.json 을 읽어 목록을 만든다 (프로그램을 다시 켜도 보이게)."""
        rows = []
        base = Path(self.collector.out_dir)
        try:
            metas = sorted(base.glob("*/*/run_*.json"),
                           key=lambda p: p.stat().st_mtime, reverse=True)[:30]
        except OSError:
            metas = []
        for mp in metas:
            try:
                m = json.loads(mp.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            bad = (m.get("shortfall") or m.get("write_error")
                   or (("유실 %.2f%%" % m["loss_pct"]) if m.get("lost_packets") else "")
                   or (("드롭 %d" % m["queue_drops"]) if m.get("queue_drops") else ""))
            # 폭이 들쭉날쭉한 한글 판정은 맨 끝에 둔다 (고정폭 칸이 어긋나지 않게)
            rows.append((mp.parent, "%-3s run_%04d  %s  %-7s %-4s %9s  %s" % (
                m.get("sensor", "?"), m.get("run", 0),
                (m.get("started") or "")[5:16].replace("T", " "),
                session_mod.human_duration((m.get("actual_seconds") or 0) / 60.0),
                (m.get("format") or "").upper(),
                writer_mod.human_bytes(m.get("bytes") or 0),
                ("⚠ " + bad) if bad else "정상")))
        self.recent.delete(0, "end")
        self._recent_dirs = [d for d, _ in rows]
        for _, line in rows:
            self.recent.insert("end", line)
        if not rows:
            self.recent.insert("end", "아직 수집한 기록이 없습니다")

    # ================================================================
    # 기타
    # ================================================================
    def logln(self, kind, msg):
        self.log.configure(state="normal")
        self.log.insert("end", "%s  %s\n" % (time.strftime("%H:%M:%S"), msg),
                        kind if kind in ("warn", "error", "started", "finished") else ())
        self.log.see("end")
        self.log.configure(state="disabled")

    def on_open_folder(self):
        sel = self.recent.curselection()
        d = (self._recent_dirs[sel[0]] if sel and sel[0] < len(self._recent_dirs)
             else Path(self.collector.out_dir))
        d.mkdir(parents=True, exist_ok=True)
        exe = shutil.which("xdg-open")
        if not exe:
            messagebox.showinfo(APP_NAME, "폴더: %s" % d)
            return
        subprocess.Popen([exe, str(d)], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)

    def on_help(self):
        try:
            helpdoc.open_help()
            self.logln("info", "도움말을 열었습니다")
        except helpdoc.HelpError as e:
            messagebox.showwarning(APP_NAME, str(e))

    def on_save_log(self):
        path = filedialog.asksaveasfilename(
            title="로그 저장", defaultextension=".txt",
            initialfile="수집기로그_%s.txt" % datetime.now().strftime("%Y%m%d_%H%M%S"))
        if not path:
            return
        try:
            Path(path).write_text(self.log.get("1.0", "end"), encoding="utf-8")
            self.logln("info", "로그 저장: %s" % path)
        except OSError as e:
            messagebox.showerror(APP_NAME, "저장 실패: %s" % e)

    def on_close(self):
        if self.collector.any_recording and not messagebox.askyesno(
                APP_NAME, "수집 중인 센서가 있습니다.\n지금 끝내면 이 시점까지 저장하고 "
                          "종료합니다. 끝낼까요?"):
            return
        self.logln("info", "종료 중...")
        self.update_idletasks()
        self.collector.close()
        self.destroy()

    # ---- 스크롤 컨테이너 (gui 시안과 같은 방식) ----
    def _on_body_configure(self, _e=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self._sync_scrollbar()

    def _on_canvas_configure(self, e):
        self.canvas.itemconfigure(self._body_id, width=e.width)
        self._sync_scrollbar()

    def _on_yscroll(self, first, last):
        self.vsb.set(first, last)
        self._sync_scrollbar()

    def _sync_scrollbar(self):
        need = self.body.winfo_reqheight() > self.canvas.winfo_height() + 1
        if need and not self.vsb.winfo_ismapped():
            self.vsb.pack(side="right", fill="y", before=self.canvas)
        elif not need and self.vsb.winfo_ismapped():
            self.vsb.pack_forget()
            self.canvas.yview_moveto(0)

    def _wheel(self, d):
        if self.body.winfo_reqheight() > self.canvas.winfo_height():
            self.canvas.yview_scroll(d, "units")

    def _autosize(self):
        self.update_idletasks()
        w = self.body.winfo_reqwidth() + 24
        h = self.body.winfo_reqheight() + 8
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        avail_h = max(320, sh - self.MARGIN_TOP - self.MARGIN_BOTTOM)
        w = min(w, int(sw * 0.95))
        h = min(h, avail_h)
        x = max(0, (sw - w) // 2)
        y = self.MARGIN_TOP + max(0, (avail_h - h) // 2)
        self.geometry("%dx%d+%d+%d" % (w, h, x, y))
        self.minsize(min(w, 620), 400)
        self.update_idletasks()
        self._sync_scrollbar()


def main():
    try:
        app = CollectorApp()
    except tk.TclError as e:        # 화면(DISPLAY)이 없을 때
        print("GUI 를 띄울 수 없습니다 (%s)\n"
              "글자 화면으로 수집하려면:  bash run.sh --auto" % e, file=sys.stderr)
        return 3
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
