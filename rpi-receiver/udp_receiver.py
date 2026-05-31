#!/usr/bin/env python3
"""
IIS3DWB 센서 데이터 UDP 수신 프로그램 (라즈베리파이)

ESP32-S3가 UDP로 보내는 진동 데이터를 수신·파싱하여
CSV로 저장하고, 실시간 처리량/유실률을 표시합니다.

패킷 포맷 (펌웨어 sensor_streamer.h 와 일치):
  헤더 16B: magic(4) version(1) rate_step(1) sample_count(2) seq(4) timestamp_ms(4)
  페이로드: sample_count × (int16 x, int16 y, int16 z)  [little-endian]

사용법:
  python3 udp_receiver.py                 # 포트 9000, raw 값 CSV 저장
  python3 udp_receiver.py --port 9000 --out vib.csv
  python3 udp_receiver.py --mg            # mg 단위로 변환 저장 (--fs 로 풀스케일 지정)
"""

import socket
import struct
import argparse
import time
import sys

MAGIC = 0x49495333  # "IIS3"
HEADER = struct.Struct("<IBBHII")  # 16 bytes
HEADER_SIZE = HEADER.size

# rate_step → Hz (펌웨어와 일치)
RATE_HZ = {0: 1000, 1: 3333, 2: 6667, 3: 13333, 4: 26667}

# 풀스케일별 감도 (mg/LSB) — 펌웨어 기본 ±4g
SENSITIVITY = {2: 0.061, 4: 0.122, 8: 0.244, 16: 0.488}


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
    ap.add_argument("--out", default="vibration.csv", help="저장할 CSV 파일")
    ap.add_argument("--mg", action="store_true", help="raw 대신 mg 단위로 저장")
    ap.add_argument("--fs", type=int, default=4, choices=[2, 4, 8, 16],
                    help="풀스케일 ±g (mg 변환용, 기본 4)")
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
    print(f"[수신 대기] … (Ctrl+C 종료)")

    sens = SENSITIVITY[args.fs]
    f = open(args.out, "w")
    f.write("seq,timestamp_ms,sample_idx,x,y,z\n")

    last_seq = None
    total_pkts = 0
    total_samples = 0
    lost = 0
    t0 = None              # 첫 패킷 도착 시각 (대기시간 제외 위해 지연 설정)
    last_report = time.time()
    win_samples = 0        # 직전 보고 이후 누적 (순간 레이트용)

    try:
        while True:
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue

            if len(data) < HEADER_SIZE:
                continue
            magic, ver, rate, count, seq, ts = HEADER.unpack_from(data, 0)
            if magic != MAGIC:
                continue

            # 첫 패킷 도착 시각 기준으로 평균 계산 (수신기 대기시간 제외)
            if t0 is None:
                t0 = time.time()

            # 손실 감지 (seq 불연속)
            if last_seq is not None:
                gap = (seq - last_seq - 1) & 0xFFFFFFFF
                if 0 < gap < 1000000:
                    lost += gap
            last_seq = seq

            # 샘플 파싱
            expected = HEADER_SIZE + count * 6
            if len(data) < expected:
                continue
            samples = struct.unpack_from("<%dh" % (count * 3), data, HEADER_SIZE)

            # CSV 기록 (raw 또는 mg)
            for i in range(count):
                x, y, z = samples[i*3], samples[i*3+1], samples[i*3+2]
                if args.mg:
                    x, y, z = x * sens, y * sens, z * sens
                    f.write(f"{seq},{ts},{i},{x:.2f},{y:.2f},{z:.2f}\n")
                else:
                    f.write(f"{seq},{ts},{i},{x},{y},{z}\n")

            total_pkts += 1
            total_samples += count
            win_samples += count

            # 1초마다 상태 출력
            now = time.time()
            if now - last_report >= 1.0:
                # 순간 레이트: 직전 보고 이후 구간 (실제 수신 속도 = 1000Hz여야)
                inst_hz = win_samples / (now - last_report)
                # 평균 레이트: 첫 패킷 도착 이후 (대기시간 제외)
                elapsed = now - t0
                eff_hz = total_samples / elapsed if elapsed > 0 else 0
                loss_pct = 100.0 * lost / (total_samples + lost) if (total_samples + lost) else 0
                print(f"\r[{addr[0]}] 패킷={total_pkts} 샘플={total_samples} "
                      f"순간≈{inst_hz:.0f}Hz 평균≈{eff_hz:.0f}Hz "
                      f"(목표 {RATE_HZ.get(rate,'?')}Hz) 유실≈{loss_pct:.2f}%   ",
                      end="", flush=True)
                win_samples = 0
                last_report = now

    except KeyboardInterrupt:
        print("\n[종료]")
    finally:
        f.close()
        elapsed = (time.time() - t0) if t0 else 0
        print(f"\n총 패킷={total_pkts}, 샘플={total_samples}, 추정유실={lost}")
        if elapsed > 0:
            print(f"평균 실효레이트≈{total_samples/elapsed:.0f}Hz (첫 수신 이후 {elapsed:.0f}초)")
        else:
            print("(수신된 데이터 없음)")
        print(f"저장: {args.out}")


if __name__ == "__main__":
    main()
