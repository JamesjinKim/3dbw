#!/usr/bin/env python3
"""
IIS3DWB 센서 데이터 USB 시리얼 수신 프로그램 (라즈베리파이)

디바이스 NVS 의 `transport = 2` (USB 직결) 모드에서, ESP32-S3 가 USB CDC 로
쏟아내는 진동 데이터를 수신·파싱하여 CSV 로 저장하고 처리량/유실률을 표시합니다.
WiFi 가 없거나 불안정한 현장에서 UDP 대신 사용합니다.

설계 근거: docs/2026-09-22-serial-streaming-design.md 7장 (수신 측 계약)

동작상 주의 (설계문서 7.5):
  - 디바이스는 부팅 후 약 3초 뒤부터 **무조건 단방향 송신**합니다.
    수신기가 없어도 계속 보냅니다.
  - 스트림 선두에 부팅 로그 잔여 바이트가 섞일 수 있어, magic("IIS3") 스캔으로
    패킷 경계를 찾아야 합니다. 이 스크립트가 자동으로 재동기합니다.
  - 보드레이트는 펌웨어 설정과 **반드시 일치**해야 합니다.
    이 보드의 USB 커넥터는 USB-UART 브리지로 이어지므로 스트리밍이 실제 UART 로
    나갑니다. 따라서 보드레이트가 실제 속도를 제한하며, 형식적인 값이 아닙니다.
    펌웨어: CONFIG_STREAM_SERIAL_UART_BAUD (기본 2000000)
    내장 USB(303a)로 직결된 보드라면 CDC 라 값이 형식적이며 115200 이어도 됩니다.

준비물:
  pyserial  —  sudo apt install python3-serial   (또는 pip install pyserial)

사용법:
  python3 serial_receiver.py                    # 포트 자동 감지, 2 Mbps
  python3 serial_receiver.py --port /dev/ttyACM0
  python3 serial_receiver.py --baud 115200       # 내장 USB(CDC) 보드
  python3 serial_receiver.py --out vib.csv --seconds 60
  python3 serial_receiver.py --no-csv           # 화면 표시만 (디스크 절약)
"""

import argparse
import glob
import os
import sys
import time
from datetime import datetime

# 패킷 파서의 정본은 배포물인 ../rpi-collector/ 에 있다. 이 CLI 는 개발·디버그용으로
# 남은 것이라, 사본을 두지 않고 그쪽을 임포트해 파서가 갈라지지 않게 한다.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "rpi-collector"))

try:
    import serial                      # pyserial
except ImportError:
    sys.exit("❌ pyserial 이 필요합니다:  sudo apt install -y python3-serial")

from iis3dwb_packet import (
    MAGIC_LE, RATE_HZ, parse_header, parse_samples, SeqTracker,
)

# 헤더의 sample_count 가 이 값을 넘으면 우연히 데이터에 나타난 가짜 magic 으로 본다.
# (펌웨어 STREAM_SAMPLES_PER_PACKET = 200)
MAX_SAMPLES = 1024


def detect_port():
    """연결된 ESP32 시리얼 포트를 찾는다.

    라즈베리파이 내장 UART(/dev/ttyAMA*, /dev/serial*) 는 IIS3DWB 와 무관하므로
    후보에서 제외한다. ESP32-S3 내장 USB Serial/JTAG 는 /dev/ttyACM* 로,
    CP210x·CH340 등 USB-UART 브리지 보드는 /dev/ttyUSB* 로 잡힌다.
    """
    cands = sorted(glob.glob("/dev/ttyACM*")) + sorted(glob.glob("/dev/ttyUSB*"))
    return cands[0] if cands else None


