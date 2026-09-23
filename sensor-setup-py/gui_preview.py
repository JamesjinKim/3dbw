#!/usr/bin/env python3
"""GUI 레이아웃 미리보기 — 실제 tkinter 위젯으로 모습만 확인한다.

디바이스에 아무것도 쓰지 않는다. 버튼을 눌러도 로그에 안내만 찍힌다.
레이아웃·문구·배치를 확정한 뒤 set_sensor_gui.py 에 반영하기 위한 것이다.

실행:
    python3 gui_preview.py
"""

import sys
import tkinter as tk
from tkinter import ttk

# ---- 실제 GUI 와 같은 선택지 (set_sensor_gui.py 와 동일하게 유지) ----
TRANSPORT_OPTIONS = [
    ("WiFi (UDP) · 라즈베리파이로 전송", 0),
    ("USB 직결 (시리얼) · WiFi 미사용", 2),
]
RATE_OPTIONS = [
    ("3.3 kHz · 일반 모니터링 (기본·권장)", 1),
    ("6.6 kHz · 중속 분석", 2),
    ("13.3 kHz · 고속 분석", 3),
    ("26.6 kHz · 정밀 진동분석 (최대)", 4),
    ("1 kHz · 안정 수신 (저부담)", 0),
]
READMODE_OPTIONS = [
    ("자동 (폴링) · 전 속도 검증됨 (기본)", 0),
    ("인터럽트 · INT1 배선 필요", 1),
]
FULLSCALE_OPTIONS = [
    ("±4 g · 일반 모니터링 (기본·권장)", 4),
    ("±2 g · 정밀 측정 (미세 진동)", 2),
    ("±8 g · 강한 진동", 8),
    ("±16 g · 충격·낙하 측정", 16),
]

PAD = {"padx": 12, "pady": 3}


