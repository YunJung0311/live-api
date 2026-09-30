#!/bin/bash
# Live Agent 설치 — 작품 Pi마다 한 번. 셋 중 편한 걸로:
#   1) curl -fsSL http://live-control.local:8700/join | bash    → 중앙 대시보드에서 '허용'
#   2) curl -fsSL "http://<중앙>:8700/install.sh?t=<토큰>" | bash → 허용 없이 바로 연결
#   3) bash pi-setup.sh [작품 폴더나 zip]                        → 파일로 설치 (중앙이 아직 없어도 됨)
# 다시 실행해도 안전함 (작품·설정·연결 정보 유지). 뭔가 꼬이면 그냥 다시 실행.
# (이 파일은 중앙 서버가 agent 파일을 안에 넣어서 만들어 줌: controller/server.py의 render_setup)
set -euo pipefail

CONTROLLER="${CONTROLLER:-__CONTROLLER__}"
TOKEN="__TOKEN__"
DIR="$HOME/.live-agent"
RUN_PATH="${1:-}"

if [ "$(id -u)" = 0 ]; then
  echo "sudo 없이, 평소 로그인하는 사용자로 실행하세요." >&2
  exit 1
fi
if [ -n "$RUN_PATH" ]; then
  if [ ! -e "$RUN_PATH" ]; then
    echo "작품 폴더/zip이 없음: $RUN_PATH" >&2
    exit 1
  fi
  RUN_PATH="$(realpath "$RUN_PATH")"
fi

echo "==> 1/6 패키지 설치 (조금 걸림)"
sudo apt-get update -qq || true
sudo apt-get install -y -qq python3-venv curl >/dev/null
if ! command -v chromium >/dev/null && ! command -v chromium-browser >/dev/null; then
  sudo apt-get install -y -qq chromium >/dev/null || sudo apt-get install -y -qq chromium-browser >/dev/null
fi
# 있으면 좋은 것 (실패해도 계속): 커서 숨기기 / 스피커·마이크 설정(pactl) / 한글 글꼴 / 화면 목록·배치(2인치 화면)
for pkg in unclutter pulseaudio-utils fonts-nanum wlr-randr x11-xserver-utils; do
  sudo apt-get install -y -qq "$pkg" >/dev/null 2>&1 || echo "    ($pkg 설치 실패 — 건너뜀)"
done

echo "==> 2/6 agent 설치"
mkdir -p "$DIR"
echo "__AGENT_PY__" | base64 -d > "$DIR/agent.py.new"
mv "$DIR/agent.py.new" "$DIR/agent.py"
echo "__BRIDGE_JS__" | base64 -d > "$DIR/live-bridge.js"
echo "__PROJECT_PY__" | base64 -d > "$DIR/project.py"
[ -x "$DIR/venv/bin/python" ] || python3 -m venv "$DIR/venv"
if ! "$DIR/venv/bin/pip" install -q --upgrade "aiohttp>=3.8,<4" 2>/dev/null; then
  echo "    pip 설치 실패 → apt의 python3-aiohttp로 대신"
  sudo apt-get install -y -qq python3-aiohttp >/dev/null
  rm -rf "$DIR/venv"
  python3 -m venv --system-site-packages "$DIR/venv"
fi
"$DIR/venv/bin/python" -c "import aiohttp"
# 중앙 주소는 새로 쓰고, 토큰은 새로 받은 게 있을 때만 바꿈 (다시 실행해도 연결 유지)
python3 - "$DIR/config.json" "$CONTROLLER" "$TOKEN" "${OCTO_ROLE:-agent}" <<'PY'
import json, os, sys
path, controller, token, role = sys.argv[1:5]
try:
    with open(path) as f:
        cfg = json.load(f)
except (OSError, ValueError):
    cfg = {}
cfg["controller"] = controller.rstrip("/")
if role == "master":
    cfg["id"] = "master"
if token:
    cfg["token"] = token
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(cfg, f)
os.chmod(tmp, 0o600)
os.replace(tmp, path)
PY

