"""배포 패키지의 vendor/ 를 import 경로 맨 앞에 넣는다.

폐쇄망 현장에서는 apt / pip 를 쓸 수 없어 배포 패키지가 pyserial 을 vendor/ 에
소스째 넣어 온다. 이 모듈을 import 하면 시스템 것보다 먼저 잡힌다.
개발 트리에는 vendor/ 가 없으므로 아무 일도 하지 않는다.
"""
import sys
from pathlib import Path

VENDOR_DIR = Path(__file__).resolve().parent / "vendor"
if VENDOR_DIR.is_dir() and str(VENDOR_DIR) not in sys.path:
    sys.path.insert(0, str(VENDOR_DIR))
