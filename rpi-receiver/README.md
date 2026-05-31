# IIS3DWB UDP 수신기 (라즈베리파이)

ESP32-S3가 WiFi(UDP)로 보내는 진동 센서 데이터를 라즈베리파이에서 수신·저장합니다.

## 실행

```bash
# 기본: 포트 9000, raw 값으로 vibration.csv 저장
python3 udp_receiver.py

# 포트/파일 지정
python3 udp_receiver.py --port 9000 --out vib.csv

# mg 단위로 변환 저장 (풀스케일 ±4g 기준)
python3 udp_receiver.py --mg --fs 4
```

Python 3.6+ 표준 라이브러리만 사용 (추가 설치 불필요).

## 동작
- UDP 포트(기본 9000)로 수신 대기
- 패킷 헤더 검증(magic "IIS3") 후 샘플 파싱
- CSV로 저장: `seq, timestamp_ms, sample_idx, x, y, z`
- 1초마다 실효 샘플레이트 / 유실률 표시 (seq 기반)

## 디바이스 설정과 맞추기
디바이스(ESP32-S3)에는 이 라즈베리파이의 **IP와 포트**가 설정되어 있어야 합니다.
(설정 툴 또는 NVS로 `server_ip`, `server_port`, `rate_step` 저장)

| rate_step | 샘플레이트 |
|-----------|-----------|
| 0 | 1 kHz (기본) |
| 1 | 3.3 kHz |
| 2 | 6.6 kHz |
| 3 | 13.3 kHz |
| 4 | 26.6 kHz |

## 패킷 포맷
펌웨어 `components/sensor_streamer/sensor_streamer.h` 와 일치:
```
헤더 16B: magic(4,"IIS3") ver(1) rate_step(1) sample_count(2) seq(4) timestamp_ms(4)
페이로드: sample_count × {int16 x, int16 y, int16 z}  (little-endian)
```
- mg 변환은 수신 측에서 (감도 = 풀스케일 의존)
- seq로 패킷 손실/유실률 측정
