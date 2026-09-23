#!/usr/bin/env python3
"""수집기 GUI 레이아웃 미리보기 — 실제 위젯으로 모습만 확인한다.

센서도 GPIO 도 건드리지 않는다. 숫자는 모두 예시값이다.
레이아웃·문구·배치를 확정한 뒤 collector_gui.py 에 반영하기 위한 것이다.

    python3 gui_preview.py            둘 다 대기
    python3 gui_preview.py mixed      A 수집 중 · B 대기 (독립 동작)
    python3 gui_preview.py one        1대만 인식 (안내 — 정상 운영)
    python3 gui_preview.py warn       이름 없는 슬롯 (경고 — 시작 막음)

## 구조

진동센서 1대마다 포토센서 1개가 짝을 이루므로, **센서 칸마다 자기 포토센서와
자기 진행률을 담는다.** 두 칸은 서로 다른 상태일 수 있다.
수집 길이·활성 레벨·저장 형식은 공통이라 아래 '공통 설정' 에 한 번만 둔다.
"""

import sys
import tkinter as tk
from tkinter import ttk

PAD = {"padx": 12, "pady": 3}

ACTIVE_LEVELS = ["LOW (NPN · 기본)", "HIGH (PNP)"]
DIN_CHOICES = ["DIN1 (GPIO5)", "DIN2 (GPIO17)", "DIN3 (GPIO27)", "DIN4 (GPIO22)"]
FORMATS = [("CSV — 바로 열어봄 (용량 큼)", "csv"),
           ("바이너리 — 용량 1/10 (변환 필요)", "bin")]

# (이름, 슬롯라벨, 장치, 수신중, Hz, 목표Hz, 풀스케일, mg, DIN인덱스, 상태, 진행%, 진행문구)
DEMO = {
    "idle": [
        ("1", "/dev/iis3dwb1", "ttyACM0", True, 26797, 26667, 8, 981, 0,
         "idle", 0, "대기 — 포토센서 신호를 기다립니다"),
        ("2", "/dev/iis3dwb2", "ttyACM1", True, 26612, 26667, 4, 1004, 1,
         "idle", 0, "대기 — 포토센서 신호를 기다립니다"),
    ],
    "mixed": [
        ("1", "/dev/iis3dwb1", "ttyACM0", True, 26788, 26667, 8, 992, 0,
         "rec", 44, "run_0012   2분 12초 / 5분   3,528,000샘플  유실 0.00%  큐 0%"),
        ("2", "/dev/iis3dwb2", "ttyACM1", True, 26794, 26667, 4, 997, 1,
         "idle", 0, "대기 — 직전 run_0007 정상 (09:12)"),
    ],
    # 1대만 연결 — **정상 운영이다.** 경고가 아니라 안내로 보여야 한다.
    "one": [
        ("1", "/dev/iis3dwb1", "ttyACM0", True, 26790, 26667, 8, 986, 0,
         "idle", 0, "대기 — 포토센서 신호를 기다립니다"),
    ],
    # 모르는 슬롯이 꽂혀 있음 — 이것만 진짜 경고다 (시작을 막는다).
    "warn": [
        ("1", "/dev/iis3dwb1", "ttyACM0", True, 26790, 26667, 8, 986, 0,
         "idle", 0, "대기 — 포토센서 신호를 기다립니다"),
    ],
}


