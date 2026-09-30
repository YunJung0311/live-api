#!/bin/bash
# 대시보드 주소와 '새 Pi 추가' 명령을 다시 보여줌. 중앙 컴퓨터에서:  bash octo/controller/url.sh
HERE="$(cd "$(dirname "$0")" && pwd)"
python3 - "$HERE/data" <<'PY'
import json
import socket
import sys
from pathlib import Path

data = Path(sys.argv[1])
try:
    token = json.loads((data / "state.json").read_text())["token"]
    info = json.loads((data / "server.json").read_text())
except (OSError, ValueError, KeyError):
    sys.exit("아직 서버가 한 번도 안 켜졌음 → sudo systemctl status live-control 로 확인")
port, public = info["port"], info["public_url"]
try:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
except OSError:
    ip = None
ip_url = f"http://{ip}:{port}" if ip else None
other = ip_url and ip_url != public

print()
print(f"  대시보드 (이 컴퓨터에서):  http://localhost:{port}/?t={token}")
print(f"  대시보드 (노트북·폰에서):  {public}/?t={token}")
if other:
    print(f"    └ 안 열리면 IP로:        {ip_url}/?t={token}")
print()
print("  새 Pi 추가 (각 Pi 터미널에 붙여넣고 엔터 → 대시보드에 뜨는 '연결 요청'에서 허용):")
print(f"    curl -fsSL {public}/join | bash")
if other:
    print("    └ 'Could not resolve host'가 나오면 이걸로:")
    print(f"    curl -fsSL {ip_url}/join | bash")
print()
print("  허용 없이 바로 붙이기 (토큰 포함 — 단톡방 같은 데 올리지 말기):")
print(f'    curl -fsSL "{(ip_url if other else public)}/install.sh?t={token}" | bash')
print()
PY
