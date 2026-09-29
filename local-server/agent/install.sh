#!/bin/bash
# Live Agent 설치 — 중앙 컴퓨터(controller)가 주소와 토큰을 채워서 내려주는 스크립트.
# 각 라즈베리 파이 터미널에서 딱 한 번:  curl -fsSL "http://<중앙>:8700/install.sh?t=<토큰>" | bash
# 다시 실행해도 안전함 (기존 프로젝트·설정은 유지).
set -euo pipefail

CONTROLLER="__CONTROLLER__"
TOKEN="__TOKEN__"
DIR="$HOME/.live-agent"

if [ "$(id -u)" = 0 ]; then
  echo "sudo 없이, 평소 로그인하는 사용자로 실행하세요." >&2
  exit 1
fi

echo "==> 1/5 패키지 설치 (조금 걸림)"
sudo apt-get update -qq || true
sudo apt-get install -y -qq python3-venv curl unclutter >/dev/null
if ! command -v chromium >/dev/null && ! command -v chromium-browser >/dev/null; then
  sudo apt-get install -y -qq chromium >/dev/null || sudo apt-get install -y -qq chromium-browser >/dev/null
fi

echo "==> 2/5 agent 다운로드"
mkdir -p "$DIR"
curl -fsSL -H "X-Token: $TOKEN" "$CONTROLLER/agent/agent.py" -o "$DIR/agent.py"
curl -fsSL -H "X-Token: $TOKEN" "$CONTROLLER/agent/live-bridge.js" -o "$DIR/live-bridge.js"
[ -x "$DIR/venv/bin/python" ] || python3 -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install -q --upgrade aiohttp

cat > "$DIR/config.json" <<EOF
{"controller": "$CONTROLLER", "token": "$TOKEN"}
EOF
chmod 600 "$DIR/config.json"

echo "==> 3/5 부팅 시 자동 실행 등록 (systemd)"
USER_NAME="$(id -un)"
USER_ID="$(id -u)"
sudo tee /etc/systemd/system/live-agent.service >/dev/null <<EOF
[Unit]
Description=Live Agent (central control client)
After=network-online.target sound.target
Wants=network-online.target

[Service]
User=$USER_NAME
Environment=XDG_RUNTIME_DIR=/run/user/$USER_ID
Environment=PYTHONUNBUFFERED=1
ExecStart=$DIR/venv/bin/python $DIR/agent.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
echo "$USER_NAME ALL=(root) NOPASSWD: /usr/bin/systemctl reboot, /usr/bin/systemctl poweroff" \
  | sudo tee /etc/sudoers.d/live-agent >/dev/null
sudo chmod 440 /etc/sudoers.d/live-agent

echo "==> 4/5 전시용 설정 (데스크톱 자동 로그인, 화면 꺼짐 방지)"
if command -v raspi-config >/dev/null; then
  sudo raspi-config nonint do_boot_behaviour B4 || true
  sudo raspi-config nonint do_blanking 1 || true
fi

echo "==> 5/5 agent 시작"
sudo systemctl daemon-reload
sudo systemctl enable live-agent >/dev/null
sudo systemctl restart live-agent

echo
echo "완료! 이제 이 기기($(hostname))가 대시보드에 보여야 함."
echo "안 보이면: journalctl -u live-agent -f"
