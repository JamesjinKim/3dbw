#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IIS3DWB 센서 설정 툴 (Tkinter GUI) — 무설치 버전

ESP-IDF 개발환경이 갖춰진 PC에서 이 폴더를 가져와 실행하면 됩니다.
별도 라이브러리 설치 불필요 (파이썬 표준 라이브러리 tkinter 만 사용).

동작:
  1. WiFi / 서버(라즈베리파이) / 측정속도 / 읽기방식 입력
  2. NVS 바이너리(devcfg)를 파이썬에서 직접 생성 (nvs_gen.py — 외부 도구 불필요)
  3. esptool 로 NVS 파티션(0x9000) 에 주입
  4. (선택) 디바이스 재부팅 → idf.py monitor 로 연결/스트리밍 확인

전제:
  - NVS 생성은 ESP-IDF 활성화가 필요 없습니다 (nvs_gen.py 가 순수 파이썬).
  - 주입(esptool)만 있으면 됩니다. ESP-IDF 환경을 활성화했다면 esptool 이 PATH 에 잡히고,
    아니면 `pip install esptool` 한 번이면 됩니다. (둘 다 자동 탐색)

펌웨어와 반드시 일치해야 하는 값 (수정 금지):
  - NVS namespace = "devcfg"
  - NVS 파티션 offset = 0x9000, size = 0x6000
  - 키: wifi_ssid, wifi_pass, srv_ip, srv_port(u16), stream_rate(u8),
        transport(u8), read_mode(u8)   ← Rust 설정툴(nvs.rs)과 동일한 순서
