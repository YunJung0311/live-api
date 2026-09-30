#!/bin/bash
# 중앙 라즈베리 파이에 Live Control(대시보드 서버)을 설치. 중앙 Pi에서 딱 한 번:
#     bash local-server/controller/install-central.sh
# - 부팅하면 자동으로 켜지고, 죽으면 3초 뒤 다시 켜짐 (systemd 서비스 이름: live-control)
# - 다시 실행해도 안전함 (토큰·기기 목록·올린 작품은 그대로)
# 옵션:
#     PORT=8800 bash install-central.sh                               포트 바꾸기
#     PUBLIC_URL=http://192.168.0.10:8700 bash install-central.sh     Pi들이 접속할 주소를 직접 지정
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PORT="${PORT:-8700}"
DEFAULT_NAME="live-control"

if [ "$(id -u)" = 0 ]; then
  echo "sudo 없이, 평소 로그인하는 사용자로 실행하세요." >&2
  exit 1
fi

echo "==> 1/4 패키지 설치"
sudo apt-get update -qq || true
sudo apt-get install -y -qq python3-venv avahi-daemon >/dev/null

echo "==> 2/4 이 Pi의 이름 (다른 Pi들이 이 이름.local 로 찾아옴)"
if [ "$(hostname)" = "raspberrypi" ]; then
  # 기본 이름 그대로면 다른 Pi들과 겹치니까 바꿈
  if grep -q '^127\.0\.1\.1' /etc/hosts; then
    sudo sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t$DEFAULT_NAME/" /etc/hosts
  else
    printf '127.0.1.1\t%s\n' "$DEFAULT_NAME" | sudo tee -a /etc/hosts >/dev/null
  fi
  if sudo hostnamectl set-hostname "$DEFAULT_NAME" 2>/dev/null; then
    echo "    이름을 raspberrypi → $DEFAULT_NAME 로 바꿈"
  else
    echo "    (이름 바꾸기 실패 — 그대로 진행)"
  fi
fi
sudo systemctl restart avahi-daemon 2>/dev/null || true
HOST="$(hostname)"
PUBLIC_URL="${PUBLIC_URL:-http://$HOST.local:$PORT}"
# 중앙은 모두가 붙는 곳이라 Wi-Fi 절전도 끔 (다음 부팅부터 적용. 가능하면 랜선 연결 추천)
if [ -d /etc/NetworkManager/conf.d ]; then
  printf '[connection]\nwifi.powersave = 2\n' | sudo tee /etc/NetworkManager/conf.d/99-live-control.conf >/dev/null
fi

echo "==> 3/4 파이썬 환경"
[ -x "$HERE/.venv/bin/python" ] || python3 -m venv "$HERE/.venv"
if ! "$HERE/.venv/bin/pip" install -q -r "$HERE/requirements.txt" 2>/dev/null; then
  echo "    pip 설치 실패 → apt의 python3-aiohttp로 대신"
  sudo apt-get install -y -qq python3-aiohttp >/dev/null
  rm -rf "$HERE/.venv"
  python3 -m venv --system-site-packages "$HERE/.venv"
fi
"$HERE/.venv/bin/python" -c "import aiohttp"

echo "==> 4/4 부팅 시 자동 실행 등록 (systemd: live-control)"
sudo tee /etc/systemd/system/live-control.service >/dev/null <<EOF
[Unit]
Description=Live Control (central dashboard)
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
User=$(id -un)
WorkingDirectory=$HERE
Environment=PYTHONUNBUFFERED=1
ExecStart="$HERE/.venv/bin/python" "$HERE/server.py" --port $PORT --public-url $PUBLIC_URL
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable live-control >/dev/null
sudo systemctl restart live-control

# 서버가 뜰 때까지 잠깐 기다렸다가 주소 출력
for _ in $(seq 1 30); do
  if [ -f "$HERE/data/server.json" ] && curl -s -o /dev/null "http://127.0.0.1:$PORT/"; then
    break
  fi
  sleep 1
done
echo
echo "완료! 중앙 서버가 켜졌고, 이 Pi를 재부팅해도 자동으로 켜짐."
bash "$HERE/url.sh"
echo "  주소를 다시 보려면:  bash $HERE/url.sh"
echo "  서버 로그:          journalctl -u live-control -f"
