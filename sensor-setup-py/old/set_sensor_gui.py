#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IIS3DWB 센서 설정 툴 (Tkinter GUI) — 무설치 버전

ESP-IDF 개발환경이 갖춰진 PC에서 이 파일만 가져와 실행하면 됩니다.
별도 라이브러리 설치 불필요 (파이썬 표준 라이브러리 tkinter 만 사용).

동작:
  1. WiFi / 서버(라즈베리파이) / 측정속도 / 읽기방식 입력
  2. ESP-IDF 의 nvs_partition_gen.py 로 NVS 바이너리(devcfg) 생성
  3. ESP-IDF 의 esptool.py 로 NVS 파티션(0x9000) 에 주입
  4. (선택) 디바이스 재부팅 → idf.py monitor 로 연결/스트리밍 확인

전제:
  - ESP-IDF 환경이 활성화돼 있어야 합니다.   source ~/esp/.../export.sh
  - 그래야 nvs_partition_gen.py / esptool.py 를 PATH 에서 찾습니다.

펌웨어와 반드시 일치해야 하는 값 (수정 금지):
  - NVS namespace = "devcfg"
  - NVS 파티션 offset = 0x9000, size = 0x6000
  - 키: wifi_ssid, wifi_pass, srv_ip, srv_port(u16), stream_rate(u8),
        transport(u8), read_mode(u8)   ← Rust 설정툴(nvs.rs)과 동일한 순서
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import tkinter as tk
from tkinter import ttk, messagebox

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


# ===================== ESP-IDF 도구 탐색 =====================
def find_tool(name):
    """PATH 에서 도구를 찾는다. nvs_partition_gen.py 는 ESP-IDF 안에 있다."""
    # 1) PATH 직접
    p = shutil.which(name)
    if p:
        return p
    # 2) IDF_PATH 기반 보조 탐색
    idf = os.environ.get("IDF_PATH")
    if idf:
        cand = list(Path(idf).rglob(name))
        if cand:
            return str(cand[0])
    return None


def esptool_cmd():
    """esptool 실행 커맨드(리스트) 반환. esptool.py 또는 'python -m esptool'."""
    p = shutil.which("esptool.py")
    if p:
        return [p]
    return [sys.executable, "-m", "esptool"]


# ===================== NVS 생성 + 주입 =====================
def build_nvs_bin(cfg, out_bin):
    """ESP-IDF nvs_partition_gen.py 로 NVS 바이너리 생성."""
    gen = find_tool("nvs_partition_gen.py")
    if not gen:
        raise RuntimeError(
            "nvs_partition_gen.py 를 찾을 수 없습니다.\n"
            "ESP-IDF 환경을 먼저 활성화하세요:  source ~/esp/.../export.sh"
        )

    # CSV 작성 — 키 순서는 펌웨어/Rust툴과 동일하게 유지
    csv_lines = [
        "key,type,encoding,value",
        f"{NVS_NAMESPACE},namespace,,",
        f"wifi_ssid,data,string,{cfg['ssid']}",
        f"wifi_pass,data,string,{cfg['pw']}",
        f"srv_ip,data,string,{cfg['srv_ip']}",
        f"srv_port,data,u16,{cfg['srv_port']}",
        f"stream_rate,data,u8,{cfg['rate']}",
        f"transport,data,u8,0",
        f"read_mode,data,u8,{cfg['read_mode']}",
    ]
    with tempfile.NamedTemporaryFile(
        "w", suffix=".csv", delete=False, encoding="utf-8"
    ) as f:
        f.write("\n".join(csv_lines) + "\n")
        csv_path = f.name

    try:
        cmd = [sys.executable, gen, "generate", csv_path, out_bin, str(NVS_SIZE)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(
                "NVS 바이너리 생성 실패:\n" + (r.stderr or r.stdout)
            )
    finally:
        try:
            os.unlink(csv_path)
        except OSError:
            pass


def flash_nvs(port, bin_path):
    """esptool 로 NVS 파티션(0x9000) 에 주입."""
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
    """간단한 시리얼 포트 후보 목록."""
    ports = []
    # pyserial 이 있으면 사용 (없어도 동작)
    try:
        from serial.tools import list_ports
        ports = [p.device for p in list_ports.comports()]
    except Exception:
        # 폴백: /dev 스캔 (Linux/macOS)
        dev = Path("/dev")
        if dev.exists():
            for entry in dev.iterdir():
                name = str(entry)
                if any(h in name for h in DEFAULT_PORT_HINTS):
                    ports.append(name)
    return sorted(set(ports))


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
        self.geometry("460x620")
        self.resizable(False, True)

        prefs = load_prefs()
        pad = {"padx": 14, "pady": 4}

        row = 0
        ttk.Label(
            self, text="IIS3DWB 센서 설정", font=("", 15, "bold")
        ).grid(row=row, column=0, columnspan=2, pady=(14, 2)); row += 1
        ttk.Label(
            self, text="ESP-IDF 환경에서 실행 · NVS(0x9000)에 직접 주입",
            foreground="#666",
        ).grid(row=row, column=0, columnspan=2, pady=(0, 10)); row += 1

        # 포트
        ttk.Label(self, text="USB 포트").grid(row=row, column=0, sticky="w", **pad)
        self.port_var = tk.StringVar()
        self.port_cb = ttk.Combobox(self, textvariable=self.port_var, width=28)
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
        ports = list_serial_ports()
        self.port_cb["values"] = ports
        if ports and not self.port_var.get():
            self.port_var.set(ports[0])

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
        port = self.port_var.get()
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