def main():
    ap = argparse.ArgumentParser(description="IIS3DWB USB 시리얼 수신기")
    ap.add_argument("--port", default=None,
                    help="시리얼 포트 (기본: /dev/ttyACM* 자동 감지)")
    ap.add_argument("--baud", type=int, default=2000000,
                    help="보드레이트 (기본 2000000 — 펌웨어의 "
                         "CONFIG_STREAM_SERIAL_UART_BAUD 와 같아야 한다)")
    ap.add_argument("--out", default="vibration_serial.csv",
                    help="저장할 CSV 파일 (raw+mg 모두 기록)")
    ap.add_argument("--no-csv", action="store_true",
                    help="CSV 저장 없이 화면 표시만 (고속에서 디스크 부하 회피)")
    ap.add_argument("--fs", type=int, default=4, choices=[2, 4, 8, 16],
                    help="풀스케일 ±g — v1 펌웨어에만 사용 (v2는 헤더값 우선, 기본 4)")
    ap.add_argument("--seconds", type=float, default=0,
                    help="지정 초 후 자동 종료 (0=무제한)")
    args = ap.parse_args()

    port = args.port or detect_port()
    if not port:
        print("❌ 시리얼 포트를 찾지 못했습니다. 현재 후보:", file=sys.stderr)
        for p in sorted(glob.glob("/dev/tty[AU]*")):
            print("   ", p, file=sys.stderr)
        print("   · USB 케이블이 '데이터 전송용'인지 확인하세요 (충전 전용 불가)",
              file=sys.stderr)
        print("   · 포트를 직접 지정: --port /dev/ttyACM0", file=sys.stderr)
        return 1

    try:
        # timeout: 데이터가 없어도 주기적으로 루프를 돌려 화면을 갱신한다.
        ser = serial.Serial(port, args.baud, timeout=0.2)
    except serial.SerialException as e:
        print(f"❌ 포트 열기 실패 ({port}): {e}", file=sys.stderr)
        print("   · 'sudo usermod -aG dialout $USER' 후 재로그인이 필요할 수 있습니다.",
              file=sys.stderr)
        print("   · idf.py monitor 등 다른 프로그램이 포트를 쓰고 있지 않은지 확인하세요.",
              file=sys.stderr)
        return 1

    # 중단 후 재개 시 OS 버퍼에 쌓인 과거 데이터를 버리고 시작 (설계문서 7.6)
    ser.reset_input_buffer()

    print("=" * 56)
    print("  IIS3DWB USB 시리얼 수신기")
    print("=" * 56)
    print(f"  포트      : {port}  ({args.baud} 8N1)")
    print(f"  저장 파일 : {'(저장 안 함)' if args.no_csv else args.out}")
    print(f"  풀스케일  : v2 헤더에서 자동 인식 (v1이면 ±{args.fs}g 사용)")
    print("=" * 56)
    print("  ▶ 디바이스 NVS 가 transport=2 (USB 직결) 여야 합니다.")
    print("  ▶ 부팅 후 약 3초 뒤부터 스트리밍이 시작됩니다.")
    print("[수신 대기] … 센서를 흔들면 X/Y/Z 값이 변합니다. (Ctrl+C 종료)")

    f = None
    if not args.no_csv:
        f = open(args.out, "w")
        f.write("recv_iso,seq,timestamp_ms,sample_idx,x_raw,y_raw,z_raw,x_mg,y_mg,z_mg\n")

    buf = bytearray()
    seqt = SeqTracker()
    total_pkts = 0
    total_samples = 0
    resync_bytes = 0       # magic 을 찾으며 버린 바이트 (부팅 로그 등)
    t0 = None
    last_report = time.time()
    win_samples = 0
    cur = (0, 0, 0)
    sens = 0.122
    hdr = None
    start = time.time()

    try:
        while True:
            if args.seconds and (time.time() - start) >= args.seconds:
                print("\n[시간 종료]")
                break

            # in_waiting 만큼 한 번에 읽어 고속(26.6kHz≈162KB/s)에서도 따라간다.
            chunk = ser.read(max(ser.in_waiting, 1))
            if chunk:
                buf += chunk

            # 버퍼에서 꺼낼 수 있는 패킷을 모두 처리한다.
            while True:
                i = buf.find(MAGIC_LE)
                if i < 0:
                    # magic 이 없음 — 경계에 걸친 부분 magic(최대 3B)만 남기고 버린다
                    if len(buf) > 3:
                        resync_bytes += len(buf) - 3
                        del buf[:-3]
                    break
                if i > 0:
                    # magic 앞의 쓰레기(부팅 로그 등) 폐기
                    resync_bytes += i
                    del buf[:i]

                hdr = parse_header(buf, 0, default_fs_g=args.fs)
                if hdr is None:
                    break                      # 헤더가 아직 덜 들어옴

                if hdr.count == 0 or hdr.count > MAX_SAMPLES:
                    # 샘플 데이터에 우연히 나타난 가짜 magic — 4바이트 건너뛰고 재탐색
                    resync_bytes += 4
                    del buf[:4]
                    continue

                if len(buf) < hdr.total_bytes:
                    break                      # 페이로드가 아직 덜 들어옴

                samples = parse_samples(buf, hdr)
                del buf[:hdr.total_bytes]

                sens = hdr.sensitivity
                if t0 is None:
                    t0 = time.time()
                seqt.update(hdr.seq)
                count = hdr.count

                if f is not None:
                    recv_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
                    for k in range(count):
                        xr, yr, zr = samples[k*3], samples[k*3+1], samples[k*3+2]
                        f.write(f"{recv_iso},{hdr.seq},{hdr.timestamp_ms},{k},"
                                f"{xr},{yr},{zr},"
                                f"{xr*sens:.2f},{yr*sens:.2f},{zr*sens:.2f}\n")

                cur = (samples[(count-1)*3], samples[(count-1)*3+1], samples[(count-1)*3+2])
                total_pkts += 1
                total_samples += count
                win_samples += count

            # 1초마다 상태 출력
            now = time.time()
            if now - last_report >= 1.0 and hdr is not None:
                inst_hz = win_samples / (now - last_report)
                lost = seqt.lost
                loss_pct = 100.0 * lost / (total_samples + lost) if (total_samples + lost) else 0
                xm, ym, zm = cur[0]*sens, cur[1]*sens, cur[2]*sens
                target = RATE_HZ.get(hdr.rate_step, 0)
                print(f"\r[{port}] {inst_hz:.0f}/{target}Hz 유실{loss_pct:.2f}% │ "
                      f"X={xm:+8.1f} Y={ym:+8.1f} Z={zm:+8.1f} mg │ "
                      f"±{hdr.full_scale_g}g v{hdr.version} │ "
                      f"샘플={total_samples}   ",
                      end="", flush=True)
                win_samples = 0
                last_report = now

    except KeyboardInterrupt:
        print("\n[종료]")
    finally:
        if f is not None:
            f.close()
        ser.close()
        elapsed = (time.time() - t0) if t0 else 0
        print(f"\n총 패킷={total_pkts}, 샘플={total_samples}, "
              f"추정유실={seqt.lost}, 재동기폐기={resync_bytes}B")
        if elapsed > 0:
            print(f"평균 실효레이트≈{total_samples/elapsed:.0f}Hz (첫 수신 이후 {elapsed:.0f}초)")
        else:
            print("(수신된 데이터 없음 — 디바이스 NVS 의 transport 가 2 인지 확인하세요)")
        if not args.no_csv:
            print(f"저장: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