# 이 Pi에서 직접 쓰는 명령: live status / run / stop / start / restart / key / logs
sudo tee /usr/local/bin/live >/dev/null <<'LIVE'
#!/bin/bash
# 이 Pi의 Live Agent를 직접 다루는 명령
#   live                  지금 상태 (중앙 연결, 작품, 화면)
#   live run <폴더|zip>    이 Pi에 있는 작품을 바로 띄움 (중앙이 없어도 됨)
#   live stop | start | restart
#   live key              Gemini API 키 넣기 (중앙 대시보드에 키가 있으면 그게 우선)
#   live logs             최근 로그 (계속 보기: journalctl -u live-agent -f)
CONFIG="$HOME/.live-agent/config.json"
PORT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("bridge_port", 8765))' "$CONFIG" 2>/dev/null || echo 8765)"
API="http://127.0.0.1:$PORT/local"
call() {
  if ! curl -fsS --noproxy '*' -H "X-Live-Local: 1" "$@"; then
    echo "agent가 응답 없음 → sudo systemctl restart live-agent  (로그: journalctl -u live-agent -f)" >&2
    return 1
  fi
}
json_of() { python3 -c 'import json,sys; print(json.dumps({sys.argv[1]: sys.stdin.read().strip()}))' "$1"; }
case "${1:-status}" in
  status) call "$API/status" ;;
  run)
    if [ -z "${2:-}" ] || [ ! -e "$2" ]; then echo "사용법: live run <작품 폴더 또는 zip>" >&2; exit 1; fi
    echo "작품 준비 중… 잠시 뒤 전체화면으로 뜸 (끄기: 터미널에서 live stop / 터미널 열기: Ctrl+Alt+T)"
    realpath "$2" | json_of path | call -X POST -H "Content-Type: application/json" --data-binary @- "$API/run" ;;
  stop|start|restart) call -X POST "$API/$1" ;;
  key)
    read -rsp "Gemini API 키 붙여넣기 (화면에 안 보임, 지우려면 그냥 Enter): " key; echo
    printf '%s' "$key" | json_of key | call -X POST -H "Content-Type: application/json" --data-binary @- "$API/key" ;;
  logs) call "$API/logs" ;;
  *) sed -n '2,8p' "$0" ;;
esac
LIVE
sudo chmod 755 /usr/local/bin/live

echo "==> 3/6 부팅 시 자동 실행 등록 (systemd)"
USER_NAME="$(id -un)"
USER_ID="$(id -u)"
sudo tee /etc/systemd/system/live-agent.service >/dev/null <<EOF
[Unit]
Description=Live Agent (central control client)
After=network-online.target sound.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
User=$USER_NAME
Environment=XDG_RUNTIME_DIR=/run/user/$USER_ID
Environment=PYTHONUNBUFFERED=1
ExecStart="$DIR/venv/bin/python" "$DIR/agent.py"
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
echo "$USER_NAME ALL=(root) NOPASSWD: /usr/bin/systemctl reboot, /usr/bin/systemctl poweroff" \
  | sudo tee /etc/sudoers.d/live-agent >/dev/null
sudo chmod 440 /etc/sudoers.d/live-agent

echo "==> 4/6 전시용 설정 (데스크톱 자동 로그인, 화면 꺼짐 방지)"
if command -v raspi-config >/dev/null; then
  sudo raspi-config nonint do_boot_behaviour B4 || true
  sudo raspi-config nonint do_blanking 1 || true
fi

echo "==> 5/6 네트워크·시간 (Wi-Fi 절전 끄기, 한국 시간)"
# Wi-Fi 절전이 켜져 있으면 음성 스트리밍이 끊기고 대시보드 연결이 자주 튕김 (다음 부팅부터 적용)
if [ -d /etc/NetworkManager/conf.d ]; then
  printf '[connection]\nwifi.powersave = 2\n' | sudo tee /etc/NetworkManager/conf.d/99-live-agent.conf >/dev/null
fi
TZ_NOW="$(timedatectl show -p Timezone --value 2>/dev/null || true)"
case "$TZ_NOW" in
  ""|UTC|Etc/UTC) sudo timedatectl set-timezone Asia/Seoul 2>/dev/null || true ;;
esac

echo "==> 6/6 agent 시작"
sudo systemctl daemon-reload
sudo systemctl enable live-agent >/dev/null
sudo systemctl restart live-agent
LIVE="$(command -v live || echo /usr/local/bin/live)"
for _ in $(seq 1 20); do
  if "$LIVE" status >/dev/null 2>&1; then break; fi
  sleep 1
done

echo
if [ ! -x /usr/sbin/lightdm ]; then
  echo "주의: 데스크톱이 없는 OS(Lite)로 보임 → 화면에 작품을 못 띄움."
  echo "      'Raspberry Pi OS (with desktop)'로 SD카드를 다시 굽는 걸 추천."
  echo
fi
if [ -n "$RUN_PATH" ]; then
  "$LIVE" run "$RUN_PATH" || true
  echo
fi
echo "완료! 이 Pi($(hostname)) 상태:"
"$LIVE" status || true
echo
if grep -q '"token"' "$DIR/config.json"; then
  echo "  → 중앙 대시보드에 이 Pi가 보여야 함"
else
  echo "  → 중앙 대시보드 위쪽에 '연결 요청'이 뜸 → '허용' 누르기"
  echo "    (중앙이 아직 안 켜져 있어도 괜찮음: 켜지면 알아서 요청함)"
fi
echo "  - 작품 바로 띄우기 (중앙 없이도):  live run ~/작품폴더   · 끄기: live stop   · 키: live key"
echo "  - 처음 설치했으면 한 번 재부팅 (자동 로그인·Wi-Fi 절전 설정 적용):  sudo reboot"
