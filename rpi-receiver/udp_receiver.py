#!/usr/bin/env python3
"""
IIS3DWB 센서 데이터 UDP 수신 프로그램 (라즈베리파이)

ESP32-S3가 UDP로 보내는 진동 데이터를 수신·파싱하여
CSV로 저장하고, 실시간 처리량/유실률을 표시합니다.

패킷 포맷은 `iis3dwb_packet.py` 에 정의되어 있습니다 (v1 16B / v2 18B 헤더).
v2 헤더는 풀스케일(full_scale_g)을 담고 있어 `--fs` 없이도 mg 환산이 정확합니다.

사용법:
  python3 udp_receiver.py                       # 포트 9000, vibration.csv 저장
  python3 udp_receiver.py --port 9000 --out vib.csv
  python3 udp_receiver.py --fs 4                # v1 펌웨어용 풀스케일 지정
"""

import socket
import argparse
import os
import sys
import time
from datetime import datetime

# 패킷 파서의 정본은 배포물인 ../rpi-collector/ 에 있다. 이 CLI 는 개발·디버그용으로
# 남은 것이라, 사본을 두지 않고 그쪽을 임포트해 파서가 갈라지지 않게 한다.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "rpi-collector"))

from iis3dwb_packet import (
    RATE_HZ, SENSITIVITY, parse_header, parse_samples, SeqTracker,
)


def local_ip():
    """이 라즈베리파이가 네트워크에서 가지는 IP를 알아냄.
    이 값을 디바이스(ESP32) NVS의 server_ip 에 넣어야 한다."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))   # 실제 전송 X, 출구 IP만 확인
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def main():
    ap = argparse.ArgumentParser(description="IIS3DWB UDP 수신기")
    ap.add_argument("--port", type=int, default=9000, help="수신 포트 (기본 9000)")
    ap.add_argument("--out", default="vibration.csv",
                    help="저장할 CSV 파일 (raw+mg 모두 기록)")
    ap.add_argument("--fs", type=int, default=4, choices=[2, 4, 8, 16],
                    help="풀스케일 ±g — v1 펌웨어에만 사용 (v2는 헤더값 우선, 기본 4)")
    args = ap.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", args.port))
    sock.settimeout(1.0)

    ip = local_ip()
    print("=" * 52)
    print("  IIS3DWB UDP 수신기")
    print("=" * 52)
    print(f"  이 라즈베리파이 IP : {ip}")
    print(f"  수신 포트          : {args.port}")
    print()
    print("  ▶ 디바이스(ESP32) NVS 에 아래 값을 넣으세요:")
    print(f"      server_ip   = {ip}")
    print(f"      server_port = {args.port}")
    print("=" * 52)
    print(f"  저장 파일 : {args.out}  (raw + mg 전체 샘플)")
    print(f"  풀스케일  : v2 헤더에서 자동 인식 (v1이면 ±{args.fs}g 사용)")
    print("[수신 대기] … 센서를 흔들면 X/Y/Z 값이 변합니다. (Ctrl+C 종료)")

    f = open(args.out, "w")
    # 실제 센서 데이터: raw(LSB)와 mg(가속도) 둘 다 기록 + 수신 시각
    f.write("recv_iso,seq,timestamp_ms,sample_idx,x_raw,y_raw,z_raw,x_mg,y_mg,z_mg\n")

    seqt = SeqTracker()
    total_pkts = 0
    total_samples = 0
    bad_pkts = 0           # magic 불일치·길이 부족으로 버린 패킷
    t0 = None              # 첫 패킷 도착 시각 (대기시간 제외 위해 지연 설정)
    last_report = time.time()
    win_samples = 0        # 직전 보고 이후 누적 (순간 레이트용)
    cur = (0, 0, 0)        # 화면 표시용 최신 샘플 (raw)
    sens = SENSITIVITY[args.fs]
    hdr = None

    try:
        while True:
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue

            # 헤더 파싱 — version 에 따라 16B(v1) / 18B(v2) 를 자동 판별한다.
            hdr = parse_header(data, 0, default_fs_g=args.fs)
            if hdr is None:
                bad_pkts += 1
                continue

            samples = parse_samples(data, hdr)
            if samples is None:
                bad_pkts += 1
                continue

            # 감도는 v2 헤더의 풀스케일에서 — 사용자 지정 불필요
            sens = hdr.sensitivity

            # 첫 패킷 도착 시각 기준으로 평균 계산 (수신기 대기시간 제외)
            if t0 is None:
                t0 = time.time()

            seqt.update(hdr.seq)
            count = hdr.count

            # 수신 시각 (사람이 읽는 ISO, ms 정밀도)
            recv_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]

            # CSV 기록: raw(LSB) + mg(가속도) 모두 — 실제 센서 데이터
            for i in range(count):
                xr, yr, zr = samples[i*3], samples[i*3+1], samples[i*3+2]
                f.write(f"{recv_iso},{hdr.seq},{hdr.timestamp_ms},{i},{xr},{yr},{zr},"
                        f"{xr*sens:.2f},{yr*sens:.2f},{zr*sens:.2f}\n")

            # 화면 표시용: 이 패킷의 마지막 샘플 (가장 최신)
            cur = (samples[(count-1)*3], samples[(count-1)*3+1], samples[(count-1)*3+2])

            total_pkts += 1
            total_samples += count
            win_samples += count

            # 1초마다 상태 출력
            now = time.time()
            if now - last_report >= 1.0:
                # 순간 레이트: 직전 보고 이후 구간 (실제 수신 속도)
                inst_hz = win_samples / (now - last_report)
                lost = seqt.lost
                loss_pct = 100.0 * lost / (total_samples + lost) if (total_samples + lost) else 0
                # 실제 센서값 (최신 샘플) — mg 단위로 표시. 흔들면 값이 변함.
                xm, ym, zm = cur[0]*sens, cur[1]*sens, cur[2]*sens
                target = RATE_HZ.get(hdr.rate_step, 0)
                print(f"\r[{addr[0]}] {inst_hz:.0f}/{target}Hz 유실{loss_pct:.2f}% │ "
                      f"X={xm:+8.1f} Y={ym:+8.1f} Z={zm:+8.1f} mg │ "
                      f"±{hdr.full_scale_g}g v{hdr.version} │ "
                      f"샘플={total_samples}   ",
                      end="", flush=True)
                win_samples = 0
                last_report = now

    except KeyboardInterrupt:
        print("\n[종료]")
    finally:
        f.close()
        elapsed = (time.time() - t0) if t0 else 0
        print(f"\n총 패킷={total_pkts}, 샘플={total_samples}, 추정유실={seqt.lost}, 불량패킷={bad_pkts}")
        if elapsed > 0:
            print(f"평균 실효레이트≈{total_samples/elapsed:.0f}Hz (첫 수신 이후 {elapsed:.0f}초)")
        else:
            print("(수신된 데이터 없음)")
        print(f"저장: {args.out}")


if __name__ == "__main__":
    main()