class Preview(tk.Tk):
    # 화면 위아래로 비워 둘 공간.
    #
    # 라즈베리파이 데스크톱은 **labwc(Wayland)** 이고 tkinter 는 XWayland 로 뜬다.
    # 이 환경에서 클라이언트가 요청한 창 위치는 **컴포지터가 무시한다.** 그래서
    # "좌표를 중앙으로 계산" 하는 방식으로는 제목줄이 가려지는 것을 막을 수 없다.
    # (실제로 그렇게 고쳤는데도 제목줄이 화면 위로 숨는 문제가 계속됐다.)
    #
    # 확실한 방법은 **위치가 아니라 크기로 보장**하는 것이다. 상단 패널(약 36px)과
    # 창 제목줄(약 40px), 하단 여유를 빼고 남는 만큼만 창을 키우면, 컴포지터가
    # 어디에 놓든 제목줄이 화면 안에 들어온다.
    MARGIN_TOP = 96        # 상단 패널 + 제목줄
    MARGIN_BOTTOM = 64     # 하단 여유

    def __init__(self, mode="idle"):
        super().__init__()
        self.mode = mode
        self.title("IIS3DWB 진동 수집기 — 레이아웃 미리보기")
        # 제목줄이 어떤 이유로든 가려져도 키보드로 닫을 수 있게 한다.
        for seq in ("<Escape>", "<Control-q>", "<Control-w>"):
            self.bind(seq, lambda _e: self.destroy())

        # ===== 스크롤 컨테이너 =====
        # 스크롤바는 화면이 부족할 때만 나타난다. 첫 로딩에서는 모든 항목이
        # 보이도록 창을 키우는 것이 기본 동작이다.
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

        # ===== 헤더 =====
        hdr = ttk.Frame(body)
        hdr.grid(row=r, column=0, sticky="ew", padx=12, pady=(8, 2))
        ttk.Label(hdr, text="IIS3DWB 진동 수집기",
                  font=("", 15, "bold")).pack(anchor="w")
        ttk.Label(hdr, text="진동센서 1대마다 포토센서 1개 — 각자 자기 신호만 보고 "
                            "따로 수집합니다",
                  foreground="#555").pack(anchor="w")
        r += 1
        ttk.Separator(body, orient="horizontal").grid(
            row=r, column=0, sticky="ew", pady=5); r += 1

        # ===== 센서 칸 =====
        top = ttk.Frame(body)
        top.grid(row=r, column=0, sticky="ew", padx=12, pady=(0, 2)); r += 1
        ttk.Button(top, text="슬롯 지정", command=self._noop).pack(side="left")
        ttk.Button(top, text="새로고침", width=9,
                   command=self._noop).pack(side="left", padx=6)
        ttk.Label(top, text="USB 구멍마다 고정 이름(/dev/iis3dwb1 …)을 씁니다 — 재부팅해도 바뀌지 않습니다",
                  foreground="#666").pack(side="left", padx=8)

        for spec in DEMO[self.mode]:
            self._sensor_card(body, r, spec); r += 1

        # 두 상황을 구분한다.
        #
        #  · 일부만 연결 (1대만 인식) — **정상 운영이다.** 한 대만으로 수집하는
        #    경우가 실제로 있으므로 경고로 겁줄 일이 아니다. 회색 안내로 둔다.
        #  · 모르는 슬롯 — 이것만 진짜 문제다. 어느 센서인지 모르는 데이터를
        #    'A' 로 기록하면 되돌릴 수 없으므로 시작을 막고 빨강으로 알린다.
        if self.mode == "one":
            nf = ttk.Frame(body)
            nf.grid(row=r, column=0, sticky="ew", padx=12, pady=(0, 4)); r += 1
            ttk.Label(nf, foreground="#555", justify="left",
                      text="ℹ 센서 1대(1번)가 인식되었습니다. 이대로 수집할 수 있습니다."
                           "  (2번은 연결되지 않음)"
                      ).pack(anchor="w")
        elif self.mode == "warn":
            wf = ttk.Frame(body)
            wf.grid(row=r, column=0, sticky="ew", padx=12, pady=(0, 4)); r += 1
            ttk.Label(wf, foreground="#a33", justify="left",
                      text="⚠ 이름이 지정되지 않은 USB 슬롯이 있습니다 (usb-0:1.3).\n"
                           "   어느 센서인지 모르는 데이터를 기록하지 않기 위해 "
                           "시작하지 않습니다 — ‘슬롯 지정’ 을 누르세요."
                      ).pack(anchor="w")

        # ===== 공통 설정 =====
        cf = ttk.LabelFrame(body, text=" 공통 설정 ")
        cf.grid(row=r, column=0, sticky="ew", padx=12, pady=4); r += 1

        ttk.Label(cf, text="수집 길이").grid(row=0, column=0, sticky="w", padx=10, pady=3)
        df = ttk.Frame(cf); df.grid(row=0, column=1, sticky="w", padx=10, pady=3)
        e = ttk.Entry(df, width=6); e.insert(0, "5"); e.pack(side="left")
        ttk.Label(df, text=" 분   (두 센서 공통 — 트리거는 각자)").pack(side="left")

        ttk.Label(cf, text="포토센서 활성 레벨").grid(row=1, column=0, sticky="w",
                                                padx=10, pady=3)
        al = ttk.Combobox(cf, width=18, state="readonly", values=ACTIVE_LEVELS)
        al.current(0)
        al.grid(row=1, column=1, sticky="w", padx=10, pady=3)

        af = ttk.Frame(cf)
        af.grid(row=2, column=0, columnspan=2, sticky="w", padx=10, pady=(4, 8))
        ttk.Checkbutton(af, text="포토센서로 자동 시작").pack(side="left")
        ttk.Button(af, text="전체 시작", width=10,
                   command=self._noop).pack(side="left", padx=(16, 4))
        ttk.Button(af, text="전체 중지", width=10, command=self._noop,
                   state=("normal" if self.mode == "mixed" else "disabled")).pack(side="left")

        # ===== 저장 =====
        of = ttk.LabelFrame(body, text=" 저장 ")
        of.grid(row=r, column=0, sticky="ew", padx=12, pady=4); r += 1
        ttk.Label(of, text="폴더").grid(row=0, column=0, sticky="w", padx=10, pady=3)
        pf = ttk.Frame(of); pf.grid(row=0, column=1, sticky="w", padx=10, pady=3)
        pe = ttk.Entry(pf, width=30); pe.insert(0, "/home/pi/vibdata"); pe.pack(side="left")
        ttk.Button(pf, text="변경", width=6, command=self._noop).pack(side="left", padx=4)

        ttk.Label(of, text="형식").grid(row=1, column=0, sticky="w", padx=10, pady=3)
        ff = ttk.Frame(of); ff.grid(row=1, column=1, sticky="w", padx=10, pady=3)
        var = tk.StringVar(value="csv")
        for label, val in FORMATS:
            ttk.Radiobutton(ff, text=label, value=val, variable=var).pack(anchor="w")

        # 경로 예시와 용량을 한 줄로 합친다 — 1920x1080 에서 스크롤바 없이
        # 전체가 보이도록 세로를 아끼기 위한 것이다.
        n = len(DEMO[self.mode])
        ttk.Label(of, text="20260923/1/run_0012.csv · 20260923/2/run_0007.csv "
                           "(센서별 폴더·번호)",
                  foreground="#666").grid(row=2, column=0, columnspan=2,
                                          sticky="w", padx=10)
        ttk.Label(of, foreground="#2a7",
                  text="예상 5분 × %d대%s ≈ %s   ·   디스크 여유 87.0 GB  ✓"
                       % (n, " 동시" if n > 1 else "",
                          "1.1 GB" if n == 2 else "560 MB")).grid(
            row=3, column=0, columnspan=2, sticky="w", padx=10, pady=(0, 6))

        # ===== 최근 수집 =====
        rf = ttk.LabelFrame(body, text=" 최근 수집 ")
        rf.grid(row=r, column=0, sticky="ew", padx=12, pady=4); r += 1
        lst = tk.Listbox(rf, height=4, width=58, font=("monospace", 9))
        for line in ("1  run_0012  11:06  5분  정상          1.1 GB",
                     "2  run_0007  09:12  5분  정상          1.1 GB",
                     "1  run_0011  09:02  5분  ⚠ 유실 0.03%  1.1 GB",
                     "2  run_0006  08:51  5분  정상          1.1 GB",
                     ):
            lst.insert("end", line)
        lst.grid(row=0, column=0, sticky="ew", padx=10, pady=(6, 4))
        bf2 = ttk.Frame(rf); bf2.grid(row=1, column=0, sticky="w", padx=10, pady=(0, 8))
        ttk.Button(bf2, text="폴더 열기", command=self._noop).pack(side="left")
        ttk.Button(bf2, text="CSV 로 변환", command=self._noop).pack(side="left", padx=6)

        # ===== 로그 =====
        self.log = tk.Text(body, height=3, width=72, wrap="word")
        self.log.grid(row=r, column=0, padx=12, pady=(6, 4)); r += 1
        self.log.insert("end",
                        "[미리보기] 이 창은 레이아웃 확인용입니다. 센서·GPIO 를 건드리지 않습니다.\n"
                        "실제 동작 시 여기에 채널별 트리거·저장·유실 상황이 표시됩니다.\n")

        # ===== 하단 =====
        foot = ttk.Frame(body)
        foot.grid(row=r, column=0, sticky="ew", padx=12, pady=(0, 10))
        # 도움말을 왼쪽 첫 자리에 둔다 — 데이터 단위·샘플레이트 선택처럼
        # 분석할 때마다 다시 보게 되는 내용이 들어 있다.
        ttk.Button(foot, text="도움말", command=self._open_help).pack(side="left")
        ttk.Button(foot, text="로그 저장",
                   command=self._noop).pack(side="left", padx=6)
        ttk.Label(foot, text="보드레이트 2,000,000 · 프로토콜 v2",
                  foreground="#888").pack(side="right")

        self._autosize()

    # ---- 센서 칸 하나 ----
    def _sensor_card(self, parent, row, spec):
        (name, slot, dev, live, hz, target, fs, mg, din_idx,
         state, pct, note) = spec
        rec = (state == "rec")

        card = ttk.LabelFrame(parent, text=" 센서 %s " % name)
        card.grid(row=row, column=0, sticky="ew", padx=12, pady=3)

        # 1행: 수신 상태
        l1 = ttk.Frame(card)
        l1.grid(row=0, column=0, sticky="w", padx=10, pady=(6, 2))
        ttk.Label(l1, text="●" if live else "○",
                  foreground="#2a7" if live else "#999",
                  font=("", 12)).pack(side="left", padx=(0, 6))
        ttk.Label(l1, text=slot, width=15,
                  font=("monospace", 9)).pack(side="left")
        ttk.Label(l1, text="(%s)" % dev, width=11,
                  foreground="#888").pack(side="left")
        ttk.Label(l1, text="%s / %s Hz" % ("{:,}".format(hz), "{:,}".format(target)),
                  width=17).pack(side="left")
        ttk.Label(l1, text="±%dg" % fs, width=5).pack(side="left")
        # 중력 1g 표시 — 배선·환산이 맞는지 한눈에 보인다 (설계문서 7.3.1)
        okg = 900 <= mg <= 1100
        ttk.Label(l1, text="|a| %d mg %s" % (mg, "✓" if okg else "⚠"),
                  foreground="#2a7" if okg else "#a60").pack(side="left")

        # 2행: 이 센서의 포토센서
        l2 = ttk.Frame(card)
        l2.grid(row=1, column=0, sticky="w", padx=10, pady=2)
        ttk.Label(l2, text="포토센서").pack(side="left")
        dc = ttk.Combobox(l2, width=15, state="readonly", values=DIN_CHOICES)
        dc.current(din_idx)
        dc.pack(side="left", padx=6)
        ttk.Label(l2, text="● %s" % ("LOW — 감지" if rec else "HIGH — 대기"),
                  foreground="#a60" if rec else "#2a7",
                  font=("", 10, "bold")).pack(side="left", padx=8)
        ttk.Button(l2, text="수동 시작", width=10,
                   command=self._noop).pack(side="left", padx=(16, 4))
        ttk.Button(l2, text="중지", width=7, command=self._noop,
                   state=("normal" if rec else "disabled")).pack(side="left")

        # 3행: 이 센서의 진행 상황
        pb = ttk.Progressbar(card, length=560, mode="determinate", value=pct)
        pb.grid(row=2, column=0, sticky="w", padx=10, pady=(3, 1))
        # 높이 1줄 고정 — 수집 시작/종료로 창 높이가 뛰지 않게 한다
        tk.Label(card, text=note, fg="#444" if rec else "#666", height=1,
                 anchor="w", justify="left",
                 font=("monospace", 9)).grid(row=3, column=0, sticky="w",
                                             padx=10, pady=(0, 8))

    # ---- 스크롤 컨테이너 ----
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
        """내용에 맞춰 크기를 정하고 화면 안에 들어오게 한다.

        **핵심은 크기다.** 위 MARGIN_* 주석 참조 — Wayland 에서는 위치 요청이
        무시되므로, 제목줄이 들어갈 자리를 남기고 창을 그만큼 작게 만든다.
        위치는 X11 에서만 의미가 있어 함께 요청하되 의존하지 않는다.
        """
        self.update_idletasks()
        w = self.body.winfo_reqwidth() + 24      # 스크롤바가 나올 경우의 여유
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

    def _open_help(self):
        """도움말은 미리보기에서도 **실제로 연다** — 내용을 확인해야 하기 때문이다."""
        import helpdoc
        try:
            helpdoc.open_help()
            self.log.insert("end", "도움말을 열었습니다: %s\n" % helpdoc.HELP_FILE)
        except helpdoc.HelpError as e:
            self.log.insert("end", "도움말 열기 실패 — %s\n" % e)
        self.log.see("end")

    def _noop(self):
        self.log.insert("end", "[미리보기] 실제 동작은 하지 않습니다.\n")
        self.log.see("end")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "idle"
    Preview(mode if mode in DEMO else "idle").mainloop()
