#!/bin/bash
# 헐 실행 (라즈베리파이 전시용) — 2인치 '보조 모니터'에 전체화면으로 띄움.
# 전원 켤 때 자동 실행에는 이 파일 하나만 등록하면 됨.
DIR="$(cd "$(dirname "$0")" && pwd)"
export DISPLAY="${DISPLAY:-:0}"

# ── 설정 ─────────────────────────────────────────────
TARGET=""        # 2인치 모니터 이름 (비워두면 가장 작은 모니터를 자동 선택). 예: SPI-1
ROTATE=auto      # auto = 세로로 잡혀 있으면 270도 돌림. 얼굴이 반대로 누우면 90, 똑바르면 0
# ────────────────────────────────────────────────────

xset s off -dpms s noblank 2>/dev/null                       # 화면 꺼짐 방지
command -v unclutter >/dev/null && unclutter -idle 0.5 -root &  # 마우스 커서 숨김
sleep 5                                                      # 부팅 직후 화면/네트워크/오디오 준비 대기

# 2인치 모니터의 위치와 크기 찾기 → "이름 폭 높이 X Y"
GEOM="$(TARGET="$TARGET" python3 - <<'PY'
import os, re, subprocess
want = os.environ.get("TARGET", "")
mons = []
def run(cmd):
    try: return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
    except Exception: return ""
if os.environ.get("WAYLAND_DISPLAY"):
    name = None; cur = None
    for line in run(["wlr-randr"]).splitlines():
        if line and not line[0].isspace():
            name = line.split()[0]; cur = {"name": name}; mons.append(cur)
        elif cur is not None:
            m = re.search(r"(\d+)x(\d+) px.*current", line)
            if m: cur["w"], cur["h"] = int(m[1]), int(m[2])
            m = re.search(r"Position: (-?\d+),(-?\d+)", line)
            if m: cur["x"], cur["y"] = int(m[1]), int(m[2])
            m = re.search(r"Transform: (\S+)", line)
            if m and m[1] in ("90", "270", "flipped-90", "flipped-270") and "w" in cur:
                cur["w"], cur["h"] = cur["h"], cur["w"]
    mons = [m for m in mons if {"w", "h", "x", "y"} <= m.keys()]
else:
    for line in run(["xrandr", "--listmonitors"]).splitlines():
        m = re.search(r"(\d+)/\d+x(\d+)/\d+\+(-?\d+)\+(-?\d+)\s+(\S+)\s*$", line)
        if m: mons.append({"w": int(m[1]), "h": int(m[2]), "x": int(m[3]), "y": int(m[4]), "name": m[5]})
if mons:
    pick = next((m for m in mons if m["name"] == want), None) or min(mons, key=lambda m: m["w"] * m["h"])
    print(pick["name"], pick["w"], pick["h"], pick["x"], pick["y"])
PY
)"
read -r NAME W H X Y <<< "$GEOM"
echo "모니터 목록에서 선택: ${NAME:-못 찾음} ${W}x${H} @ ${X},${Y}"

if [ "$ROTATE" = "auto" ]; then
  if [ -n "$W" ] && [ "$H" -gt "$W" ]; then ROTATE=270; else ROTATE=0; fi
fi

POS=()
[ -n "$NAME" ] && POS=(--window-position="$X,$Y" --window-size="$W,$H")
# Wayland에서는 창 위치 지정이 안 되는 경우가 많아서 X11(Xwayland) 모드로 띄움
[ -n "$WAYLAND_DISPLAY" ] && POS+=(--ozone-platform=x11)

BROWSER="$(command -v chromium-browser || command -v chromium)"
# --password-store=basic: 자동 로그인 상태에서 '키링 비밀번호' 창이 떠서 화면을 가리는 것 방지
# --allow-file-access-from-files: file:// 페이지에서 fetch("persona.txt")가 되게
exec "$BROWSER" "${POS[@]}" \
  --kiosk --force-device-scale-factor=1 \
  --use-fake-ui-for-media-stream \
  --autoplay-policy=no-user-gesture-required \
  --no-first-run --password-store=basic --allow-file-access-from-files \
  --noerrdialogs --disable-infobars --disable-session-crashed-bubble --hide-crash-restore-bubble \
  --disable-features=Translate --check-for-update-interval=31536000 \
  --user-data-dir="$HOME/.heol-chromium" \
  "file://$DIR/heol.html?rotate=$ROTATE$1"