class Preview(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("IIS3DWB 센서 설정 툴 — 레이아웃 미리보기")
        # 제목줄이 어떤 이유로든 가려져도 키보드로 닫을 수 있게 한다.
        for seq in ("<Escape>", "<Control-q>", "<Control-w>"):
            self.bind(seq, lambda _e: self.destroy())
        # 크기는 __init__ 끝에서 내용에 맞춰 자동 결정한다 (_autosize).
        # 고정값을 주면 항목이 잘려 스크롤이 항상 생긴다.

        # ===== 스크롤 컨테이너 =====
        # 스크롤바는 **화면이 부족할 때만** 나타난다 (_sync_scrollbar).
        # 첫 로딩에서는 모든 항목이 보이도록 창을 키우는 것이 기본 동작이다.
        outer = ttk.Frame(self)
        outer.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(outer, highlightthickness=0)
        self.vsb = ttk.Scrollbar(outer, orient="vertical",
                                 command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self._on_yscroll)
        self.canvas.pack(side="left", fill="both", expand=True)
        canvas = self.canvas
        body = ttk.Frame(canvas)
        self.body = body
        self._body_id = canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", self._on_body_configure)
        canvas.bind("<Configure>", self._on_canvas_configure)
        # 휠 스크롤 (스크롤이 필요한 경우에만 의미가 있다)
        for seq, delta in (("<Button-4>", -2), ("<Button-5>", 2)):
            canvas.bind_all(seq, lambda ev, d=delta: self._wheel(d))

        r = 0

        # ===== 헤더: 번들 펌웨어 정보 (manifest.json 에서) =====
        hdr = ttk.Frame(body)
        hdr.grid(row=r, column=0, columnspan=2, sticky="ew", padx=12, pady=(12, 2))
        ttk.Label(hdr, text="IIS3DWB 진동센서 설정",
                  font=("", 15, "bold")).pack(anchor="w")
        ttk.Label(hdr, text="펌웨어 v1.0.0  ·  2026-09-22 20:47  ·  ESP32-S3  ·  IDF v5.4.3",
                  foreground="#555").pack(anchor="w")
        ttk.Label(hdr, text="패키지 0.0.2  ·  무결성 확인됨 ✓",
                  foreground="#2a7").pack(anchor="w")
        r += 1
        ttk.Separator(body, orient="horizontal").grid(
            row=r, column=0, columnspan=2, sticky="ew", pady=8); r += 1

        # ===== 포트 =====
        ttk.Label(body, text="USB 포트").grid(row=r, column=0, sticky="w", **PAD)
        pf = ttk.Frame(body); pf.grid(row=r, column=1, sticky="w", **PAD)
        self.port = ttk.Combobox(pf, width=22, state="readonly",
                                 values=["/dev/ttyACM0"])
        self.port.current(0); self.port.pack(side="left")
        ttk.Button(pf, text="⟳", width=3, command=self._noop).pack(side="left", padx=4)
        r += 1
        bf = ttk.Frame(body); bf.grid(row=r, column=1, sticky="w", padx=12, pady=(0, 4))
        ttk.Button(bf, text="디바이스 확인", command=self._noop).pack(side="left")
        ttk.Button(bf, text="현재 설정 읽기", command=self._noop).pack(side="left", padx=6)
        r += 1
        ttk.Label(body, text="‘현재 설정 읽기’ 를 누르면 디바이스에 저장된 값을 아래에 채웁니다.\n"
                             "값이 없는 항목은 비워 두고 직접 입력하시면 됩니다.",
                  foreground="#666", justify="left").grid(
            row=r, column=0, columnspan=2, sticky="w", padx=12); r += 1

        ttk.Separator(body, orient="horizontal").grid(
            row=r, column=0, columnspan=2, sticky="ew", pady=8); r += 1

        # ===== 전송 방식 (설계문서 6.1 — 1단계 최상단) =====
        ttk.Label(body, text="데이터 전송 방식",
                  font=("", 10, "bold")).grid(row=r, column=0, sticky="w", **PAD)
        self.tr = ttk.Combobox(body, width=30, state="readonly",
                               values=[l for l, _ in TRANSPORT_OPTIONS])
        self.tr.current(0)
        self.tr.grid(row=r, column=1, sticky="w", **PAD)
        self.tr.bind("<<ComboboxSelected>>", lambda e: self._on_transport())
        r += 1
        # 높이 3줄 고정: 전송 방식을 바꿀 때 안내 문구 줄 수가 달라져도
        # 폼 전체 높이가 변하지 않게 한다 (창이 흔들리거나 스크롤바가
        # 생겼다 사라지는 것을 막는다).
        self.tr_hint = tk.Label(body, text="", fg="#666", height=3,
                                wraplength=470, justify="left", anchor="nw")
        self.tr_hint.grid(row=r, column=0, columnspan=2, sticky="w", padx=12); r += 1

        # ===== WiFi / 서버 (USB 직결에서는 비활성) =====
        self.wifi_frame = ttk.LabelFrame(body, text=" WiFi 전송 설정 ")
        self.wifi_frame.grid(row=r, column=0, columnspan=2,
                             sticky="ew", padx=12, pady=6); r += 1
        self.wifi_widgets = []
        for i, (label, val, show) in enumerate([
                ("WiFi 이름 (SSID)", "shinho2.4G", None),
                ("WiFi 비밀번호", "shinho1234", "*"),
                ("라즈베리파이 IP", "192.168.0.77", None),
                ("수신 포트", "9000", None)]):
            ttk.Label(self.wifi_frame, text=label).grid(
                row=i, column=0, sticky="w", padx=10, pady=3)
            e = ttk.Entry(self.wifi_frame, width=26, show=show)
            e.insert(0, val)
            e.grid(row=i, column=1, sticky="w", padx=10, pady=3)
            self.wifi_widgets.append(e)

        # ===== 측정 설정 (전송 방식과 무관하게 항상 표시) =====
        meas = ttk.LabelFrame(body, text=" 측정 설정 ")
        meas.grid(row=r, column=0, columnspan=2, sticky="ew", padx=12, pady=6); r += 1
        for i, (label, opts) in enumerate([
                ("측정 속도", RATE_OPTIONS),
                ("데이터 읽기 방식", READMODE_OPTIONS),
                ("측정 범위 (풀스케일)", FULLSCALE_OPTIONS)]):
            ttk.Label(meas, text=label).grid(row=i, column=0, sticky="w",
                                             padx=10, pady=3)
            cb = ttk.Combobox(meas, width=30, state="readonly",
                              values=[l for l, _ in opts])
            cb.current(0)
            cb.grid(row=i, column=1, sticky="w", padx=10, pady=3)
        ttk.Label(meas, text="측정 범위를 넘는 진동은 잘려서 기록됩니다(클리핑).",
                  foreground="#666").grid(row=3, column=0, columnspan=2,
                                          sticky="w", padx=10, pady=(0, 6))

        # ===== 실행 =====
        ttk.Separator(body, orient="horizontal").grid(
            row=r, column=0, columnspan=2, sticky="ew", pady=8); r += 1
        act = ttk.Frame(body)
        act.grid(row=r, column=0, columnspan=2, pady=(2, 4)); r += 1
        ttk.Button(act, text="①  펌웨어 굽기", width=18,
                   command=self._noop).pack(side="left", padx=6)
        ttk.Button(act, text="②  설정 주입", width=18,
                   command=self._noop).pack(side="left", padx=6)
        ttk.Button(act, text="취소", width=8,
                   command=self._noop).pack(side="left", padx=6)

        self.pbar = ttk.Progressbar(body, length=460, mode="determinate", value=0)
        self.pbar.grid(row=r, column=0, columnspan=2, padx=12, pady=(6, 2)); r += 1
        self.status = ttk.Label(body, text="대기 중", foreground="#444")
        self.status.grid(row=r, column=0, columnspan=2, sticky="w", padx=12); r += 1

        # ===== 로그 =====
        self.log = tk.Text(body, height=7, width=62, wrap="word")
        self.log.grid(row=r, column=0, columnspan=2, padx=12, pady=(8, 4)); r += 1
        self.log.insert("end",
            "[미리보기] 이 창은 레이아웃 확인용입니다. 디바이스에 아무것도 쓰지 않습니다.\n"
            "실제 동작 시 여기에 esptool 진행 상황과 부팅 로그 판정이 표시됩니다.\n")

        # ===== 하단: 공장 초기화 (설계문서 6.3 — 작은 링크) =====
        foot = ttk.Frame(body)
        foot.grid(row=r, column=0, columnspan=2, sticky="ew", padx=12, pady=(0, 14))
        self.remember = tk.BooleanVar(value=False)
        ttk.Checkbutton(foot, text="비밀번호 기억", variable=self.remember).pack(side="left")
        ttk.Button(foot, text="로그 저장", command=self._noop).pack(side="left", padx=8)
        fr = ttk.Label(foot, text="공장 초기화", foreground="#a33", cursor="hand2")
        fr.pack(side="right")
        fr.bind("<Button-1>", lambda e: self._noop())

        self._on_transport()
        self._autosize()

    # ---- 스크롤 컨테이너 동작 ----
    def _on_body_configure(self, _e=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self._sync_scrollbar()

    def _on_canvas_configure(self, e):
        # 내부 프레임 폭을 캔버스 폭에 맞춰 가로 잘림을 막는다
        self.canvas.itemconfigure(self._body_id, width=e.width)
        self._sync_scrollbar()

    def _on_yscroll(self, first, last):
        self.vsb.set(first, last)
        self._sync_scrollbar()

    def _sync_scrollbar(self):
        """내용이 창보다 클 때만 스크롤바를 보인다."""
        need = self.body.winfo_reqheight() > self.canvas.winfo_height() + 1
        if need and not self.vsb.winfo_ismapped():
            self.vsb.pack(side="right", fill="y", before=self.canvas)
        elif not need and self.vsb.winfo_ismapped():
            self.vsb.pack_forget()
            self.canvas.yview_moveto(0)

    def _wheel(self, delta):
        if self.body.winfo_reqheight() > self.canvas.winfo_height():
            self.canvas.yview_scroll(delta, "units")

    # 화면 위아래로 비워 둘 공간.
    #
    # 라즈베리파이 데스크톱은 **labwc(Wayland)** 이고 tkinter 는 XWayland 로 뜬다.
    # 이 환경에서 클라이언트가 요청한 창 위치는 **컴포지터가 무시한다.** 그래서
    # 좌표를 중앙으로 계산해도 제목줄이 화면 위로 숨는 것을 막을 수 없다.
    #
    # 확실한 방법은 **위치가 아니라 크기로 보장**하는 것이다. 상단 패널(약 36px)과
    # 창 제목줄(약 40px), 하단 여유를 빼고 남는 만큼만 창을 키우면, 컴포지터가
    # 어디에 놓든 제목줄과 닫기 버튼이 화면 안에 들어온다.
    MARGIN_TOP = 96        # 상단 패널 + 제목줄
    MARGIN_BOTTOM = 64     # 하단 여유

    def _autosize(self):
        """내용에 맞춰 크기를 정하고 화면 중앙에 배치한다.

        위치를 지정하지 않으면 창 관리자가 좌측 상단에 놓고, 창이 화면보다 높으면
        제목줄이 화면 밖으로 나간다. 좌표를 직접 계산해 그 상황을 막는다.
        """
        self.update_idletasks()
        w = self.body.winfo_reqwidth() + 24    # 스크롤바가 나올 경우의 여유 폭
        h = self.body.winfo_reqheight() + 8
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()

        avail_h = max(320, sh - self.MARGIN_TOP - self.MARGIN_BOTTOM)
        w = min(w, int(sw * 0.95))
        h = min(h, avail_h)

        x = max(0, (sw - w) // 2)
        y = self.MARGIN_TOP + max(0, (avail_h - h) // 2)
        self.geometry("%dx%d+%d+%d" % (w, h, x, y))
        # 너무 작게 줄이면 폼이 깨지므로 하한만 둔다 (그 아래로는 스크롤 사용)
        self.minsize(min(w, 460), 400)
        self.update_idletasks()
        self._sync_scrollbar()

    # ---- 전송 방식에 따라 WiFi 입력란을 전환 (설계문서 6.1 + 4.2) ----
    def _on_transport(self):
        transport = TRANSPORT_OPTIONS[self.tr.current()][1]
        if transport == 2:
            # 설계문서 6.1: USB 직결에서는 WiFi/서버 입력란을 감춘다.
            # 단 4.2: 값은 보존한다 → 지우지 않고 비활성화만 한다.
            for w in self.wifi_widgets:
                w.configure(state="disabled")
            self.wifi_frame.configure(text=" WiFi 전송 설정 (USB 직결에서는 사용 안 함) ")
            self.tr_hint.configure(
                text="💡 USB 케이블로 연결된 PC가 데이터를 받습니다 (최대 26.6 kHz).\n"
                     "⚠️ 전송 중에는 디바이스 로그가 표시되지 않습니다.\n"
                     "수신:  rpi-collector/collect_cli.py --auto",
                fg="#8a5a00")
        else:
            for w in self.wifi_widgets:
                w.configure(state="normal")
            self.wifi_frame.configure(text=" WiFi 전송 설정 ")
            self.tr_hint.configure(
                text="센서가 WiFi에 접속해 아래 IP·포트로 데이터를 보냅니다.\n"
                     "2.4GHz 전용 — 5GHz 네트워크에는 접속하지 못합니다.\n"
                     "수신:  rpi-collector/udp_receiver.py",
                fg="#666")

    def _noop(self):
        self.log.insert("end", "[미리보기] 실제 동작은 하지 않습니다.\n")
        self.log.see("end")


if __name__ == "__main__":
    app = Preview()
    # 스크린샷용: 인자 usb 를 주면 USB 직결 모드로 시작한다
    if len(sys.argv) > 1 and sys.argv[1] == "usb":
        app.tr.current(1)
        app._on_transport()
    app.mainloop()
