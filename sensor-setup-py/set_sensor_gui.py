#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SHT 진동센서 설정 (tkinter GUI) — 공식 명칭. 예전 이름: IIS3DWB 센서 설정 툴

한 창에서 아래를 순서대로 처리한다.

  ①  펌웨어 굽기   번들된 배포 펌웨어 3종을 디바이스에 쓰고 부팅을 확인
  ②  설정 주입     입력값으로 NVS(0x9000)를 만들어 주입하고 되울린 값을 대조

부가 기능: 현재 설정 읽기 · 공장 초기화 · 로그 저장

요구 환경: 라즈베리파이(또는 리눅스) · Python 3.8+ · tkinter · esptool
           (nvs_gen.py 가 순수 파이썬이라 NVS 생성에는 ESP-IDF 가 필요 없다)

펌웨어와 반드시 일치해야 하는 값 (수정 금지):
  - NVS namespace = "devcfg"
  - NVS 파티션 offset = 0x9000, size = 0x6000
  - 키: wifi_ssid, wifi_pass, srv_ip, srv_port(u16), stream_rate(u8),
        transport(u8), read_mode(u8), full_scale_g(u8)
"""

import json
import os
import queue
import re
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
# 배포 패키지는 pyserial·esptool 을 vendor/ 에 소스째 넣어 온다 (폐쇄망 대비).
# 시스템에 설치된 것보다 먼저 잡히도록 앞에 넣는다.
if (HERE / "vendor").is_dir():
    sys.path.insert(0, str(HERE / "vendor"))

try:
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog
except ImportError as _e:
    # tkinter 는 파이썬 소스가 아니라 OS 의 Tk 라이브러리에 묶여 있어 패키지에
    # 넣어 갈 수 없다. Raspberry Pi OS **Lite**(화면 없는 이미지)에는 빠져 있다.
    sys.exit("❌ 화면(GUI) 라이브러리 tkinter 가 이 OS 에 없습니다 (%s).\n"
             "   Raspberry Pi OS Desktop 이미지에는 기본으로 들어 있습니다.\n"
             "   Lite 이미지라면 python3-tk 패키지를 오프라인으로 설치해야 합니다." % _e)

import nvs_gen          # 순수 파이썬 NVS 생성기 (Rust nvs.rs 와 byte-exact 검증됨)
import esp_flash        # esptool 래퍼 (스텁 누락 우회·스톨 감지 포함)
import fw_manifest      # 번들 펌웨어 manifest 파싱·무결성 검증
import nvs_read         # 디바이스의 현재 NVS 를 읽어 파싱
import ports as ports_mod   # USB 물리 슬롯 기준 보드 열거 (boardcheck 와 공용)
import provision_log    # 어느 보드(MAC)에 무엇을 넣었는지 이력
import boot_log         # 부팅 로그 판정
import usb_reset        # USB 스톨 복구

# ===================== 펌웨어와 일치해야 하는 상수 =====================
NVS_NAMESPACE = "devcfg"
NVS_OFFSET = 0x9000
NVS_SIZE = 0x6000          # 24576 bytes

# 워커 스레드 메시지를 메인 스레드가 꺼내가는 주기(ms).
# 80ms 면 사람 눈에는 즉시로 보이고, 유휴 시 CPU 부담도 없다.
PUMP_MS = 80

# 입력값 기억 파일 (이 PC 사용자 홈) — 여러 센서 연속 세팅 편의
PREFS_PATH = Path.home() / ".iis3dwb_sensor_setup.json"
PREFS_SCHEMA = 2           # 2부터 비밀번호는 "기억" 체크 시에만 저장한다

PAD = {"padx": 12, "pady": 3}

# 로그창에 유지할 최대 줄 수 (넘으면 오래된 줄부터 버린다)
LOG_MAX_LINES = 2000

# esptool 진행률 줄 — "4096 (16 %)" 또는 그것이 여러 개 이어붙은 형태.
# 진행률 막대가 따로 있으므로 로그창에는 띄우지 않는다.
PROGRESS_RE = re.compile(r"(?:\d+\s*\(\s*\d+\s*%\)\s*)+")

# ---- 선택지 (화면 문구는 help.html 과 맞춰 둔다) ----
TRANSPORT_OPTIONS = [
    ("WiFi (UDP) · 라즈베리파이로 전송", 0),
    ("USB 직결 (시리얼) · WiFi 미사용", 2),
]
# 기본은 1 kHz — 데이터량을 줄이기 위한 결정 (2026-09-26). 펌웨어도 NVS 에 값이
# 없으면 1 kHz 다 (config_manager.c). 주파수 분석이 필요하면 현장에서 26.6 kHz 를
# 고른다 — 낮은 레이트는 필터 없이 솎아 에일리어싱이 생긴다 (help.html 5장).
RATE_OPTIONS = [
    ("1 kHz · 데이터량 최소 (기본)", 0),
    ("3.3 kHz · 일반 모니터링", 1),
    ("6.6 kHz · 중속 분석", 2),
    ("13.3 kHz · 고속 분석", 3),
    ("26.6 kHz · 주파수 분석(FFT) · 최대", 4),
]
# 읽기 방식: 인터럽트가 기본, 폴링은 선택 (사용자 결정).
# boardcheck 로 INT1 배선이 확인된 보드에서는 인터럽트가 26.6kHz 전 속도를 낸다.
READMODE_OPTIONS = [
    ("인터럽트 · 26.6 kHz 전 속도 (기본·권장)", 1),
    ("폴링 · INT1 배선 문제 시 대안", 0),
]
FULLSCALE_OPTIONS = [
    ("±4 g · 일반 모니터링 (기본·권장)", 4),
    ("±2 g · 정밀 측정 (미세 진동)", 2),
    ("±8 g · 강한 진동", 8),
    ("±16 g · 충격·낙하 측정", 16),
]


# ===================== 번들 펌웨어 찾기 =====================
def find_package_dir():
    """배포 패키지 루트를 찾는다. 없으면 None.

    `fw_manifest.load_manifest()` 와 `esp_flash.flash_firmware()` 는 둘 다
    **패키지 루트**를 받는다 (그 안의 firmware/manifest.json 을 스스로 찾는다).
    firmware/ 자체를 넘기면 firmware/firmware/... 를 찾다 실패한다.

    두 가지 배치를 모두 지원한다:
      · 배포 패키지    — 이 스크립트가 패키지 루트에 있다
      · 저장소 체크아웃 — ../dist/<패키지>/ 중 가장 최근 것
    """
    cands = [HERE]
    dist = HERE.parent / "dist"
    if dist.is_dir():
        cands += sorted((p for p in dist.iterdir() if p.is_dir()),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    for c in cands:
        if (c / "firmware" / "manifest.json").is_file():
            return c
    return None


# ===================== NVS 생성 =====================
def build_nvs_bin(cfg, out_bin):
    """NVS 바이너리(devcfg)를 파이썬에서 직접 생성해 out_bin 에 쓴다."""
    data = nvs_gen.generate_full_nvs(
        NVS_NAMESPACE,
        cfg["ssid"], cfg["pw"], cfg["srv_ip"], int(cfg["srv_port"]),
        int(cfg["rate"]), int(cfg["transport"]), int(cfg["read_mode"]),
        NVS_SIZE,
        full_scale_g=int(cfg["full_scale_g"]),
    )
    with open(out_bin, "wb") as f:
        f.write(data)


def build_blank_nvs(out_bin):
    """공장 초기화용 빈 NVS.

    지워진 플래시는 전부 0xFF 이고, NVS 는 그 상태를 "미초기화" 로 보고 첫
    사용 시 스스로 포맷한다. 그래서 0xFF 로 덮어쓰는 것이 지우는 것과 같다.
    검증된 주입 경로(write_flash)를 그대로 쓰므로 erase_region 보다 안전하다.
    """
    with open(out_bin, "wb") as f:
        f.write(b"\xFF" * NVS_SIZE)


# (포트 열거는 ports.py 로 옮겼다 — boardcheck 와 같은 구현을 쓴다.
#  by-id 를 쓰면 보드 2대가 1개로 보여 한 대를 놓친다. 그 판단을 한 곳에만 둔다.)


# ===================== 입력값 기억 =====================
def load_prefs():
    try:
        if PREFS_PATH.exists():
            d = json.loads(PREFS_PATH.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                return d
    except Exception:
        pass
    return {}


def save_prefs(cfg, remember_password):
    """입력값 저장. 비밀번호는 사용자가 "기억" 을 켠 경우에만 남긴다.

    schema 1 로 저장된 기존 파일에는 비밀번호가 평문으로 들어 있다. 체크가
    꺼져 있으면 이번 저장에서 그 값을 **지운다** (마이그레이션).
    파일 권한은 0600 으로 좁힌다 — 같은 PC 의 다른 계정이 읽지 못하게.
    """
    out = {k: v for k, v in cfg.items() if k != "pw"}
    out["schema"] = PREFS_SCHEMA
    out["remember_password"] = bool(remember_password)
    if remember_password:
        out["pw"] = cfg.get("pw", "")
    try:
        PREFS_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2),
                              encoding="utf-8")
        os.chmod(PREFS_PATH, 0o600)
    except Exception:
        pass  # 저장 실패해도 기능엔 영향 없음


class StepError(Exception):
    """한 단계가 실패한 이유. 제목·본문을 그대로 사용자에게 보인다.

    순차 진행에서 **다음 보드로 넘어가지 않고 멈추는** 신호로도 쓰인다.
    `warn=True` 는 "치명적이진 않으나 확인이 필요" 를 뜻한다.
    """

    def __init__(self, title, body, warn=False):
        super().__init__(body)
        self.title = title
        self.body = body
        self.warn = warn


# ===================== GUI =====================
class SetupApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("SHT 진동센서 설정")
        # 제목줄이 어떤 이유로든 가려져도 키보드로 닫을 수 있게 한다.
        for seq in ("<Escape>", "<Control-q>", "<Control-w>"):
            self.bind(seq, lambda _e: self.destroy())

        self.pkg_dir = find_package_dir()
        self.manifest = None
        self.manifest_error = None
        if self.pkg_dir:
            try:
                self.manifest = fw_manifest.load_manifest(str(self.pkg_dir))
            except Exception as e:
                self.manifest_error = str(e)

        prefs = load_prefs()
        self._cancel = threading.Event()
        self._busy = False
        self._q = queue.Queue()

        self._build_ui(prefs)
        self.refresh_ports()
        self._on_transport()
        self._autosize()
        self.after(PUMP_MS, self._pump)

    # ------------------------------------------------------------------
    # 화면 구성
    # ------------------------------------------------------------------
    def _build_ui(self, prefs):
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
        body = ttk.Frame(self.canvas)
        self.body = body
        self._body_id = self.canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", self._on_body_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        for seq, delta in (("<Button-4>", -2), ("<Button-5>", 2)):
            self.canvas.bind_all(seq, lambda ev, d=delta: self._wheel(d))

        r = 0

        # ===== 헤더 =====
        # 도움말을 **맨 위 오른쪽**에 둔다. 시작하기 전에 읽어야 하는 것이라
        # 화면 아래에 있으면 눈에 들어오지 않는다. 화면의 안내 문구는 최소로
        # 줄이고 자세한 설명은 전부 도움말로 넘겼다.
        hdr = ttk.Frame(body)
        hdr.grid(row=r, column=0, columnspan=2, sticky="ew", padx=12, pady=(10, 2))
        ttk.Button(hdr, text="도움말 — 먼저 읽어보세요",
                   command=self.on_help).pack(side="right", anchor="n")
        left = ttk.Frame(hdr); left.pack(side="left", anchor="w")
        ttk.Label(left, text="SHT 진동센서 설정",
                  font=("", 15, "bold")).pack(anchor="w")
        if self.manifest:
            ttk.Label(left, text="펌웨어 %s · 패키지 %s"
                                 % (self.manifest.get("app_version", "?"),
                                    self.manifest.get("package_version", "?")),
                      foreground="#555").pack(anchor="w")
        else:
            ttk.Label(left, text="⚠ 번들 펌웨어 없음 — ① 펌웨어 굽기 사용 불가",
                      foreground="#a33").pack(anchor="w")
            ttk.Label(left, text=self.manifest_error or
                      "firmware/manifest.json 을 찾지 못했습니다",
                      foreground="#666", wraplength=430,
                      justify="left").pack(anchor="w")
        r += 1
        ttk.Separator(body, orient="horizontal").grid(
            row=r, column=0, columnspan=2, sticky="ew", pady=8); r += 1

        # ===== 진동센서 목록 =====  (화면 이름. 예전: 대상 보드)
        #
        # 예전에는 포트 콤보박스 하나(`/dev/ttyACM0`)였다. 보드를 두 대 꽂으면
        # **작업자가 어느 보드에 굽는지 확신할 수 없다** — 브리지칩에 고유
        # 일련번호가 없어 포트 번호만으로는 보드를 특정하지 못하기 때문이다.
        # 그래서 목록으로 바꾸고 MAC·USB 슬롯·직전 작업 이력을 함께 보여준다.
        bl = ttk.LabelFrame(body, text=" 진동센서 목록 ")
        bl.grid(row=r, column=0, columnspan=2, sticky="ew", padx=12, pady=(0, 4))
        r += 1

        btop = ttk.Frame(bl)
        btop.grid(row=0, column=0, sticky="ew", padx=10, pady=(6, 2))
        self.find_btn = ttk.Button(btop, text="MAC 주소 확인",
                                   command=self.on_find_boards)
        self.find_btn.pack(side="left")
        self.refresh_btn = ttk.Button(btop, text="목록만 갱신", width=12,
                                      command=self.refresh_ports)
        self.refresh_btn.pack(side="left", padx=6)

        cols = ("slot", "dev", "mac", "state")
        self.tree = ttk.Treeview(bl, columns=cols, show="headings", height=3,
                                 selectmode="browse")
        for c, t, w in (("slot", "USB 포트", 110), ("dev", "장치", 90),
                        ("mac", "MAC", 160), ("state", "이력", 190)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w", stretch=False)
        self.tree.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 4))
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._on_board_select())
        self.boards = []            # ports.Board 목록 (표시 순서와 같다)

        # 1줄 고정 — 대상 MAC 은 2대 작업에서 필수 정보라 남기고, 나머지 설명은 뺐다
        self.board_hint = tk.Label(bl, text="", fg="#555", height=1, anchor="w")
        self.board_hint.grid(row=2, column=0, sticky="w", padx=10, pady=(0, 6))

        bf = ttk.Frame(bl)
        bf.grid(row=3, column=0, sticky="w", padx=10, pady=(0, 8))
        # '디바이스 확인' 버튼은 뺐다 (2026-09-26) — MAC 주소 확인와 같은 esptool 호출이라
        # 둘 다 눌러야 하는지 헷갈렸다. 칩·플래시 정보는 MAC 주소 확인가 로그에 남긴다.
        self.read_btn = ttk.Button(bf, text="현재 설정 읽기",
                                   command=self.on_read_settings)
        self.read_btn.pack(side="left")

        ttk.Separator(body, orient="horizontal").grid(
            row=r, column=0, columnspan=2, sticky="ew", pady=8); r += 1

        # ===== 전송 방식 =====
        ttk.Label(body, text="데이터 전송 방식",
                  font=("", 10, "bold")).grid(row=r, column=0, sticky="w", **PAD)
        self.tr_cb = ttk.Combobox(body, width=30, state="readonly",
                                  values=[l for l, _ in TRANSPORT_OPTIONS])
        self.tr_cb.current(self._index_by_value(TRANSPORT_OPTIONS,
                                                prefs.get("transport", 0)))
        self.tr_cb.grid(row=r, column=1, sticky="w", **PAD)
        self.tr_cb.bind("<<ComboboxSelected>>", lambda e: self._on_transport())
        r += 1
        # 예전에는 여기 '수신: 수집기 패키지의 …' 한 줄이 있었다. 설정툴과 수집기는
        # 별개 배포물이라 뺐다 (2026-09-27). 받는 방법은 수집기 패키지의 도움말에 있다.

        # ===== WiFi / 서버 =====
        self.wifi_frame = ttk.LabelFrame(body, text=" WiFi 전송 설정 ")
        self.wifi_frame.grid(row=r, column=0, columnspan=2,
                             sticky="ew", padx=12, pady=6); r += 1
        self.ssid = tk.StringVar(value=prefs.get("ssid", ""))
        self.pw = tk.StringVar(value=prefs.get("pw", ""))
        self.srv_ip = tk.StringVar(value=prefs.get("srv_ip", ""))
        self.srv_port = tk.StringVar(value=str(prefs.get("srv_port", "9000")))
        self.wifi_widgets = []
        for i, (label, var, show) in enumerate([
                ("WiFi 이름 (SSID)", self.ssid, None),
                ("WiFi 비밀번호", self.pw, "*"),
                ("라즈베리파이 IP", self.srv_ip, None),
                ("수신 포트", self.srv_port, None)]):
            ttk.Label(self.wifi_frame, text=label).grid(
                row=i, column=0, sticky="w", padx=10, pady=3)
            e = ttk.Entry(self.wifi_frame, textvariable=var, width=26, show=show)
            e.grid(row=i, column=1, sticky="w", padx=10, pady=3)
            self.wifi_widgets.append(e)
        # 비밀번호 기억 — WiFi 비밀번호에만 해당하므로 그 입력란 바로 옆에 둔다.
        # (예전에는 맨 아래 '로그 저장' 옆에 있어 무엇을 기억하는지 알기 어려웠다.)
        # USB 직결이면 WiFi 입력란과 함께 비활성화된다.
        self.remember = tk.BooleanVar(value=bool(prefs.get("remember_password", False)))
        cb = ttk.Checkbutton(self.wifi_frame, text="비밀번호 기억", variable=self.remember)
        cb.grid(row=1, column=2, sticky="w", padx=(0, 10), pady=3)
        self.wifi_widgets.append(cb)

        # ===== 측정 설정 =====
        meas = ttk.LabelFrame(body, text=" 측정 설정 ")
        meas.grid(row=r, column=0, columnspan=2, sticky="ew", padx=12, pady=6); r += 1
        self.rate_cb = self._combo(meas, 0, "측정 속도", RATE_OPTIONS,
                                   prefs.get("rate", 0))
        self.rm_cb = self._combo(meas, 1, "데이터 읽기 방식", READMODE_OPTIONS,
                                 prefs.get("read_mode", 1))
        self.fs_cb = self._combo(meas, 2, "측정 범위 (풀스케일)", FULLSCALE_OPTIONS,
                                 prefs.get("full_scale_g", 4))

        # ===== 실행 =====
        ttk.Separator(body, orient="horizontal").grid(
            row=r, column=0, columnspan=2, sticky="ew", pady=8); r += 1
        act = ttk.Frame(body)
        act.grid(row=r, column=0, columnspan=2, pady=(2, 4)); r += 1
        self.flash_btn = ttk.Button(act, text="①  펌웨어 굽기", width=18,
                                    command=self.on_flash_firmware)
        self.flash_btn.pack(side="left", padx=6)
        if not self.manifest:
            self.flash_btn.configure(state="disabled")
        self.inject_btn = ttk.Button(act, text="②  설정 주입", width=18,
                                     command=self.on_inject)
        self.inject_btn.pack(side="left", padx=6)
        # '전체 순차' 버튼은 뺐다 (2026-09-26, 사용자 결정) — 여러 대를 한 버튼으로
        # 처리하면 어느 보드가 진행 중인지 헷갈린다. 항상 선택한 1대에 ① → ② 를 한다.
        self.cancel_btn = ttk.Button(act, text="취소", width=8,
                                     command=self.on_cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=6)

        self.pbar = ttk.Progressbar(body, length=460, mode="determinate", value=0)
        self.pbar.grid(row=r, column=0, columnspan=2, padx=12, pady=(6, 2)); r += 1
        self.status = ttk.Label(body, text="대기 중", foreground="#444")
        self.status.grid(row=r, column=0, columnspan=2, sticky="w", padx=12); r += 1

        # ===== 로그 =====
        self.log = tk.Text(body, height=7, width=62, wrap="word", state="disabled")
        self.log.grid(row=r, column=0, columnspan=2, padx=12, pady=(8, 4)); r += 1

        # ===== 하단 =====
        foot = ttk.Frame(body)
        foot.grid(row=r, column=0, columnspan=2, sticky="ew", padx=12, pady=(0, 14))
        ttk.Button(foot, text="로그 저장", command=self.on_save_log).pack(side="left")
        self.factory_lbl = ttk.Label(foot, text="공장 초기화", foreground="#a33",
                                     cursor="hand2")
        self.factory_lbl.pack(side="right")
        self.factory_lbl.bind("<Button-1>", lambda e: self.on_factory_reset())

    def _combo(self, parent, row, label, options, value):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w",
                                           padx=10, pady=3)
        cb = ttk.Combobox(parent, width=30, state="readonly",
                          values=[l for l, _ in options])
        cb.current(self._index_by_value(options, value))
        cb.grid(row=row, column=1, sticky="w", padx=10, pady=3)
        return cb

    @staticmethod
    def _index_by_value(options, value):
        for i, (_, v) in enumerate(options):
            if v == value:
                return i
        return 0

    # ------------------------------------------------------------------
    # 스크롤 컨테이너 동작
    # ------------------------------------------------------------------
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
        """내용에 맞춰 크기를 정하고 **화면 중앙에 배치**한다.

        위치를 지정하지 않으면 창 관리자가 좌측 상단(+0+0)에 놓는데, 창이 화면보다
        높으면 제목줄이 화면 밖으로 밀려 마우스로 닫을 수 없다. 그래서 높이를
        장식 높이까지 감안해 제한하고 좌표를 직접 계산한다.
        화면보다 내용이 크면 그때만 스크롤바가 생긴다.
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
        self.minsize(min(w, 460), 400)
        self.update_idletasks()
        self._sync_scrollbar()

    # ------------------------------------------------------------------
    # 워커 스레드 ↔ 메인 스레드
    #
    # tkinter 위젯은 **mainloop 를 돌리는 스레드에서만** 만질 수 있다. 예전에는
    # 워커가 logln()·messagebox·configure 를 직접 불렀는데, 이는 라이브러리 규칙
    # 위반이라 평소엔 되는 듯 보이다가 타이밍에 따라 창이 굳거나 Tcl 오류로 죽는다.
    # 재현이 불규칙해 원인을 찾기 어려운 종류의 고장이다.
    #
    # 그래서 워커는 큐에 **메시지만** 넣고, 메인 스레드가 주기적으로 꺼내 처리한다.
    # 워커에서 위젯을 만지는 코드는 하나도 없어야 한다.
    # ------------------------------------------------------------------
    def _post(self, kind, *args):
        """워커 스레드에서 호출한다. 위젯을 만지지 않고 큐에만 넣는다."""
        self._q.put((kind, args))

    def _pump(self):
        """메인 스레드에서 주기적으로 큐를 비운다."""
        try:
            while True:
                kind, args = self._q.get_nowait()
                self._handle(kind, args)
        except queue.Empty:
            pass
        except Exception as e:                      # 처리 중 오류가 나도
            try:                                    # 펌프 자체는 살려 둔다
                self.logln("⚠ 화면 갱신 중 오류: %s" % e)
            except Exception:
                pass
        finally:
            # 여기서 멈추면 이후 모든 진행 상황이 화면에 나타나지 않아
            # "멈춘 것처럼" 보인다. 어떤 경우에도 다음 주기를 예약한다.
            self.after(PUMP_MS, self._pump)

    def _handle(self, kind, args):
        if kind == "log":
            self.logln(args[0])
        elif kind == "status":
            self.status.configure(text=args[0])
        elif kind == "progress":
            self.pbar.configure(value=args[0])
        elif kind == "busy":
            self._set_busy(args[0])
        elif kind == "info":
            messagebox.showinfo(args[0], args[1])
        elif kind == "warn":
            messagebox.showwarning(args[0], args[1])
        elif kind == "error":
            messagebox.showerror(args[0], args[1])
        elif kind == "prefill":
            self._apply_prefill(args[0])
        elif kind == "boards":
            self._apply_macs(args[0])
        elif kind == "board_row":
            self._show_board_row(*args)
        elif kind == "ask":
            # 워커가 답을 기다리고 있다. 반드시 event 를 set 해야 한다.
            title, msg, holder, ev = args
            try:
                holder["answer"] = messagebox.askyesno(title, msg)
            finally:
                ev.set()

    def _ask(self, title, msg):
        """워커 스레드에서 사용자에게 예/아니오를 묻는다 (대화상자는 메인에서)."""
        holder, ev = {}, threading.Event()
        self._post("ask", title, msg, holder, ev)
        ev.wait()
        return bool(holder.get("answer"))

    def logln(self, msg):
        """로그 한 줄 추가. **메인 스레드 전용.**"""
        if threading.current_thread() is not threading.main_thread():
            # 조용히 넘어가면 규칙 위반이 다시 스며든다. 바로 드러나게 한다.
            raise RuntimeError(
                "logln() 은 메인 스레드에서만 호출해야 합니다. "
                "워커 스레드에서는 self._post('log', msg) 를 쓰세요.")
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n")
        # 로그가 무한히 쌓이면 Text 위젯이 느려진다. 최근 것만 남긴다.
        lines = int(self.log.index("end-1c").split(".")[0])
        if lines > LOG_MAX_LINES:
            self.log.delete("1.0", "%d.0" % (lines - LOG_MAX_LINES + 1))
        self.log.see("end")
        self.log.configure(state="disabled")

    # ---- 디바이스 출력 표시 필터 ----
    #
    # 판정은 항상 **원문**으로 한다 (boot_log 가 직접 읽는다). 아래 필터는
    # 화면에 뿌릴지 말지만 정한다 — 걸러진 줄 때문에 판정이 달라지지 않는다.

    @staticmethod
    def _is_noise(line):
        """로그창에 띄우지 않을 줄인가.

        두 가지를 거른다:
          · 바이너리 — 시리얼 스트리밍(transport=2)이 시작되면 같은 포트로
            2 Mbps 패킷이 흘러든다. 115200 으로 디코딩돼 깨진 문자가 되는데,
            그대로 두면 로그창을 가득 채워 판정 요약이 묻힌다.
          · esptool 진행률 — "4096 (16 %)" 같은 줄. 진행률 막대가 이미 있다.
        """
        s = line.strip()
        if not s:
            return False
        if PROGRESS_RE.fullmatch(s):
            return True
        bad = sum(1 for ch in s if ch == "�" or (ord(ch) < 0x20 and ch != "\t"))
        return bad > max(2, len(s) * 0.2)

    def _dev_line(self, prefix="  "):
        """디바이스/esptool 출력을 로그로 보내는 콜백을 만든다 (워커에서 사용)."""
        def cb(line):
            if not self._is_noise(line):
                self._post("log", prefix + line)
        return cb

    def _set_busy(self, busy):
        self._busy = busy
        state = "disabled" if busy else "normal"
        for w in (self.read_btn, self.inject_btn,
                  self.refresh_btn, self.find_btn):
            w.configure(state=state)
        # 번들 펌웨어가 없으면 굽기는 계속 비활성으로 둔다
        if self.manifest:
            self.flash_btn.configure(state=state)
        # 작업 중에는 작업할 센서를 바꾸지 못하게 한다 — 도중에 바뀌면
        # 어느 보드에 무엇을 썼는지 알 수 없게 된다.
        self.tree.configure(selectmode="none" if busy else "browse")
        self.factory_lbl.configure(foreground="#999" if busy else "#a33")
        self.cancel_btn.configure(state="normal" if busy else "disabled")
        if not busy:
            self._cancel.clear()
            self._stop_indeterminate()

    def _start(self, name, fn, *fnargs):
        """워커 시작. 이미 실행 중이면 거부한다 (두 작업이 포트를 다투지 않게)."""
        if self._busy:
            messagebox.showwarning("실행 중",
                                   "다른 작업이 진행 중입니다. 끝난 뒤 다시 시도하세요.")
            return False
        self._cancel.clear()
        self._set_busy(True)
        self.pbar.configure(value=0)
        self.status.configure(text=name)
        threading.Thread(target=self._wrap, args=(fn, fnargs), daemon=True).start()
        return True

    def _wrap(self, fn, fnargs):
        """워커 공통 뒷정리. 어떤 경로로 끝나도 버튼이 되살아나게 한다."""
        try:
            fn(*fnargs)
        except StepError as e:
            self._post("log", "%s %s" % ("⚠" if e.warn else "❌", e.title))
            self._post("warn" if e.warn else "error", e.title, e.body)
        except esp_flash.PortBusyError as e:
            self._post("log", "❌ %s" % e)
            self._post("error", "포트 사용 중", str(e))
        except (esp_flash.EsptoolError, usb_reset.UsbResetError) as e:
            self._post("log", "❌ %s" % e)
            self._post("error", "실패", str(e))
        except Exception as e:
            self._post("log", "❌ 예기치 못한 오류: %s" % e)
            self._post("error", "실패", str(e))
        finally:
            self._post("status", "대기 중")
            self._post("busy", False)

    # ------------------------------------------------------------------
    # 전송 방식 전환
    # ------------------------------------------------------------------
    def _on_transport(self):
        """USB 직결에서는 WiFi 입력란을 비활성화한다. 값은 지우지 않는다 —
        나중에 WiFi 모드로 되돌릴 때 다시 입력하지 않아도 되게."""
        transport = TRANSPORT_OPTIONS[self.tr_cb.current()][1]
        if transport == 2:
            for w in self.wifi_widgets:
                w.configure(state="disabled")
            self.wifi_frame.configure(text=" WiFi 전송 설정 (USB 직결에서는 사용 안 함) ")
        else:
            for w in self.wifi_widgets:
                w.configure(state="normal")
            self.wifi_frame.configure(text=" WiFi 전송 설정 ")

    # ------------------------------------------------------------------
    # 입력 수집 / 채우기
    # ------------------------------------------------------------------
    # ---------- 진동센서 목록 ----------
    def refresh_ports(self, *, keep_mac=True):
        """연결된 보드로 목록을 다시 채운다 (MAC 조회는 하지 않는다).

        MAC 조회는 보드를 부트로더로 리셋하므로 목록 갱신마다 하지 않는다.
        `keep_mac` 이면 **같은 USB 슬롯**에서 앞서 읽은 MAC 을 물려받는다 —
        슬롯이 같으면 같은 보드이므로 다시 읽을 필요가 없다. 슬롯이 바뀌었으면
        물려받지 않는다 (다른 보드일 수 있다).
        """
        prev_mac = {b.slot: b.mac for b in self.boards if b.mac} if keep_mac else {}
        prev_sel = self._selected_slot()

        self.boards = ports_mod.list_ports()
        for b in self.boards:
            b.mac = prev_mac.get(b.slot)

        log = provision_log.load()
        self.tree.delete(*self.tree.get_children())
        for b in self.boards:
            entry = log.get(b.mac) if b.mac else None
            self.tree.insert("", "end", values=(
                b.short_slot, b.short_dev, b.mac or "미확인",
                provision_log.summary_line(b.mac, entry) if b.mac else ""))

        # 이전 선택을 슬롯 기준으로 되살린다 (포트 번호가 바뀌어도 유지)
        items = self.tree.get_children()
        if items:
            idx = 0
            for i, b in enumerate(self.boards):
                if b.slot == prev_sel:
                    idx = i
                    break
            self.tree.selection_set(items[idx])
        self._on_board_select()

    def _selected_slot(self):
        sel = self.tree.selection() if hasattr(self, "tree") else ()
        if not sel:
            return None
        i = self.tree.index(sel[0])
        return self.boards[i].slot if i < len(self.boards) else None

    def selected_board(self):
        """지금 선택된 Board. 없으면 None."""
        sel = self.tree.selection()
        if not sel:
            return None
        i = self.tree.index(sel[0])
        return self.boards[i] if i < len(self.boards) else None

    def _on_board_select(self):
        b = self.selected_board()
        if b is None:
            self.board_hint.configure(text="연결된 보드가 없습니다", fg="#a33")
            return
        if not b.mac:
            self.board_hint.configure(
                text="대상  %s · %s   —  ‘MAC 주소 확인’ 을 누르세요"
                     % (b.short_dev, b.short_slot), fg="#8a5a00")
            return
        entry = provision_log.load().get(b.mac)
        self.board_hint.configure(
            text="대상  %s   (%s · %s)   %s"
                 % (b.mac, b.short_dev, b.short_slot,
                    provision_log.summary_line(b.mac, entry) if entry else "새 보드"),
            fg="#555")

    def on_find_boards(self):
        """모든 포트의 MAC 을 읽어 목록을 채운다 (포트당 약 3초)."""
        self.refresh_ports(keep_mac=False)
        if not self.boards:
            messagebox.showwarning("보드 없음", "연결된 USB 시리얼 포트가 없습니다.")
            return
        self.logln("── MAC 주소 확인 (%d대, 포트당 약 3초)" % len(self.boards))
        # 한 대씩 차례로 읽으므로 뒤 순번은 한동안 기다린다. 그동안 반응이 없으면
        # 멈춘 줄 알고, 결과 칸이 비어 있으면 실패로 오해한다 — 줄마다 상태를 보인다.
        for item in self.tree.get_children():
            self.tree.set(item, "mac", "대기 — 다음 차례")
        if self._start("보드 확인 중...", self._w_find_boards, list(self.boards)):
            self.pbar.configure(mode="indeterminate")
            self.pbar.start(12)

    def _w_find_boards(self, boards):
        """**워커 스레드.** MAC 을 읽고 결과만 메인으로 보낸다."""
        n = len(boards)
        order = {id(b): i + 1 for i, b in enumerate(boards)}

        def on_board(b, state):
            self._post("board_row", b.slot, state, b.device, b.mac, b.error)
            if state == "checking":
                self._post("status", "보드 확인 중... (%d/%d)" % (order[id(b)], n))

        ports_mod.read_macs(boards, on_line=lambda l: self._post("log", "  " + l),
                            on_board=on_board)
        found = [(b.slot, b.mac, b.error) for b in boards]
        for slot, mac, err in found:
            self._post("log", "  %s → %s" % (slot.rsplit("-usb-", 1)[-1],
                                             mac or "읽기 실패: %s" % err))
        self._post("boards", found)

    def _show_board_row(self, slot, state, device, mac, err):
        """MAC 주소 확인 중 한 줄의 상태를 바로 보인다. **메인 스레드 전용.**"""
        for item, b in zip(self.tree.get_children(), self.boards):
            if b.slot != slot:
                continue
            b.device = device           # USB 복구로 ttyACM 번호가 바뀌었을 수 있다
            self.tree.set(item, "dev", b.short_dev)
            self.tree.set(item, "mac", {
                "checking": "확인 중…",
                "recovering": "USB 복구 중…",
            }.get(state) or mac or "읽기 실패 (로그 참고)")
            break

    def _stop_indeterminate(self):
        if str(self.pbar.cget("mode")) == "indeterminate":
            self.pbar.stop()
            self.pbar.configure(mode="determinate", value=0)

    def _apply_macs(self, found):
        """읽은 MAC 을 목록에 반영한다. **메인 스레드 전용.**"""
        by_slot = {slot: (mac, err) for slot, mac, err in found}
        for b in self.boards:
            if b.slot in by_slot:
                b.mac, b.error = by_slot[b.slot]
        log = provision_log.load()
        for item, b in zip(self.tree.get_children(), self.boards):
            entry = log.get(b.mac) if b.mac else None
            self.tree.item(item, values=(
                b.short_slot, b.short_dev,
                b.mac or ("읽기 실패 (로그 참고)" if b.error else "미확인"),
                provision_log.summary_line(b.mac, entry) if b.mac else ""))
        self._on_board_select()

    def _port(self):
        """선택된 보드의 포트. 선택이 없으면 ValueError."""
        b = self.selected_board()
        if b is None:
            raise ValueError(
                "진동센서 목록에서 작업할 센서를 선택하세요.\n"
                "목록이 비어 있으면 케이블을 확인하고 ‘목록만 갱신’ 을 누르세요.")
        return b.device

    def collect(self):
        ssid = self.ssid.get().strip()
        pw = self.pw.get()
        srv_ip = self.srv_ip.get().strip()
        srv_port_s = self.srv_port.get().strip()
        rate = RATE_OPTIONS[self.rate_cb.current()][1]
        read_mode = READMODE_OPTIONS[self.rm_cb.current()][1]
        transport = TRANSPORT_OPTIONS[self.tr_cb.current()][1]
        full_scale_g = FULLSCALE_OPTIONS[self.fs_cb.current()][1]

        self._port()        # 포트 미선택이면 여기서 막힌다

        if transport == 2:
            # USB 직결에서는 디바이스가 WiFi 에 접속하지 않으므로 SSID/서버IP 가
            # 쓰이지 않는다. 다만 NVS 키 구조는 유지해야 하므로 비어 있을 때만
            # 자리표시 값을 채운다. 입력된 값은 그대로 보존해 나중에 WiFi 모드로
            # 되돌릴 때 다시 입력하지 않아도 되게 한다.
            if not ssid:
                ssid = "unused"
            if not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", srv_ip):
                srv_ip = "0.0.0.0"
            if not srv_port_s.isdigit() or not (1 <= int(srv_port_s) <= 65535):
                srv_port_s = "9000"
        else:
            if not ssid:
                raise ValueError("WiFi 이름(SSID)을 입력하세요.")
            if not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", srv_ip):
                raise ValueError("라즈베리파이 IP 형식이 올바르지 않습니다. (예: 192.168.0.37)")
            if not srv_port_s.isdigit() or not (1 <= int(srv_port_s) <= 65535):
                raise ValueError("포트는 1~65535 범위여야 합니다. (기본 9000)")

        return {"ssid": ssid, "pw": pw, "srv_ip": srv_ip,
                "srv_port": int(srv_port_s), "rate": rate,
                "read_mode": read_mode, "transport": transport,
                "full_scale_g": full_scale_g}

    def _apply_prefill(self, prefill):
        """디바이스에서 읽은 값을 입력란에 채운다. **메인 스레드 전용.**

        "있으면 있는 대로 보여주고, 없으면 없는 대로 입력받는다" —
        없는 키는 건드리지 않아 사용자가 이미 입력한 값을 지우지 않는다.
        """
        if not prefill:
            self.logln("디바이스에 저장된 설정이 없습니다 (공장 초기 상태).")
            self.logln("→ 값을 직접 입력하고 ② 설정 주입 을 누르세요.")
            return
        if "ssid" in prefill:
            self.ssid.set(prefill["ssid"])
        if "pw" in prefill:
            self.pw.set(prefill["pw"])
        if "srv_ip" in prefill:
            self.srv_ip.set(prefill["srv_ip"])
        if "srv_port" in prefill:
            self.srv_port.set(str(prefill["srv_port"]))
        if "transport" in prefill:
            self.tr_cb.current(self._index_by_value(TRANSPORT_OPTIONS,
                                                    prefill["transport"]))
            self._on_transport()
        if "rate" in prefill:
            self.rate_cb.current(self._index_by_value(RATE_OPTIONS, prefill["rate"]))
        if "read_mode" in prefill:
            self.rm_cb.current(self._index_by_value(READMODE_OPTIONS,
                                                    prefill["read_mode"]))
        if "full_scale_g" in prefill:
            self.fs_cb.current(self._index_by_value(FULLSCALE_OPTIONS,
                                                    prefill["full_scale_g"]))
        missing = [n for k, n in (("ssid", "WiFi 이름"), ("pw", "WiFi 비밀번호"),
                                  ("srv_ip", "라즈베리파이 IP"))
                   if k not in prefill]
        if missing:
            self.logln("비어 있는 항목(직접 입력 필요): " + ", ".join(missing))

    # ------------------------------------------------------------------
    # 동작 — 설정 읽기
    # ------------------------------------------------------------------
    def on_read_settings(self):
        try:
            port = self._port()
        except ValueError as e:
            messagebox.showwarning("입력 확인", str(e)); return
        self.logln("── 현재 설정 읽기 (%s)" % port)
        self._start("디바이스 설정 읽는 중...", self._w_read, port)

    def _w_read(self, port):
        devcfg = nvs_read.read_from_device(
            port, offset=NVS_OFFSET, size=NVS_SIZE,
            on_line=self._dev_line())
        # 비밀번호는 화면 로그에 남기지 않는다. 입력란에는 채우되(수정 편의),
        # 로그·저장 파일에는 흘리지 않는다.
        self._post("log", nvs_read.describe(devcfg, show_secrets=False))
        self._post("prefill", nvs_read.to_gui_prefill(devcfg))
        self._post("progress", 100)

    # ------------------------------------------------------------------
    # 동작 — ① 펌웨어 굽기
    # ------------------------------------------------------------------
    def on_flash_firmware(self):
        if not self.manifest:
            messagebox.showerror("펌웨어 없음",
                                 "번들된 펌웨어를 찾지 못해 굽기를 실행할 수 없습니다.")
            return
        try:
            port = self._port()
        except ValueError as e:
            messagebox.showwarning("입력 확인", str(e)); return
        if not messagebox.askyesno(
                "펌웨어 굽기",
                "디바이스에 펌웨어를 씁니다 (약 1~2분).\n\n"
                "· 저장된 설정(NVS)은 지워지지 않습니다\n"
                "· 진행 중 USB 케이블을 뽑지 마세요\n\n계속할까요?"):
            return
        b = self.selected_board()
        self.logln("── ① 펌웨어 굽기  대상 %s (%s)"
                   % ((b.mac if b and b.mac else "MAC 미확인"), port))
        self._start("펌웨어 굽는 중...", self._w_flash_fw, port)

    def _do_flash_fw(self, port):
        """① 굽기 본체. **워커 스레드.** 새 포트를 돌려준다. 실패하면 StepError.

        대화상자를 띄우지 않는다 — 순차 진행에서 보드마다 대화상자가 뜨면
        사용자가 매번 눌러야 해 "자동" 이 아니게 된다. 판정은 예외로 올린다.
        """
        cb = dict(
            on_line=self._dev_line(),
            on_progress=lambda p: self._post("progress", p),
            on_status=lambda s: self._post("status", s),
            cancel=self._cancel,
        )
        try:
            res = esp_flash.flash_firmware(port, str(self.pkg_dir), **cb)
        except esp_flash.UsbStallError as e:
            port = self._recover_stall(port, e)
            res = esp_flash.flash_firmware(port, str(self.pkg_dir), **cb)

        self._post("log", "✅ 펌웨어 쓰기 완료 (이미지 %d개 검증 %d회)"
                   % (res["images"], res["verified"]))
        self._post("status", "부팅 확인 중...")
        st = boot_log.verify_after_flash(port, on_line=self._dev_line())
        self._post("log", st.summary())

        if st.sensor_missing:
            raise StepError("센서 미감지",
                            "펌웨어는 정상 동작하지만 센서를 찾지 못했습니다.\n"
                            "boardcheck 로 보드를 먼저 진단하세요.")
        if not st.deploy_build:
            raise StepError("개발 빌드",
                            "번들 펌웨어가 개발 빌드입니다 — 설정툴의 WiFi 입력이 "
                            "무시됩니다.\n배포 담당자에게 정상 패키지를 요청하세요.")
        return port

    def _w_flash_fw(self, port):
        port = self._do_flash_fw(port)
        self._post("info", "완료",
                   "펌웨어 굽기가 끝났습니다.\n\n이어서 ② 설정 주입 을 누르세요.")

    def _recover_stall(self, port, err):
        """USB 스톨 시 장치 리셋을 제안하고, 승낙하면 복구 후 새 포트를 돌려준다."""
        self._post("log", "⚠ USB 전송이 멈췄습니다 (bulk-OUT 스톨).")
        if not self._ask("USB 복구",
                         "USB 전송이 멈췄습니다.\n\n"
                         "장치를 리셋하고 한 번 더 시도할까요?\n"
                         "(데이터는 지워지지 않습니다)"):
            raise err
        self._post("status", "USB 장치 리셋 중...")
        new_port = usb_reset.recover(port, on_line=self._dev_line())
        self._post("log", "포트 복구: %s" % new_port)
        return new_port

    # ------------------------------------------------------------------
    # 동작 — ② 설정 주입
    # ------------------------------------------------------------------
    def _port_conflict(self, cfg, mac):
        """WiFi 모드에서 **다른 보드**가 이미 같은 IP:포트를 쓰는지 본다.

        두 센서가 같은 수신 포트로 보내면 수신기가 두 데이터를 한 파일에 섞고
        유실이 실제보다 훨씬 크게 잡힌다 — 조용한 오류라 가장 나쁘다.
        이력에 근거가 있으므로 주입 전에 걸러낼 수 있다.
        """
        if int(cfg.get("transport", 0)) != 0:
            return None                      # USB 직결은 포트를 쓰지 않는다
        for other, e in provision_log.load().items():
            if mac and other == mac.lower():
                continue                     # 같은 보드 재설정은 충돌이 아니다
            if int(e.get("transport", -1)) != 0:
                continue
            if (e.get("srv_ip") == cfg["srv_ip"]
                    and int(e.get("srv_port") or 0) == int(cfg["srv_port"])):
                return other
        return None

    def on_inject(self):
        try:
            cfg = self.collect()
        except ValueError as e:
            messagebox.showwarning("입력 확인", str(e)); return
        port = self._port()
        b = self.selected_board()
        mac = b.mac if b else None

        other = self._port_conflict(cfg, mac)
        if other:
            nxt = int(cfg["srv_port"]) + 1
            ans = messagebox.askyesnocancel(
                "수신 포트 중복",
                "다른 보드가 이미 %s:%s 를 쓰고 있습니다.\n(%s)\n\n"
                "같은 포트를 쓰면 두 센서 데이터가 한 파일에 섞이고\n"
                "유실이 실제보다 크게 잡힙니다.\n\n"
                "이 보드를 %d 번 포트로 바꿀까요?\n\n"
                "  예   — %d 로 바꿔 진행\n"
                "  아니오 — 그대로 진행\n"
                "  취소 — 중단"
                % (cfg["srv_ip"], cfg["srv_port"], other, nxt, nxt))
            if ans is None:
                return
            if ans:
                self.srv_port.set(str(nxt))
                cfg["srv_port"] = nxt
                self.logln("수신 포트를 %d 로 바꿨습니다 (중복 회피)" % nxt)

        save_prefs(cfg, self.remember.get())
        # 센서 번호 = 목록(슬롯 순)에서의 순번. 수집기 slots.py --auto 와 같은 규칙이다.
        sensor = str(self.boards.index(b) + 1) if b in self.boards else None
        self.logln("── ② 설정 주입  대상 %s (%s)" % (mac or "MAC 미확인", port))
        self._start("설정 주입 중...", self._w_inject, port, cfg, mac,
                    b.slot if b else None, sensor)

    def _w_inject(self, port, cfg, mac=None, slot=None, sensor=None):
        """단독 ② 실행 — 본체를 돌리고 결과 대화상자를 띄운다.

        MAC 을 모르면 먼저 읽는다 (약 3초). 이력은 MAC 으로 남기는데, MAC 주소 확인을
        건너뛰고 ② 만 누르면 예전에는 이력이 아예 남지 않았다.
        """
        if not mac:
            b = next((x for x in self.boards if x.slot == slot), None)
            if b is not None:
                self._post("status", "보드 확인 중 (MAC)...")
                ports_mod.read_macs([b], on_line=lambda l: self._post("log", "  " + l),
                                    on_board=lambda bd, st: self._post(
                                        "board_row", bd.slot, st, bd.device, bd.mac, bd.error))
                mac, port = b.mac, b.device
                self._post("boards", [(b.slot, b.mac, b.error)])
                self._post("status", "설정 주입 중...")
        self._do_inject(port, cfg, mac, slot, sensor)
        self._post("info", "완료", "설정 주입 완료 — 스트리밍이 시작됐습니다.")

    def _do_inject(self, port, cfg, mac=None, slot=None, sensor=None):
        """② 주입 본체. **워커 스레드.** 새 포트를 돌려준다. 실패하면 StepError.

        성공 시 **MAC 별 이력을 남긴다** — 보드를 여러 대 다룰 때 "이 보드에
        어떤 IP·포트를 넣었나" 를 되짚을 유일한 수단이다. MAC 을 모르면
        (MAC 주소 확인을 하지 않았으면) 기록하지 않고 그 사실을 로그에 남긴다.
        """
        port = self._inject_core(port, cfg)
        if mac:
            try:
                provision_log.record(
                    mac, cfg, slot=slot, sensor=sensor,
                    fw_version=(self.manifest or {}).get("app_version"),
                    fw_package=(self.manifest or {}).get("package_version"))
                self._post("log", "이력 기록: %s" % mac)
            except Exception as e:
                self._post("log", "⚠ 이력 기록 실패: %s" % e)
        else:
            self._post("log", "ℹ MAC 미확인이라 이력을 남기지 않았습니다 "
                              "(‘MAC 주소 확인’ 을 먼저 누르면 기록됩니다)")
        return port

    def _inject_core(self, port, cfg):
        with tempfile.TemporaryDirectory() as td:
            bin_path = os.path.join(td, "devcfg_nvs.bin")
            build_nvs_bin(cfg, bin_path)
            self._post("log", "NVS 바이너리 생성 완료 (%d B)" % NVS_SIZE)
            try:
                esp_flash.flash_nvs(
                    port, bin_path, offset=NVS_OFFSET,
                    on_line=self._dev_line(),
                    on_progress=lambda p: self._post("progress", p),
                    on_status=lambda s: self._post("status", s),
                    cancel=self._cancel)
            except esp_flash.UsbStallError as e:
                port = self._recover_stall(port, e)
                esp_flash.flash_nvs(
                    port, bin_path, offset=NVS_OFFSET,
                    on_line=self._dev_line(),
                    cancel=self._cancel)

        self._post("log", "✅ 주입 완료 — 반영 여부를 확인합니다")
        self._post("status", "부팅 로그로 반영 확인 중...")
        st = boot_log.verify_after_inject(
            port, cfg, on_line=self._dev_line())
        self._post("log", st.summary())

        # 판정: 펌웨어가 되울린 설정이 입력값과 같은가가 핵심이다.
        if st.mismatches:
            raise StepError(
                "설정 불일치",
                "주입은 됐지만 펌웨어가 읽은 값이 입력값과 다릅니다.\n\n"
                + "\n".join("· %s: 입력 %r ≠ 디바이스 %r" % m
                             for m in st.mismatches))
        if not st.config_applied:
            raise StepError(
                "확인 불가",
                "주입은 완료됐으나 부팅 로그에서 설정 반영을 확인하지 못했습니다.\n"
                "USB 를 뽑았다 꽂아 다시 확인해 보세요.", warn=True)
        if not st.streaming:
            raise StepError(
                "스트리밍 미시작",
                "설정은 반영됐지만 스트리밍이 시작되지 않았습니다.\n"
                + (st.stream_fail_reason or "부팅 로그를 확인하세요."), warn=True)
        return port

    # ------------------------------------------------------------------
    # 동작 — 공장 초기화 / 취소 / 로그 저장
    # ------------------------------------------------------------------
    def on_factory_reset(self):
        if self._busy:
            return
        try:
            port = self._port()
        except ValueError as e:
            messagebox.showwarning("입력 확인", str(e)); return
        if not messagebox.askyesno(
                "공장 초기화",
                "디바이스에 저장된 설정(WiFi·서버·측정)을 모두 지웁니다.\n\n"
                "· 펌웨어는 지워지지 않습니다\n"
                "· 되돌릴 수 없습니다\n\n정말 진행할까요?",
                icon="warning", default="no"):
            return
        self.logln("── 공장 초기화 (%s)" % port)
        self._start("공장 초기화 중...", self._w_factory, port)

    def _w_factory(self, port):
        with tempfile.TemporaryDirectory() as td:
            bin_path = os.path.join(td, "blank_nvs.bin")
            build_blank_nvs(bin_path)
            esp_flash.flash_nvs(
                port, bin_path, offset=NVS_OFFSET,
                on_line=self._dev_line(),
                on_progress=lambda p: self._post("progress", p),
                cancel=self._cancel)
        self._post("log", "✅ 설정이 지워졌습니다 (공장 초기 상태)")
        self._post("info", "완료",
                   "공장 초기화 완료.\n\n다시 사용하려면 ② 설정 주입 을 실행하세요.")

    def on_cancel(self):
        if not self._busy:
            return
        self._cancel.set()
        self.logln("⏹ 취소 요청 — 진행 중인 단계가 끝나는 대로 멈춥니다.")
        self.status.configure(text="취소 중...")

    def on_help(self):
        """사용설명서를 브라우저로 연다 (오프라인 파일)."""
        import helpdoc
        try:
            helpdoc.open_help()
            self.logln("사용설명서를 열었습니다: %s" % helpdoc.HELP_FILE)
        except helpdoc.HelpError as e:
            self.logln("도움말 열기 실패 — %s" % e)
            messagebox.showwarning("도움말", str(e))

    def on_save_log(self):
        text = self.log.get("1.0", "end").strip()
        if not text:
            messagebox.showinfo("로그 저장", "저장할 내용이 없습니다.")
            return
        default = "iis3dwb-setup-%s.log" % datetime.now().strftime("%Y%m%d-%H%M%S")
        path = filedialog.asksaveasfilename(
            title="로그 저장", initialfile=default, defaultextension=".log",
            filetypes=[("로그 파일", "*.log"), ("모든 파일", "*.*")])
        if not path:
            return
        try:
            Path(path).write_text(text + "\n", encoding="utf-8")
            self.logln("로그 저장됨: %s" % path)
        except Exception as e:
            messagebox.showerror("저장 실패", str(e))


if __name__ == "__main__":
    SetupApp().mainloop()