"""

import json
import re
import shutil
import subprocess
import sys
import os
import tempfile
import threading
from pathlib import Path

import tkinter as tk
from tkinter import ttk, messagebox

# 같은 폴더의 순수 파이썬 NVS 생성기 (Rust nvs.rs 와 byte-exact 검증됨)
import nvs_gen

# ===================== 펌웨어와 일치해야 하는 상수 =====================
NVS_NAMESPACE = "devcfg"
NVS_OFFSET = 0x9000
NVS_SIZE = 0x6000          # 24576 bytes
BAUD = 115200
DEFAULT_PORT_HINTS = ("/dev/ttyACM", "/dev/ttyUSB", "/dev/cu.usb", "COM")

# 입력값 기억 파일 (이 PC 사용자 홈) — 여러 센서 연속 세팅 편의
PREFS_PATH = Path.home() / ".iis3dwb_sensor_setup.json"

# 측정 속도 선택지 (label, stream_rate 값)  — Rust GUI 와 동일한 기본/순서
RATE_OPTIONS = [
    ("3.3 kHz · 일반 모니터링 (기본·권장)", 1),
    ("6.6 kHz · 중속 분석", 2),
    ("13.3 kHz · 고속 분석", 3),
    ("26.6 kHz · 정밀 진동분석 (최대)", 4),
    ("1 kHz · 안정 수신 (저부담)", 0),
]
# 읽기 방식 선택지 (label, read_mode 값)
READMODE_OPTIONS = [
    ("인터럽트 · 저부하", 1),
    ("자동 (폴링) · 안정", 0),
]


# ===================== esptool 탐색 =====================
def esptool_cmd():
    """esptool 실행 커맨드(리스트) 반환.

    탐색 우선순위:
      1) PATH 의 `esptool.py` / `esptool` (ESP-IDF export.sh 활성화 시)
      2) ESP-IDF venv 자동 스캔 — `~/.espressif/python_env/*/bin/esptool.py`
         (export.sh 를 안 깔아도 같은 PC 에서 한 번이라도 ESP-IDF 를 설치했다면 잡힘)
      3) 현재 파이썬에서 `-m esptool` (시스템에 pip install esptool 한 경우)
    """
    for name in ("esptool.py", "esptool"):
        p = shutil.which(name)
        if p:
            return [p]
    # venv 의 esptool.py 는 같은 venv 의 python 으로 돌려야 site-packages 가 잡힘.
    # 시스템 파이썬으로 venv 스크립트를 실행하면 `import esptool` 에서 실패한다.
    for cand in sorted(Path.home().glob(".espressif/python_env/*/bin/esptool.py")):
        venv_py = cand.parent / "python"
        if cand.is_file() and venv_py.exists():
            return [str(venv_py), "-m", "esptool"]
    return [sys.executable, "-m", "esptool"]


# ===================== NVS 생성 + 주입 =====================
def build_nvs_bin(cfg, out_bin):
    """NVS 바이너리(devcfg)를 파이썬에서 직접 생성해 out_bin 에 쓴다.

    외부 도구(nvs_partition_gen.py) 불필요 — nvs_gen.py 가 순수 파이썬으로
    Rust 설정툴(nvs.rs)과 byte-exact 동일한 바이너리를 만든다.
    """
    data = nvs_gen.generate_full_nvs(
        NVS_NAMESPACE,
        cfg["ssid"],
        cfg["pw"],
        cfg["srv_ip"],
        int(cfg["srv_port"]),
        int(cfg["rate"]),        # stream_rate
        0,                       # transport (0=UDP)
        int(cfg["read_mode"]),
        NVS_SIZE,
    )
    with open(out_bin, "wb") as f:
        f.write(data)


def flash_nvs(port, bin_path):
    """esptool 로 NVS 파티션(0x9000) 에 주입.

    보드의 자동 리셋 회로(DTR/RTS)를 이용해 esptool 이 자동으로 다운로드 모드에
    진입한다. (이 센서 보드는 물리 BOOT/RESET 버튼이 없으므로 자동 리셋만 사용)
    """
    cmd = esptool_cmd() + [
        "--chip", "esp32s3",
        "-p", port,
        "-b", str(BAUD),
        "write_flash", hex(NVS_OFFSET), bin_path,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("Flash 주입 실패:\n" + (r.stderr or r.stdout))
    return r.stdout


def list_serial_ports():
    """USB 로 연결된 시리얼 포트 경로만 반환.

    라즈베리파이 내장 UART(`/dev/ttyAMA*`) 등은 IIS3DWB 와 무관하므로 제외해
    사용자가 어느 포트가 센서인지 헷갈리지 않도록 한다. (포트 경로만 표시)
    """
    ports = []
    try:
        from serial.tools import list_ports
        for p in list_ports.comports():
            hwid = (p.hwid or "").upper()
            # USB VID/PID 가 잡히는 포트만 (USB 로 꽂힌 디바이스)
            if "USB" not in hwid and "VID" not in hwid:
                continue
            ports.append(p.device)
    except Exception:
        # 폴백: /dev 스캔 (Linux/macOS) — USB UART 패턴만
        dev = Path("/dev")
        if dev.exists():
            for entry in dev.iterdir():
                name = str(entry)
                if any(h in name for h in DEFAULT_PORT_HINTS):
                    ports.append(name)
    return sorted(set(ports))


def port_device(label):
    """콤보박스 값에서 실제 디바이스 경로만 추출 (이제 경로 그대로지만 안전 보강)."""
    if not label:
        return ""
    return label.split("  ")[0].strip()


# ===================== 입력값 기억 =====================
def load_prefs():
    try:
        if PREFS_PATH.exists():
            return json.loads(PREFS_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def save_prefs(cfg):
    try:
        PREFS_PATH.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass  # 저장 실패해도 기능엔 영향 없음


# ===================== GUI =====================
class SetupApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("IIS3DWB 센서 설정 툴")
        self.geometry("460x660")
        self.resizable(False, True)

        prefs = load_prefs()
        pad = {"padx": 14, "pady": 4}

        row = 0
        ttk.Label(
            self, text="IIS3DWB 센서 설정", font=("", 15, "bold")
        ).grid(row=row, column=0, columnspan=2, pady=(14, 2)); row += 1
        ttk.Label(
            self, text="NVS(0x9000)에 직접 주입 · 외부 도구 불필요",
            foreground="#666",
        ).grid(row=row, column=0, columnspan=2, pady=(0, 10)); row += 1

        # 포트
        ttk.Label(self, text="USB 포트").grid(row=row, column=0, sticky="w", **pad)
        self.port_var = tk.StringVar()
        self.port_cb = ttk.Combobox(
            self, textvariable=self.port_var, width=28, state="readonly",
        )
        self.port_cb.grid(row=row, column=1, sticky="w", **pad); row += 1
        ttk.Button(self, text="포트 새로고침", command=self.refresh_ports).grid(
            row=row, column=1, sticky="w", padx=14
        ); row += 1

        # WiFi
        ttk.Separator(self, orient="horizontal").grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=8
        ); row += 1
        self.ssid = self._field("WiFi 이름 (SSID)", prefs.get("ssid", ""), row); row += 1
        self.pw = self._field(
            "WiFi 비밀번호", prefs.get("pw", ""), row, show="*"
        ); row += 1

        # 서버
        ttk.Separator(self, orient="horizontal").grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=8
        ); row += 1
        self.srv_ip = self._field(
            "라즈베리파이 IP", prefs.get("srv_ip", ""), row
        ); row += 1
        self.srv_port = self._field(
            "포트", str(prefs.get("srv_port", "9000")), row
        ); row += 1

        # 측정 속도
        ttk.Label(self, text="측정 속도 (용도)").grid(
            row=row, column=0, sticky="w", **pad
        )
        self.rate_var = tk.StringVar()
        rate_labels = [o[0] for o in RATE_OPTIONS]
        self.rate_cb = ttk.Combobox(
            self, textvariable=self.rate_var, values=rate_labels,
            width=28, state="readonly",
        )
        self.rate_cb.grid(row=row, column=1, sticky="w", **pad); row += 1
        self.rate_cb.current(self._index_by_value(RATE_OPTIONS, prefs.get("rate", 1)))

        # 읽기 방식
        ttk.Label(self, text="데이터 읽기 방식").grid(
            row=row, column=0, sticky="w", **pad
        )
        self.rm_var = tk.StringVar()
        rm_labels = [o[0] for o in READMODE_OPTIONS]
        self.rm_cb = ttk.Combobox(
            self, textvariable=self.rm_var, values=rm_labels,
            width=28, state="readonly",
        )
        self.rm_cb.grid(row=row, column=1, sticky="w", **pad); row += 1
        self.rm_cb.current(self._index_by_value(READMODE_OPTIONS, prefs.get("read_mode", 1)))

        # 저장 버튼
        self.save_btn = ttk.Button(
            self, text="설정 저장 & 디바이스에 주입", command=self.on_save
        )
        self.save_btn.grid(row=row, column=0, columnspan=2, pady=16); row += 1

        # 상태 로그
        self.log = tk.Text(self, height=8, width=54, state="disabled", wrap="word")
        self.log.grid(row=row, column=0, columnspan=2, padx=14, pady=(0, 12))

        self.refresh_ports()

    def _field(self, label, value, row, show=None):
        ttk.Label(self, text=label).grid(row=row, column=0, sticky="w", padx=14, pady=4)
        var = tk.StringVar(value=value)
        e = ttk.Entry(self, textvariable=var, width=30, show=show)
        e.grid(row=row, column=1, sticky="w", padx=14, pady=4)
        return var

    @staticmethod
    def _index_by_value(options, value):
        for i, (_, v) in enumerate(options):
            if v == value:
                return i
        return 0

    def logln(self, msg):
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")
        self.update_idletasks()

    def refresh_ports(self):
        """현재 USB 연결 상태로 콤보박스 갱신.

        디바이스 재열거(빼고 다시 꽂으면 ACM 번호가 바뀜)로 인해 콤보박스에
        남아 있던 과거 값이 실제 존재하지 않는 포트(예: /dev/ttyACM10) 인
        상황을 막기 위해, 현재 목록에 없는 선택값은 즉시 비우고 첫 항목으로
        다시 잡아준다.
        """
        ports = list_serial_ports()
        self.port_cb["values"] = ports
        current = self.port_var.get()
        if current not in ports:
            self.port_var.set(ports[0] if ports else "")

    def collect(self):
        ssid = self.ssid.get().strip()
        pw = self.pw.get()
        srv_ip = self.srv_ip.get().strip()
        srv_port_s = self.srv_port.get().strip()
        rate = RATE_OPTIONS[self.rate_cb.current()][1]
        read_mode = READMODE_OPTIONS[self.rm_cb.current()][1]

        if not ssid:
            raise ValueError("WiFi 이름(SSID)을 입력하세요.")
        if not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", srv_ip):
            raise ValueError("라즈베리파이 IP 형식이 올바르지 않습니다. (예: 192.168.0.37)")
        if not srv_port_s.isdigit() or not (1 <= int(srv_port_s) <= 65535):
            raise ValueError("포트는 1~65535 범위여야 합니다. (기본 9000)")
        if not self.port_var.get():
            raise ValueError("USB 포트를 선택하세요.")

        return {
            "ssid": ssid, "pw": pw, "srv_ip": srv_ip,
            "srv_port": int(srv_port_s), "rate": rate, "read_mode": read_mode,
        }

    def on_save(self):
        try:
            cfg = self.collect()
        except ValueError as e:
            messagebox.showwarning("입력 확인", str(e))
            return

        save_prefs(cfg)          # 입력값 기억 (다음 센서 때 자동 표시)
        port = port_device(self.port_var.get())

        self.save_btn.configure(state="disabled")
        self.logln("설정 저장됨 (다음 실행 시 자동으로 채워집니다)")
        self.logln("NVS 생성 + 주입 중... (디바이스를 분리하지 마세요)")

        # 블로킹 작업은 스레드로 (UI 멈춤 방지)
        threading.Thread(
            target=self._do_flash, args=(cfg, port), daemon=True
        ).start()

    def _do_flash(self, cfg, port):
        try:
            with tempfile.TemporaryDirectory() as td:
                bin_path = os.path.join(td, "devcfg_nvs.bin")
                build_nvs_bin(cfg, bin_path)
                self.logln("NVS 바이너리 생성 완료")
                self.logln(f"포트 {port} 에 주입 중 (0x9000)...")
                flash_nvs(port, bin_path)
            self.logln("✅ 주입 완료! 디바이스를 재부팅하면 자동 연결됩니다.")
            self.logln("   확인:  idf.py -p %s monitor" % port)
            messagebox.showinfo(
                "완료",
                "설정 주입 완료!\n\n디바이스가 재부팅되면 WiFi에 연결되고\n"
                "라즈베리파이로 데이터를 전송합니다.\n\n"
                "다른 센서도 같은 설정으로 바로 주입할 수 있습니다.",
            )
        except Exception as e:
            self.logln("❌ 실패: " + str(e))
            messagebox.showerror("실패", str(e))
        finally:
            self.save_btn.configure(state="normal")


if __name__ == "__main__":
    SetupApp().mainloop()
