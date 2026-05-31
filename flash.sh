#!/bin/bash
# ESP32 자동 포트 감지 및 플래시/모니터 스크립트

# ESP-IDF 환경 활성화
source ~/esp/v5.4.3/esp-idf/export.sh > /dev/null 2>&1

# USB 시리얼 포트 자동 감지
PORT=$(ls /dev/cu.usbserial-* /dev/cu.usbmodem* 2>/dev/null | grep -v Bluetooth | head -1)

if [ -z "$PORT" ]; then
    echo "ERROR: ESP32 디바이스를 찾을 수 없습니다."
    echo "USB 케이블 연결을 확인하세요."
    exit 1
fi

echo "감지된 포트: $PORT"
echo ""

# 인자에 따라 동작 선택
case "$1" in
    "flash")
        echo "플래시 중..."
        idf.py -p "$PORT" flash
        ;;
    "monitor")
        echo "모니터 시작..."
        idf.py -p "$PORT" monitor
        ;;
    "build")
        echo "빌드 중..."
        idf.py build
        ;;
    *)
        echo "빌드 + 플래시 + 모니터 실행..."
        idf.py -p "$PORT" flash monitor
        ;;
esac
