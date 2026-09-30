"""Live Agent: runs on every Raspberry Pi.

- Keeps one WebSocket open to the controller (reconnects forever, and re-finds it by
  UDP beacon if the controller's IP changed, e.g. after moving to the exhibition router).
- Downloads, installs and keeps one student project alive.
- Serves ws://127.0.0.1:8765/bridge so the project page can be told to wake/sleep.
- Lets the dashboard pick this Pi's speaker/mic, set volume and test them (pactl).
- No token yet (installed with /join or pi-setup.sh)? Asks the controller to pair; the
  dashboard shows "연결 요청" and a click on 허용 hands the token over.
- `live` command on the Pi (local API on 127.0.0.1) can run a project straight from a
  folder/zip on the Pi, even with no controller at all.
- Puts the project on the smallest screen (the 2-inch display) even when an HDMI monitor
  is plugged in: every Chromium launch gets that screen's position/size (Xwayland on labwc).
"""

import array
import copy
import base64
import asyncio
import contextlib
import hashlib
import hmac
import json
import math
import os
import re
import shutil
import signal
import socket
import string
import subprocess
import time
import unicodedata
import wave
import zipfile
from collections import deque
from pathlib import Path
from urllib.parse import quote, urlparse

from aiohttp import ClientSession, ClientTimeout, WSMsgType, WSServerHandshakeError, web

# __OCTO_PROJECT_BOOTSTRAP__
from project import extract_zip, safe_parts, guess_manifest, validate_manifest

VERSION = "1.0.0"
HOME = Path(__file__).resolve().parent
CONFIG = HOME / "config.json"
STATE = HOME / "state.json"
PROJECT = HOME / "project"
SETUP_MARK = HOME / "setup.done"
BRIDGE_JS = HOME / "live-bridge.js"
CHROMIUM_LOG = HOME / "chromium.log"
TEST_TONE = HOME / "test-tone.wav"
BIN = HOME / "bin"
BEACON_PORT = 8701
DEFAULT_CONTROLLER = "http://live-control.local:8700"
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", "__MACOSX"}
JUNK_FILES = {".DS_Store", "Thumbs.db", "desktop.ini"}
CHROMIUM_FLAGS = [
    "--kiosk",
    "--no-first-run",
    "--no-default-browser-check",
    "--noerrdialogs",
    "--disable-infobars",
    "--disable-session-crashed-bubble",
    "--hide-crash-restore-bubble",
    "--disable-features=Translate",
    "--check-for-update-interval=31536000",
    "--password-store=basic",
    "--use-fake-ui-for-media-stream",
    "--autoplay-policy=no-user-gesture-required",
]


# 작품 스크립트가 직접 Chromium을 켤 때 끼어들어서 전시용 옵션을 붙이는 래퍼 (~/.live-agent/bin)
CHROMIUM_WRAPPER = r"""#!/bin/bash
# Live Agent가 만든 파일 — 작품 스크립트가 Chromium을 켜면 전시에 필요한 옵션을 자동으로 붙임
# (마이크 권한 자동 허용 · 소리 자동 재생 · 키링 비밀번호 창/오류 창 끄기)
here="$(cd "$(dirname "$0")" && pwd -P)"
real=""
IFS=: read -ra dirs <<< "$PATH"
for d in "${dirs[@]}"; do
  [ "$(cd "$d" 2>/dev/null && pwd -P)" = "$here" ] && continue
  for b in chromium-browser chromium; do
    if [ -x "$d/$b" ]; then real="$d/$b"; break 2; fi
  done
done
if [ -z "$real" ]; then echo "chromium이 설치되어 있지 않음" >&2; exit 127; fi
# 2인치 화면 위치·크기 (agent가 LIVE_CHROMIUM_FLAGS로 넘겨줌). 스크립트가 직접 준 옵션이 뒤에 오므로 그게 우선
read -ra place <<< "${LIVE_CHROMIUM_FLAGS:-}"
exec "$real" --use-fake-ui-for-media-stream --autoplay-policy=no-user-gesture-required \
  --password-store=basic --no-first-run --no-default-browser-check --noerrdialogs \
  --disable-infobars --hide-crash-restore-bubble --disable-features=Translate \
  --check-for-update-interval=31536000 "${place[@]}" "$@"
"""


def ensure_wrappers():
    BIN.mkdir(exist_ok=True)
    for name in ("chromium-browser", "chromium", "google-chrome"):
        path = BIN / name
        if not path.exists() or path.read_text() != CHROMIUM_WRAPPER:
            path.write_text(CHROMIUM_WRAPPER)
        path.chmod(0o755)


def group_alive(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


async def kill_group(pgid, grace=5):
    """프로세스 그룹(스크립트 + 스크립트가 띄운 서버·브라우저)을 끔. 안 꺼지면 강제로."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, signal.SIGTERM)
    for _ in range(int(grace * 10)):
        if not group_alive(pgid):
            return
        await asyncio.sleep(0.1)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, signal.SIGKILL)


# ----- 화면(2인치 디스플레이) -----

# 방향: 대시보드 값 → (wlr-randr transform, xrandr rotate). 둘 다 반시계 방향 기준
ROTATIONS = {"0": ("normal", "normal"), "90": ("90", "left"), "180": ("180", "inverted"), "270": ("270", "right")}
SCREEN_DEFAULT = {"target": "auto", "rotate": "keep", "only": False}


def finish_output(o):
    modes = o.get("modes") or []
    mode = (next((m for m in modes if m[2]), None) or next((m for m in modes if m[3]), None)
            or (modes[0] if modes else (0, 0, False, False)))
    w, h = mode[0], mode[1]
    transform = str(o.get("transform") or "normal")
    if transform.endswith(("90", "270")):
        w, h = h, w
    scale = float(o.get("scale") or 1.0) or 1.0
    rotate = {"normal": "0", "90": "90", "180": "180", "270": "270"}.get(transform, transform)
    return {"name": o["name"], "label": o.get("label") or "", "enabled": bool(o.get("enabled", True)) and w > 0,
            "x": int(o.get("x", 0)), "y": int(o.get("y", 0)), "w": round(w / scale), "h": round(h / scale),
            "rotate": rotate}


def parse_wlr_randr(text):
    """wlr-randr 글자 출력 → 화면 목록 [{name, x, y, w, h, enabled, rotate}]"""
    outputs, cur = [], None
    for line in text.splitlines():
        if not line.strip():
            continue
        if not line[0].isspace():
            name = line.split()[0]
            cur = {"name": name, "label": line[len(name):].strip().strip('"'), "enabled": True, "modes": []}
            outputs.append(cur)
            continue
        if cur is None:
            continue
        item = line.strip()
        m = re.match(r"(\d+)x(\d+) px", item)
        if m:
            cur["modes"].append((int(m[1]), int(m[2]), "current" in item, "preferred" in item))
            continue
        key, _, value = item.partition(":")
        value = value.strip()
        if key == "Enabled":
            cur["enabled"] = value == "yes"
        elif key == "Position":
            x, _, y = value.partition(",")
            with contextlib.suppress(ValueError):
                cur["x"], cur["y"] = int(x), int(y)
        elif key == "Transform":
            cur["transform"] = value
        elif key == "Scale":
            with contextlib.suppress(ValueError):
                cur["scale"] = float(value)
    return [finish_output(o) for o in outputs]


def parse_wlr_randr_json(data):
    outputs = []
    for o in data:
        modes = [(m.get("width", 0), m.get("height", 0), bool(m.get("current")), bool(m.get("preferred")))
                 for m in o.get("modes") or []]
        pos = o.get("position") or {}
        outputs.append(finish_output({
            "name": o.get("name") or "?", "label": o.get("description") or "", "enabled": o.get("enabled", True),
            "modes": modes, "x": pos.get("x", 0), "y": pos.get("y", 0),
            "transform": o.get("transform") or "normal", "scale": o.get("scale") or 1.0,
        }))
    return outputs


def parse_xrandr(text):
    """xrandr --query (X11 데스크톱일 때) → 화면 목록"""
    outputs = []
    for line in text.splitlines():
        m = re.match(r"^(\S+) connected (?:primary )?(?:(\d+)x(\d+)\+(-?\d+)\+(-?\d+) )?(normal|left|inverted|right)?", line)
        if not m:
            continue
        if m[2]:
            rotate = {"normal": "0", "left": "90", "inverted": "180", "right": "270"}[m[6] or "normal"]
            outputs.append({"name": m[1], "label": "", "enabled": True, "x": int(m[4]), "y": int(m[5]),
                            "w": int(m[2]), "h": int(m[3]), "rotate": rotate})
        else:
            outputs.append({"name": m[1], "label": "", "enabled": False, "x": 0, "y": 0, "w": 0, "h": 0, "rotate": "0"})
    return outputs


def pick_target(displays, want):
    """작품을 띄울 화면: 'auto' = 켜져 있는 화면 중 제일 작은 것(2인치), 'off' = 안 옮김, 아니면 그 이름."""
    if want == "off" or not displays:
        return None
    if want and want != "auto":
        return next((d for d in displays if d["name"] == want), None)
    enabled = [d for d in displays if d["enabled"] and d["w"] * d["h"] > 0]
    return min(enabled, key=lambda d: (d["w"] * d["h"], d["name"])) if enabled else None


def screen_config(raw):
    cfg = dict(SCREEN_DEFAULT)
    cfg.update({k: v for k, v in (raw or {}).items() if k in SCREEN_DEFAULT and v is not None})
    cfg["target"] = str(cfg["target"] or "auto")
    cfg["rotate"] = str(cfg["rotate"])
    cfg["only"] = bool(cfg["only"])
    return cfg


_x_display_cache = [-1e9, ":0"]


def x_display():
    """Xwayland(또는 X11)의 DISPLAY 번호. 보통 :0"""
    now = time.monotonic()
    if now - _x_display_cache[0] < 30:
        return _x_display_cache[1]
    found = None
    for cmdline in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            args = cmdline.read_bytes().split(b"\0")
        except OSError:
            continue
        if args and args[0].endswith(b"Xwayland"):
            found = next((a.decode() for a in args[1:] if re.fullmatch(rb":\d+", a)), None)
            if found:
                break
    if not found:
        sockets = sorted(Path("/tmp/.X11-unix").glob("X*"))
        found = f":{sockets[0].name[1:]}" if len(sockets) == 1 else ":0"
    _x_display_cache[:] = [now, found]
    return found


def load(path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return default


def save(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    tmp.chmod(0o600)
    tmp.replace(path)


def board_serial():
    """라즈베리 파이 보드 고유 번호. SD카드를 복제해도 보드마다 다름."""
    candidates = []
    for path in ("/sys/firmware/devicetree/base/serial-number", "/proc/device-tree/serial-number"):
        try:
            candidates.append(Path(path).read_text(errors="ignore"))
        except OSError:
            pass
    try:
        for line in Path("/proc/cpuinfo").read_text(errors="ignore").splitlines():
            if line.lower().startswith("serial"):
                candidates.append(line.partition(":")[2])
    except OSError:
        pass
    for value in candidates:
        value = re.sub(r"[^0-9A-Za-z]", "", value)
        if value.strip("0"):
            return value.lower()
    return None


def device_id(config):
    if config.get("id"):
        return config["id"]
    serial = board_serial()
    if serial:
        return serial[-10:]
    try:
        return Path("/etc/machine-id").read_text().strip()[:10]
    except OSError:
        return re.sub(r"[^A-Za-z0-9_-]", "-", socket.gethostname())[:40] or "device"


def beacon_sign(token, host, port):
    """중앙이 뿌리는 UDP 신호의 서명. 토큰을 모르면 못 만듦 → 가짜 중앙으로 끌려가지 않음."""
    return hmac.new(token.encode(), f"{host}:{port}".encode(), hashlib.sha256).hexdigest()[:24]


def inside_project(rel):
    path = (PROJECT / rel).resolve()
    if PROJECT.resolve() not in path.parents:
        raise ValueError(f"프로젝트 폴더 밖 경로는 쓸 수 없음: {rel}")
    return path


def read_load():
    try:
        return round(os.getloadavg()[0], 2)
    except OSError:
        return None


def read_temp():
    try:
        return round(int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000, 1)
    except (OSError, ValueError):
        return None


def rotate(path, limit=5_000_000):
    try:
        if path.stat().st_size > limit:
            path.replace(path.with_name(path.name + ".1"))
    except OSError:
        pass


def write_tone(path):
    """스피커 테스트용 '띵-동' 소리."""
    rate = 44100
    frames = array.array("h")
    for freq, seconds in ((880, 0.22), (660, 0.4)):
        count = int(rate * seconds)
        for i in range(count):
            fade = min(1.0, i / 500, (count - i) / 500)
            frames.append(int(12000 * fade * math.sin(2 * math.pi * freq * i / rate)))
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(frames.tobytes())


def install_prepared(root, staging):
    """준비된 작품 폴더(root, staging 안)를 project/로 교체."""
    for script in list(root.rglob("*.sh")) + list(root.rglob("*.command")):
        script.chmod(0o755)
    if (root / "live-bridge.js").exists() and BRIDGE_JS.exists():
        shutil.copy(BRIDGE_JS, root / "live-bridge.js")
    previous = HOME / "project.previous"
    shutil.rmtree(previous, ignore_errors=True)
    if PROJECT.exists():
        PROJECT.rename(previous)
    root.rename(PROJECT)
    shutil.rmtree(staging, ignore_errors=True)
    SETUP_MARK.unlink(missing_ok=True)


def unpack_bundle(bundle, staging):
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    extract_zip(bundle, staging)
    bundle.unlink()
    install_prepared(staging, staging)


# ----- 중앙 없이 이 Pi에서 바로 올릴 때 (server.py와 같은 규칙) -----


def unwrap(folder):
    top = ""
    while True:
        entries = [p for p in folder.iterdir() if p.name not in JUNK_FILES]
        if len(entries) == 1 and entries[0].is_dir() and entries[0].name not in SKIP_DIRS:
            folder = entries[0]
            top = top or folder.name
        else:
            return folder, top


def prepare_local(src, staging):
    """이 Pi에 있는 폴더/zip/html → staging 안에 작품으로 준비. → (작품 폴더, 이름)"""
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    if src.is_dir():
        name = src.name
        for path in sorted(src.rglob("*")):
            parts = safe_parts(path.relative_to(src).as_posix())
            if parts and path.is_file():
                target = staging.joinpath(*parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
    elif zipfile.is_zipfile(src):
        name = src.stem
        extract_zip(src, staging)
    elif src.suffix.lower() in (".html", ".htm"):
        name = src.stem
        shutil.copy2(src, staging / src.name)
    else:
        raise RuntimeError("작품 폴더, zip, 또는 html 파일을 주세요")
    root, top = unwrap(staging)
    if not any(root.iterdir()):
        raise RuntimeError("빈 폴더/zip 임")
    if top and (src.is_dir() or name.lower().startswith("archive")):
        name = top
    return root, (re.sub(r"[^\w-]+", "-", name).strip("-_")[:40] or "project")



class Beacon(asyncio.DatagramProtocol):
    """중앙이 3초마다 뿌리는 '나 여기 있음' 신호를 듣고 중앙의 지금 주소를 기억함."""

    def __init__(self, agent):
        self.agent = agent

    def datagram_received(self, data, addr):
        try:
            msg = json.loads(data)
            port = int(msg["port"])
            sig = str(msg["sig"])
        except (ValueError, KeyError, TypeError):
            return
        if msg.get("live_control") != 1:
            return
        self.agent.found_any = f"http://{addr[0]}:{port}"  # 연결 요청(토큰 없을 때)용
        expected = beacon_sign(self.agent.config.get("token", ""), addr[0], port)
        if hmac.compare_digest(sig, expected):
            self.agent.found = f"http://{addr[0]}:{port}"


class Agent:
    def __init__(self):
        self.config = load(CONFIG, {})
        self.id = device_id(self.config)
        self.bridge_port = int(self.config.get("bridge_port") or 8765)
        self.state = load(STATE, {})
        self.state.setdefault("env", {})
        self.state.setdefault("local_env", {})
        self.state.setdefault("role", "agent")
        self.state.setdefault("name", socket.gethostname())
        self.state.setdefault("prompt", None)
        self.state.setdefault("manifest", None)
        self.state.setdefault("project", None)
        self.state.setdefault("running", False)
        self.state.setdefault("screen", None)
        self.phase = "stopped" if self.state["manifest"] else "empty"
        self.want_running = False
        self.procs = set()
        self.groups = set()
        self.keepers = []
        self.bridges = set()
        self.voice = "sleep"
        self.last_voice = {"type": "wake" if self.state["role"] == "master" else "sleep"}
        self.last_mode = {"type": "mode", "mode": "idle"}
        self.logs = deque(maxlen=300)
        self.outbox = asyncio.Queue(maxsize=1000)
        self.project_lock = asyncio.Lock()
        self.started_at = time.time()
        self.config.setdefault("controller", DEFAULT_CONTROLLER)
        self.base = self.config["controller"].rstrip("/")
        self.found = None
        self.found_any = None
        self.central = "시작 중"
        self.ip_cache = (-1e9, None)
        self.window_flags = []        # 작품 Chromium에 붙일 창 위치 옵션 (2인치 화면)
        self.displays = None          # 마지막으로 읽은 화면 목록
        self.display_error = None
        self.screen = None            # 작품을 띄우는(띄울) 화면 이름
        self.turned_off = set()       # '모니터 끄기'로 꺼 둔 화면
        self.hw_audio = None

    # ---------- 중앙으로 보내기 ----------

    def send(self, msg):
        try:
            self.outbox.put_nowait(msg)
        except asyncio.QueueFull:
            pass

    def log(self, line):
        line = f"{time.strftime('%H:%M:%S')} {line.rstrip()}"
        print(line, flush=True)
        self.logs.append(line)
        self.send({"type": "log", "line": line})

    def status(self):
        return {
            "type": "status",
            "state": self.phase,
            "project": self.state["project"],
            "voice": self.voice,
            "bridge": len(self.bridges),
            "temp": read_temp(),
            "load": read_load(),
            "uptime": int(time.time() - self.started_at),
            "ip": self.local_ip(),
            "hostname": socket.gethostname(),
            "version": VERSION,
            "screens": self.displays,
            "screen": self.screen,
            "screen_error": self.display_error,
            "audio": self.hw_audio,
        }

    def local_ip(self):
        """이 Pi의 LAN IP. DNS를 안 거쳐서(.local 이름 등) 이벤트 루프를 멈추지 않음."""
        now = time.monotonic()
        if now - self.ip_cache[0] < 30:
            return self.ip_cache[1]
        host = urlparse(self.base).hostname or ""
        target = host if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", host) else "8.8.8.8"
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect((target, 9))
                ip = s.getsockname()[0]
        except OSError:
            ip = None
        self.ip_cache = (now, ip)
        return ip

    def set_phase(self, phase):
        self.phase = phase
        self.send(self.status())

    # ---------- 프로젝트 실행 ----------

    def env(self):
        env = dict(os.environ)
        runtime = env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        wayland = sorted(p.name for p in Path(runtime).glob("wayland-*") if not p.name.endswith(".lock"))
        if wayland and not env.get("WAYLAND_DISPLAY"):
            env["WAYLAND_DISPLAY"] = wayland[0]
        if not env.get("DISPLAY"):
            env["DISPLAY"] = x_display()
        env.update({k: str(v) for k, v in self.effective_env().items()})
        if env.get("GEMINI_API_KEY"):
            env["GEMINI_KEY"] = env["GEMINI_API_KEY"]  # 학생 실행 스크립트들은 GEMINI_KEY라는 이름으로 읽음
        env["PATH"] = f"{BIN}:{env.get('PATH') or '/usr/local/bin:/usr/bin:/bin'}"  # chromium → 전시용 옵션 래퍼
        if self.window_flags:
            env["LIVE_CHROMIUM_FLAGS"] = " ".join(self.window_flags)  # 래퍼가 이걸 붙여서 2인치 화면에 띄움
        else:
            env.pop("LIVE_CHROMIUM_FLAGS", None)
        env.update(self.template_values())
        return env

    def effective_env(self):
        """이 Pi에서 넣은 값(live key) 위에 중앙에서 온 값을 덮음. 중앙 값이 비어 있으면 Pi 값 유지."""
        env = dict(self.state.get("local_env") or {})
        env.update({k: v for k, v in (self.state.get("env") or {}).items() if v})
        return env

    def template_values(self):
        return {
            "PROJECT_DIR": str(PROJECT),
            "DEVICE_NAME": self.state["name"],
            "DEVICE_ROLE": self.state["role"],
            "LIVE_BRIDGE": f"ws://127.0.0.1:{self.bridge_port}/bridge",
        }

    def render_files(self):
        manifest = self.state["manifest"] or {}
        env = self.effective_env()
        values = {"GEMINI_API_KEY": "", **env, **self.template_values()}
        for rel, template in (manifest.get("files") or {}).items():
            path = inside_project(rel)
            if "GEMINI_API_KEY" in template and not env.get("GEMINI_API_KEY") and path.exists():
                continue  # 키가 아직 없으면 작품에 원래 들어 있던 키 파일을 덮어쓰지 않음
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(string.Template(template).safe_substitute(values))
            path.chmod(0o600)
        if manifest.get("student"):
            config = {"key": env.get("GEMINI_API_KEY", ""), "prompt": self.state["prompt"],
                      "bridge": f"ws://127.0.0.1:{self.bridge_port}/bridge"}
            inside_project("octo-config.js").write_text("window.OCTO_CONFIG = " + json.dumps(config) + ";\n")
            inside_project("octo-config.js").chmod(0o600)
        if manifest.get("prompt_file"):
            path = inside_project(manifest["prompt_file"])
            original = inside_project(".octo-original-prompt")
            if not original.exists() and path.exists():
                shutil.copy(path, original)
            if self.state["prompt"] is not None:
                path.write_text(self.state["prompt"])
            elif original.exists():
                shutil.copy(original, path)

    async def run_setup(self):
        manifest = self.state["manifest"]
        command = (manifest.get("setup") or "").strip()
        mark = json.dumps([self.state["project"], command])
        if not command or (SETUP_MARK.exists() and SETUP_MARK.read_text() == mark):
            return
        self.set_phase("installing")
        self.log(f"설치 시작: {command}")
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", command, cwd=PROJECT, env=self.env(), limit=1 << 20,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        await self.pump(proc, "setup")
        if await proc.wait() != 0:
            raise RuntimeError("설치(setup) 명령이 실패함. 로그를 확인하세요.")
        SETUP_MARK.write_text(mark)
        self.log("설치 완료")

    async def pump(self, proc, label):
        skipping = False
        while True:
            try:
                raw = await proc.stdout.readline()
            except ValueError:
                # 줄바꿈 없는 아주 긴 출력: 버리고 계속 읽음 (안 읽으면 프로그램이 멈춤)
                if not skipping:
                    self.log(f"[{label}] (너무 긴 출력 생략)")
                skipping = True
                continue
            if not raw:
                return
            skipping = False
            self.log(f"[{label}] {raw.decode(errors='replace')}")

    async def spawn_command(self, command, label):
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", command, cwd=PROJECT, env=self.env(), limit=1 << 20,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        asyncio.create_task(self.pump(proc, label))
        return proc

    async def spawn_browser(self, browser):
        exe = shutil.which("chromium-browser") or shutil.which("chromium")
        if not exe:
            raise RuntimeError("chromium이 설치되어 있지 않음")
        await self.wait_for_display()
        url = string.Template(browser["url"]).safe_substitute(self.template_values())
        if url.startswith("http"):
            await self.wait_for_http(url)
        args = [exe, *CHROMIUM_FLAGS, *self.window_flags, f"--user-data-dir={HOME / 'chromium'}",
                *(browser.get("args") or []), url]
        rotate(CHROMIUM_LOG)
        with open(CHROMIUM_LOG, "ab") as chromium_log:
            return await asyncio.create_subprocess_exec(
                *args, cwd=PROJECT, env=self.env(), stdout=chromium_log, stderr=chromium_log,
                start_new_session=True,
            )

    async def wait_for_display(self):
        """데스크톱이 뜰 때까지 기다림. 기다렸으면(부팅 직후) True"""
        runtime = Path(self.env()["XDG_RUNTIME_DIR"])
        for i in range(90):
            if list(runtime.glob("wayland-*")) or list(Path("/tmp/.X11-unix").glob("X*")):
                return i > 0
            await asyncio.sleep(1)
        self.log("화면(데스크톱)을 못 찾음. 데스크톱 자동 로그인이 켜져 있는지 확인하세요.")
        return True

    async def wait_for_http(self, url):
        async with ClientSession(timeout=ClientTimeout(total=2)) as http:
            for _ in range(30):
                try:
                    async with http.get(url):
                        return
                except Exception:
                    await asyncio.sleep(1)

    async def keep_alive(self, label, factory):
        while self.want_running:
            started = time.monotonic()
            try:
                proc = await factory()
            except Exception as error:
                self.log(f"{label} 실행 실패: {error}")
                await asyncio.sleep(5)
                continue
            self.procs.add(proc)
            self.groups.add(proc.pid)
            code = await proc.wait()
            self.procs.discard(proc)
            if code == 0 and self.want_running and group_alive(proc.pid):
                # start.sh처럼 서버·브라우저만 띄워 놓고 스크립트는 끝나는 방식 → 띄운 것들이 살아 있는 동안 그대로 둠
                self.log(f"{label}: 실행 스크립트는 끝났고 띄운 프로그램(서버·브라우저)은 계속 도는 중")
                while self.want_running and group_alive(proc.pid):
                    await asyncio.sleep(2)
            if not self.want_running:
                return  # 정리는 stop()이 함
            await kill_group(proc.pid, grace=3)  # 남은 것 정리 (브라우저가 두 개 뜨는 것 방지)
            self.groups.discard(proc.pid)
            self.log(f"{label} 꺼짐 (code {code}) → 자동 재시작")
            await asyncio.sleep(2 if time.monotonic() - started > 30 else 5)

    async def start(self):
        manifest = self.state["manifest"]
        if not manifest or not PROJECT.exists():
            raise RuntimeError("설치된 프로젝트가 없음. 먼저 Deploy 하세요.")
        if self.want_running:
            return
        validate_manifest(manifest)
        self.render_files()
        await self.run_setup()
        if manifest.get("run") or manifest.get("browser"):
            self.set_phase("starting")
            if await self.wait_for_display():  # 부팅 직후엔 데스크톱이 뜬 다음에 실행 (화면 쓰는 run 스크립트용)
                await asyncio.sleep(5)  # 화면 배치(모니터 설정)가 끝날 때까지 조금 더
            await self.prepare_screen()
        self.want_running = True
        self.state["running"] = True
        save(STATE, self.state)
        for i, command in enumerate(manifest.get("run") or []):
            self.keepers.append(asyncio.create_task(
                self.keep_alive(f"run{i + 1}", lambda c=command, i=i: self.spawn_command(c, f"run{i + 1}"))
            ))
        if manifest.get("browser"):
            self.keepers.append(asyncio.create_task(
                self.keep_alive("browser", lambda: self.spawn_browser(manifest["browser"]))
            ))
        self.set_phase("running")
        self.log(f"실행: {manifest.get('name') or self.state['project']}")

    async def stop(self, remember=True, release_screen=False):
        self.want_running = False
        if remember:
            self.state["running"] = False
            save(STATE, self.state)
        groups = set(self.groups) | {proc.pid for proc in self.procs}
        for pgid in groups:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pgid, signal.SIGTERM)
        for proc in list(self.procs):
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), 5)
        for pgid in groups:
            await kill_group(pgid)
        for task in self.keepers:
            task.cancel()
        self.keepers = []
        self.procs.clear()
        self.groups.clear()
        self.voice = "sleep"
        if release_screen:
            await self.restore_screens()  # 작품을 끄면 '모니터 끄기'로 꺼 둔 모니터를 다시 켬
        self.set_phase("stopped" if self.state["manifest"] else "empty")

    async def deploy(self, msg):
        validate_manifest(msg["manifest"])
        self.set_phase("installing")
        bundle = HOME / "bundle.zip"
        self.log(f"다운로드: {msg['project']}")
        async with ClientSession(timeout=ClientTimeout(total=900, sock_read=120)) as http:
            async with http.get(self.base + msg["path"], headers={"X-Token": self.config["token"]}) as response:
                if response.status != 200:
                    raise RuntimeError(f"다운로드 실패 ({response.status}) {(await response.text())[:200]}")
                with open(bundle, "wb") as out:
                    async for chunk in response.content.iter_chunked(1 << 16):
                        out.write(chunk)
        staging = HOME / "project.new"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir()
        await asyncio.to_thread(extract_zip, bundle, staging)
        bundle.unlink()
        old_state = copy.deepcopy(self.state)
        old_mark = SETUP_MARK.read_text() if SETUP_MARK.exists() else None
        await self.stop()
        await asyncio.to_thread(install_prepared, staging, staging)
        self.state.update(project=msg["project"], manifest=msg["manifest"], source_prompt="")
        save(STATE, self.state)
        try:
            self.log("파일 설치 완료")
            await self.start()
        except Exception:
            await self.stop()
            previous = HOME / "project.previous"
            if previous.exists():
                shutil.rmtree(PROJECT)
                previous.rename(PROJECT)
                self.state = old_state
                save(STATE, self.state)
                if old_mark is not None:
                    SETUP_MARK.write_text(old_mark)
                if old_state["running"]:
                    await self.start()
                self.log("새 작품 설치 실패 → 이전 작품 복구")
            raise

    async def local_run(self, path):
        """중앙 없이 이 Pi에 있는 폴더/zip/html을 바로 설치·실행 ('live run')."""
        src = Path(path).expanduser()
        if not src.exists():
            raise RuntimeError(f"그런 파일/폴더가 없음: {src}")
        async with self.project_lock:
            await self.stop()
            self.set_phase("installing")
            staging = HOME / "project.new"
            root, name = await asyncio.to_thread(prepare_local, src, staging)
            try:
                manifest = guess_manifest(root)
            except RuntimeError:
                shutil.rmtree(staging, ignore_errors=True)
                self.set_phase("error")
                raise
            if manifest.get("name") in (None, "", root.name):
                manifest["name"] = name
            files = manifest.get("files") or {}
            own_key = any("GEMINI_API_KEY" in t and (root / rel).exists() for rel, t in files.items())
            await asyncio.to_thread(install_prepared, root, staging)
            self.state.update(project=f"local:{name}", manifest=manifest)
            save(STATE, self.state)
            self.log(f"이 Pi에서 직접 올림: {src}")
            await self.start()
        how = "run: " + " / ".join(manifest.get("run") or []) if manifest.get("run") else ""
        if manifest.get("browser"):
            how += (" · " if how else "") + f"화면: {manifest['browser'].get('url')}"
        lines = [f"실행 중: {name}  ({how or '실행할 게 없음 — live.json 확인'})"]
        needs_key = any("GEMINI_API_KEY" in t for t in files.values())
        if needs_key and not own_key and not self.effective_env().get("GEMINI_API_KEY"):
            lines.append("이 작품은 API 키가 필요함 → live key 로 넣으면 바로 다시 켜짐")
        return "\n".join(lines)

    async def set_local_key(self, key):
        key = (key or "").strip()
        if key and not re.fullmatch(r"[A-Za-z0-9_-]{1,512}", key):
            raise ValueError("API 키 형식이 잘못됨")
        if key:
            self.state["local_env"]["GEMINI_API_KEY"] = key
        else:
            self.state["local_env"].pop("GEMINI_API_KEY", None)
        save(STATE, self.state)
        if not (PROJECT.exists() and self.state["manifest"]):
            return "키 저장함 (작품을 올리면 적용됨)"
        async with self.project_lock:
            running = self.want_running
            await self.stop(remember=False)
            self.render_files()
            if running or self.state["running"]:
                await self.start()
        central_key = (self.state.get("env") or {}).get("GEMINI_API_KEY")
        note = " (단, 중앙 대시보드에 키가 있으면 그게 우선)" if central_key else ""
        return ("키 저장 · 작품 다시 켬" if key else "이 Pi에 넣은 키 지움") + note

    # ---------- 화면: 작품을 2인치 디스플레이에 ----------

    def wayland(self):
        return bool(self.env().get("WAYLAND_DISPLAY"))

    async def list_displays(self):
        if self.wayland():
            try:
                return parse_wlr_randr_json(json.loads(await self.tool("wlr-randr", "--json")))
            except ValueError:
                pass
            except RuntimeError as error:
                if "없음" in str(error) or "onnect" in str(error):
                    raise
            return parse_wlr_randr(await self.tool("wlr-randr"))
        return parse_xrandr(await self.tool("xrandr", "--query"))

    async def screen_cmd(self, name, rotate=None, pos=None, on=None):
        if self.wayland():
            args = ["wlr-randr", "--output", name]
            if on is not None:
                args.append("--on" if on else "--off")
            if rotate is not None:
                args += ["--transform", ROTATIONS[rotate][0]]
            if pos is not None:
                args += ["--pos", f"{pos[0]},{pos[1]}"]
        else:
            args = ["xrandr", "--output", name]
            if on is not None:
                args.append("--auto" if on else "--off")
            if rotate is not None:
                args += ["--rotate", ROTATIONS[rotate][1]]
            if pos is not None:
                args += ["--pos", f"{pos[0]}x{pos[1]}"]
        await self.tool(*args)

    async def restore_screens(self):
        names = sorted(self.turned_off)
        for name in names:
            with contextlib.suppress(RuntimeError):
                await self.screen_cmd(name, on=True)
        self.turned_off.clear()
        if names:
            self.log("모니터 다시 켬: " + ", ".join(names))

    async def prepare_screen(self):
        """작품 켜기 직전: 2인치 화면을 골라 (방향·모니터 끄기 적용) Chromium 창 위치 옵션을 만듦."""
        self.window_flags = []
        cfg = screen_config(self.state.get("screen"))
        try:
            displays = await self.list_displays()
            target = pick_target(displays, cfg["target"])
            if target is None and cfg["target"] not in ("auto", "off"):
                self.log(f"설정한 화면 {cfg['target']}이(가) 없음 → 제일 작은 화면으로")
                target = pick_target(displays, "auto")
            if target is None:
                self.displays, self.screen = displays, None
                return
            changed = False
            if not target["enabled"]:
                await self.screen_cmd(target["name"], on=True)
                changed = True
            if cfg["rotate"] in ROTATIONS and target["rotate"] != cfg["rotate"]:
                await self.screen_cmd(target["name"], rotate=cfg["rotate"])
                self.log(f"{target['name']} 방향 → {cfg['rotate']}°")
                changed = True
            others = [d for d in displays if d["enabled"] and d["name"] != target["name"]]
            if cfg["only"] and others:
                for d in others:
                    await self.screen_cmd(d["name"], on=False)
                    self.turned_off.add(d["name"])
                self.log("모니터 끔 (2인치 화면만 사용): " + ", ".join(d["name"] for d in others))
                changed = True
            elif not cfg["only"] and self.turned_off:
                await self.restore_screens()
                changed = True
            if changed:
                await asyncio.sleep(0.5)
                displays = await self.list_displays()
                target = next((d for d in displays if d["name"] == target["name"]), target)
            others = [d for d in displays if d["enabled"] and d["name"] != target["name"]]
            if others and any(d["x"] + d["w"] > target["x"] + target["w"] for d in others):
                # 작은 화면을 맨 오른쪽으로: 창이 잠깐 크게 떠도 모니터 쪽으로 넘어가지 않게
                right = max(d["x"] + d["w"] for d in others)
                await self.screen_cmd(target["name"], pos=(right, 0))
                await asyncio.sleep(0.3)
                displays = await self.list_displays()
                target = next((d for d in displays if d["name"] == target["name"]), target)
                others = [d for d in displays if d["enabled"] and d["name"] != target["name"]]
        except RuntimeError as error:
            self.display_error = str(error)[:300]
            self.log(f"화면 설정을 못 함 → 기본 화면에 띄움 ({error})")
            return
        self.displays, self.display_error, self.screen = displays, None, target["name"]
        if others:
            flags = [f"--window-position={target['x']},{target['y']}",
                     f"--window-size={target['w']},{target['h']}", "--force-device-scale-factor=1"]
            if self.wayland():
                if shutil.which("Xwayland") or Path("/usr/bin/Xwayland").exists():
                    flags.append("--ozone-platform=x11")  # Wayland에선 창 위치를 못 정해서 X11(Xwayland)로 띄움
                else:
                    self.log("Xwayland가 없어서 창 위치를 못 정함 → 대시보드에서 '모니터 끄기'를 켜세요")
                    flags = []
            self.window_flags = flags
        self.log(f"작품 화면: {target['name']} {target['w']}×{target['h']}"
                 + (f" (다른 화면: {', '.join(d['name'] for d in others)})" if others else ""))

    async def restart_for_screen(self):
        async with self.project_lock:
            if not self.want_running:
                return
            self.log("화면 설정이 바뀜 → 작품 다시 켬")
            await self.stop(release_screen=not screen_config(self.state.get("screen"))["only"])
            await self.start()

    async def refresh_hw(self):
        """대시보드에 보여줄 화면·스피커·마이크 정보"""
        env = self.env()
        if not (list(Path(env["XDG_RUNTIME_DIR"]).glob("wayland-*")) or list(Path("/tmp/.X11-unix").glob("X*"))):
            return  # 아직 데스크톱이 안 뜸
        try:
            self.displays = await self.list_displays()
            self.display_error = None
            if not self.want_running:
                picked = pick_target(self.displays, screen_config(self.state.get("screen"))["target"])
                self.screen = picked["name"] if picked else None
        except RuntimeError as error:
            self.display_error = str(error)[:300]
        try:
            audio = await self.audio_state()
            speaker = next((d["label"] for d in audio["sinks"] if d["name"] == audio["sink"]), audio["sink"] or None)
            mic = next((d["label"] for d in audio["sources"] if d["name"] == audio["source"]), None)
            if mic is None and audio["sources"]:
                mic = audio["sources"][0]["label"]
            self.hw_audio = {"speaker": speaker, "mic": mic}
        except RuntimeError as error:
            self.hw_audio = {"error": str(error)[:300]}
        self.send(self.status())

    async def hw_loop(self):
        while True:
            with contextlib.suppress(Exception):
                await self.refresh_hw()
            await asyncio.sleep(30)

    # ---------- 오디오: 스피커/마이크 고르기, 볼륨, 테스트 ----------

    async def tool(self, *args, timeout=10):
        env = dict(self.env(), LC_ALL="C")
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            raise RuntimeError(f"{args[0]} 없음 → 이 Pi에서 설치 명령(install.sh)을 다시 실행하세요")
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            raise RuntimeError(f"{args[0]} 응답 없음")
        if proc.returncode != 0:
            message = (err or out).decode(errors="replace").strip()
            if "onnection" in message:
                message += " (소리 장치는 데스크톱에 로그인된 상태에서만 쓸 수 있음)"
            raise RuntimeError(f"{args[0]} {args[1] if len(args) > 1 else ''}: {message}")
        return out.decode(errors="replace")

    async def audio_devices(self, kind):
        try:
            items = json.loads(await self.tool("pactl", "-f", "json", "list", kind))
            devices = [{"name": d["name"], "label": d.get("description") or d["name"]} for d in items]
        except (ValueError, KeyError, TypeError):
            devices = None
        except RuntimeError as error:
            if "onnection" in str(error) or "없음" in str(error):
                raise
            devices = None
        if devices is None:  # 옛날 pactl: JSON 출력 없음
            text = await self.tool("pactl", "list", "short", kind)
            names = [line.split("\t")[1] for line in text.splitlines() if line.count("\t") >= 1]
            devices = [{"name": name, "label": name} for name in names]
        if kind == "sources":
            devices = [d for d in devices if not d["name"].endswith(".monitor")]
        return devices

    async def audio_volume(self, kind):
        try:
            text = await self.tool("pactl", f"get-{kind}-volume", f"@DEFAULT_{kind.upper()}@")
        except RuntimeError:
            return None
        match = re.search(r"(\d+)%", text)
        return int(match.group(1)) if match else None

    async def audio_state(self):
        info = {}
        for line in (await self.tool("pactl", "info")).splitlines():
            key, _, value = line.partition(":")
            info[key.strip()] = value.strip()
        return {
            "sinks": await self.audio_devices("sinks"),
            "sources": await self.audio_devices("sources"),
            "sink": info.get("Default Sink", ""),
            "source": info.get("Default Source", ""),
            "sink_volume": await self.audio_volume("sink"),
            "source_volume": await self.audio_volume("source"),
        }

    async def audio_set(self, msg):
        state = await self.audio_state()
        changed = []
        for kind in ("sink", "source"):
            name = msg.get(kind)
            if name and name != state[kind]:
                if name not in {d["name"] for d in state[kind + "s"]}:
                    raise RuntimeError(f"없는 장치: {name}")
                await self.tool("pactl", f"set-default-{kind}", name)
                changed.append(f"{'스피커' if kind == 'sink' else '마이크'}={name}")
            volume = msg.get(f"{kind}_volume")
            if volume is not None:
                volume = max(0, min(150, int(volume)))
                target = f"@DEFAULT_{kind.upper()}@"
                await self.tool("pactl", f"set-{kind}-volume", target, f"{volume}%")
                await self.tool("pactl", f"set-{kind}-mute", target, "0")
                changed.append(f"{'스피커' if kind == 'sink' else '마이크'} 볼륨 {volume}%")
        if changed:
            self.log("오디오 설정: " + ", ".join(changed))
        return await self.audio_state()

    async def play_tone(self):
        if not TEST_TONE.exists():
            write_tone(TEST_TONE)
        await self.tool("paplay", str(TEST_TONE), timeout=15)

    async def mic_level(self, seconds=2):
        env = dict(self.env(), LC_ALL="C")
        try:
            proc = await asyncio.create_subprocess_exec(
                "parecord", "--raw", "--format=s16le", "--rate=16000", "--channels=1",
                env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            raise RuntimeError("parecord 없음 → 이 Pi에서 설치 명령(install.sh)을 다시 실행하세요")
        want = 16000 * 2 * seconds
        try:
            data = await asyncio.wait_for(proc.stdout.readexactly(want), seconds + 4)
        except asyncio.IncompleteReadError as error:
            data = error.partial
        except asyncio.TimeoutError:
            data = b""
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        err = (await proc.stderr.read()).decode(errors="replace").strip()
        await proc.wait()
        if len(data) < 3200:
            raise RuntimeError(f"마이크에서 소리를 못 받음 {err}".strip())
        samples = array.array("h")
        samples.frombytes(data[: len(data) // 2 * 2])
        peak = max(abs(s) for s in samples) / 32768
        rms = math.sqrt(sum(s * s for s in samples) / len(samples)) / 32768
        return {"peak": round(peak * 100), "rms": round(rms * 100, 1), "seconds": round(len(samples) / 16000, 1)}

    async def audio(self, msg):
        op = msg.get("op", "get")
        if op == "get":
            return await self.audio_state()
        if op == "set":
            return await self.audio_set(msg)
        if op == "test" and msg.get("what") == "speaker":
            await self.play_tone()
            return {"played": True}
        if op == "test" and msg.get("what") == "mic":
            return await self.mic_level()
        raise ValueError(f"모르는 오디오 명령: {op}")

    async def identify(self):
        """대시보드 Identify: 페이지가 있으면 화면에 이름+삑, 없으면 스피커로만 띵동."""
        await self.to_bridges({"type": "identify", "name": self.state["name"]})
        if self.bridges:
            return {"screen": True}
        try:
            await self.play_tone()
        except RuntimeError as error:
            raise RuntimeError(f"화면에 띄울 페이지(bridge)가 없고 소리도 못 냄: {error}")
        return {"screen": False, "sound": True}

    # ---------- 중앙에서 온 명령 ----------

    async def handle(self, msg):
        kind = msg.get("type")
        data = None
        try:
            if kind == "config":
                changed = any(self.state.get(k) != msg.get(k) for k in ("env", "prompt", "role"))
                old_screen = screen_config(self.state.get("screen"))
                for key in ("name", "role", "env", "prompt"):
                    self.state[key] = msg.get(key, self.state[key])
                if "screen" in msg:
                    self.state["screen"] = msg["screen"]
                save(STATE, self.state)
                if changed and PROJECT.exists() and self.state["manifest"]:
                    self.render_files()
                await self.to_bridges({"type": "hello", **self.bridge_hello()})
                if screen_config(self.state.get("screen")) != old_screen:
                    if self.want_running:
                        asyncio.create_task(self.restart_for_screen())
                    elif not screen_config(self.state.get("screen"))["only"]:
                        await self.restore_screens()
                return
            if kind == "voice":
                await self.to_bridges({**msg["msg"], "req": msg.get("req")})
                return
            if kind == "ping":
                pass
            elif kind == "deploy":
                async with self.project_lock:
                    await self.deploy(msg)
            elif kind == "start":
                async with self.project_lock:
                    await self.start()
            elif kind == "stop":
                async with self.project_lock:
                    await self.stop(release_screen=True)
            elif kind == "restart":
                async with self.project_lock:
                    await self.stop()
                    await self.start()
            elif kind == "get_prompt":
                data = self.read_prompt()
            elif kind == "get_logs":
                data = list(self.logs)
            elif kind == "identify":
                data = await self.identify()
            elif kind == "audio":
                data = await self.audio(msg)
            elif kind == "update_agent":
                await self.update_self()
            elif kind in ("reboot", "poweroff"):
                self.log(f"{kind} 실행")
                subprocess.Popen(["sudo", "-n", "systemctl", kind])
            else:
                raise ValueError(f"모르는 명령: {kind}")
            self.send({"type": "result", "req": msg.get("req"), "command": kind, "ok": True, "data": data})
        except Exception as error:
            self.log(f"{kind} 실패: {error}")
            if kind in ("deploy", "start", "restart"):
                self.set_phase("error")
            self.send({"type": "result", "req": msg.get("req"), "command": kind, "ok": False, "message": str(error)})

    def read_prompt(self):
        if (self.state["manifest"] or {}).get("student") and not (self.state["manifest"] or {}).get("prompt_file"):
            return self.state["prompt"] if self.state["prompt"] is not None else self.state.get("source_prompt", "")
        manifest = self.state["manifest"] or {}
        if not manifest.get("prompt_file"):
            raise RuntimeError("manifest에 prompt_file이 없음")
        path = inside_project(manifest["prompt_file"])
        return path.read_text() if path.exists() else ""

    async def update_self(self):
        files = {}
        async with ClientSession(timeout=ClientTimeout(total=60)) as http:
            for name in ("agent.py", "live-bridge.js", "project.py"):
                async with http.get(f"{self.base}/agent/{name}", headers={"X-Token": self.config["token"]}) as response:
                    response.raise_for_status()
                    files[name] = await response.read()
        compile(files["project.py"], "project.py", "exec")
        compile(files["agent.py"], "agent.py", "exec")  # 받은 파일이 깨졌으면 여기서 멈춤 (기기가 먹통 되는 것 방지)
        for name, content in files.items():
            tmp = HOME / f"{name}.new"
            tmp.write_bytes(content)
            tmp.replace(HOME / name)
        self.log("agent 업데이트 완료 → 재시작")
        self.send({"type": "result", "command": "update_agent", "ok": True})
        await asyncio.sleep(1)
        os._exit(0)  # systemd(Restart=always)가 새 코드로 다시 켜줌

    # ---------- 중앙 연결 ----------

    async def central_loop(self):
        while True:
            if not self.config.get("token"):
                await self.pair_loop()
            await self.controller_loop()  # 토큰이 거절되면(중앙을 새로 설치함) 돌아옴 → 다시 연결 요청

    def pair_candidates(self):
        out = [self.config["controller"].rstrip("/")]
        if self.found_any and self.found_any not in out:
            out.append(self.found_any)
        return out

    async def pair_loop(self):
        """토큰 없이 설치된 Pi: 중앙에 '연결 요청' → 대시보드에서 허용하면 토큰을 받아 저장."""
        self.central = "중앙을 찾는 중 (같은 Wi-Fi의 중앙 서버가 켜져 있어야 함)"
        while not self.config.get("token"):
            denied = False
            for base in self.pair_candidates():
                query = f"id={self.id}&name={quote(socket.gethostname())}"
                url = f"{base.replace('http', 'ws', 1)}/ws/pair?{query}"
                try:
                    async with ClientSession(timeout=ClientTimeout(total=None, connect=10, sock_connect=5)) as http:
                        async with http.ws_connect(url, heartbeat=15) as ws:
                            self.central = f"연결 요청 보냄 → 중앙 대시보드에서 '허용'을 누르세요 ({base})"
                            self.log(self.central)
                            async for message in ws:
                                if message.type != WSMsgType.TEXT:
                                    continue
                                data = json.loads(message.data)
                                if data.get("type") == "paired" and data.get("token"):
                                    self.config.update(token=data["token"], controller=base)
                                    save(CONFIG, self.config)
                                    self.base = base
                                    self.log("중앙이 연결을 허용함")
                                    return
                                if data.get("type") == "pair_denied":
                                    denied = True
                                    break
                except WSServerHandshakeError as error:
                    if error.status == 404:  # 중앙이 옛 버전: 연결 요청 기능이 없음
                        self.central = ("중앙 서버가 옛 버전이라 연결 요청을 못 받음 → 중앙을 새 버전으로 업데이트하거나, "
                                        "대시보드의 '허용 없이 바로 붙이는 명령'을 이 Pi에서 실행")
                    print(f"pair request failed ({base}): {error!r}", flush=True)
                except Exception as error:
                    print(f"pair request failed ({base}): {error!r}", flush=True)
                if denied:
                    break
            if denied:
                self.central = "중앙에서 거절함 — 1분 뒤 다시 요청"
                self.log(self.central)
                await asyncio.sleep(float(self.config.get("pair_retry", 60)))
            else:
                if not self.central.startswith(("중앙을 찾는", "중앙 서버가 옛 버전")):
                    self.central = "중앙을 찾는 중 (같은 Wi-Fi의 중앙 서버가 켜져 있어야 함)"
                await asyncio.sleep(5)

    async def controller_loop(self):
        configured = self.config["controller"].rstrip("/")
        delay, failures, rejected = 1, 0, 0
        while True:
            # 설정된 주소가 안 되면, UDP 신호로 찾은 중앙 주소와 번갈아 시도
            use_found = failures % 2 == 1 and self.found and self.found != configured
            base = self.found if use_found else configured
            url = f"{base.replace('http', 'ws', 1)}/ws/device?id={self.id}&t={self.config['token']}"
            try:
                async with ClientSession(timeout=ClientTimeout(total=None, connect=10, sock_connect=5)) as http:
                    async with http.ws_connect(url, heartbeat=15) as ws:
                        delay, failures, rejected = 1, 0, 0
                        self.base = base
                        self.central = f"연결됨 ({base})"
                        self.log(f"중앙 연결됨: {base}")
                        await ws.send_json({**self.status(), "type": "hello", "id": self.id,
                                            "name": self.state["name"], "role": self.state["role"]})
                        writer = asyncio.create_task(self.drain(ws))
                        try:
                            async for message in ws:
                                if message.type == WSMsgType.TEXT:
                                    asyncio.create_task(self.handle(json.loads(message.data)))
                        finally:
                            writer.cancel()
                            await asyncio.gather(writer, return_exceptions=True)
                            await self.to_bridges({"type": "sleep"})
                            self.last_mode = {"type": "mode", "mode": "idle"}
            except WSServerHandshakeError as error:
                failures += 1
                if error.status == 401:
                    rejected += 1
                    if rejected >= 3:
                        self.log("중앙이 이 Pi의 토큰을 거절함 (중앙을 새로 설치했나 봄) → 다시 연결 요청")
                        self.config.pop("token", None)
                        save(CONFIG, self.config)
                        return
                print(f"controller connection failed ({base}): {error!r}", flush=True)
            except Exception as error:
                failures += 1
                print(f"controller connection failed ({base}): {error!r}", flush=True)
            self.central = f"중앙과 연결 안 됨 — 다시 시도 중 ({base})"
            await asyncio.sleep(delay)
            delay = min(delay * 2, 10)

    async def drain(self, ws):
        while True:
            msg = await self.outbox.get()
            await ws.send_json(msg)

    async def status_loop(self):
        while True:
            self.send(self.status())
            await asyncio.sleep(5)

    async def listen_beacon(self):
        loop = asyncio.get_running_loop()
        try:
            await loop.create_datagram_endpoint(
                lambda: Beacon(self), local_addr=("0.0.0.0", BEACON_PORT), reuse_port=True,
            )
        except (OSError, ValueError) as error:
            self.log(f"중앙 자동 찾기(UDP {BEACON_PORT})를 못 켬: {error}")

    # ---------- 페이지(bridge) 연결 ----------

    def bridge_hello(self):
        return {"id": self.id, "name": self.state["name"], "role": self.state["role"]}

    async def to_bridges(self, msg):
        if msg.get("type") in ("wake", "sleep"):
            self.last_voice = msg
        elif msg.get("type") == "kickoff":
            self.last_voice = {"type": "wake"}
        elif msg.get("type") == "mode":
            self.last_mode = msg
        for ws in list(self.bridges):
            try:
                await ws.send_json(msg)
            except ConnectionError:
                self.bridges.discard(ws)

    async def bridge_ws(self, request):
        origin = request.headers.get("Origin")
        if origin and not re.fullmatch(r"http://(?:localhost|127\.0\.0\.1):[0-9]+", origin):
            raise web.HTTPForbidden()
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=2 << 20)
        await ws.prepare(request)
        self.bridges.add(ws)
        self.log("페이지가 bridge에 연결됨")
        await ws.send_json({"type": "hello", **self.bridge_hello()})
        await ws.send_json(self.last_mode)
        await ws.send_json(self.last_voice)
        self.send(self.status())
        try:
            async for message in ws:
                if message.type != WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(message.data)
                except ValueError:
                    continue
                kind = data.get("type")
                if kind == "voice_state":
                    self.voice = data.get("state", "sleep")
                    self.send(self.status())
                elif kind == "log":
                    self.log(f"[page] {str(data.get('text', ''))[:500]}")
                elif kind == "voice_result":
                    self.send({"type": "result", "command": "voice", "req": data.get("req"),
                               "ok": bool(data.get("ok")), "message": str(data.get("message", ""))[:500]})
                elif kind == "project_prompt":
                    self.state["source_prompt"] = str(data.get("text", ""))[:20000]
                    save(STATE, self.state)
                elif kind == "event":
                    self.send({**data, "type": "event"})
        finally:
            self.bridges.discard(ws)
            self.voice = "sleep"
            self.send(self.status())
        return ws

    async def bridge_js(self, _request):
        return web.FileResponse(BRIDGE_JS, headers={"Cache-Control": "no-store"})

    # ---------- 'live' 명령 (이 Pi 안에서만: 127.0.0.1) ----------

    @staticmethod
    def check_local(request):
        # 브라우저 페이지가 몰래 부르지 못하게, live 명령만 붙이는 헤더를 요구
        if request.headers.get("X-Live-Local") != "1":
            raise web.HTTPForbidden(text="live 명령으로만 쓸 수 있음\n")

    async def local_status(self, request):
        self.check_local(request)
        project = self.state["project"] or "없음"
        lines = [
            f"기기   {socket.gethostname()}  (id {self.id}, agent {VERSION})",
            f"중앙   {self.central}",
            f"작품   {project} — {self.phase}",
            f"화면   {self.screen_line()}",
            f"페이지 {'연결됨' if self.bridges else '없음'} · mic {self.voice}",
            f"API 키 {'있음' if self.effective_env().get('GEMINI_API_KEY') else '없음'}",
        ]
        return web.Response(text="\n".join(lines) + "\n")

    def screen_line(self):
        if self.display_error:
            return f"못 읽음 ({self.display_error})"
        if not self.displays:
            return "아직 모름 (데스크톱이 뜨면 보임)"
        listed = ", ".join(f"{d['name']} {d['w']}×{d['h']}{'' if d['enabled'] else ' (꺼짐)'}" for d in self.displays)
        return f"작품 → {self.screen or '기본 화면'}  [{listed}]"

    async def local_logs(self, request):
        self.check_local(request)
        return web.Response(text="\n".join(list(self.logs)[-60:]) + "\n")

    async def local_action(self, request):
        self.check_local(request)
        action = request.match_info["action"]
        try:
            if action == "run":
                message = await self.local_run((await request.json()).get("path", ""))
            elif action == "key":
                message = await self.set_local_key((await request.json()).get("key", ""))
            elif action in ("stop", "start", "restart"):
                async with self.project_lock:
                    if action != "start":
                        await self.stop(release_screen=action == "stop")
                    if action != "stop":
                        await self.start()
                message = {"stop": "멈춤 (다시 켜기: live start)", "start": "켬", "restart": "다시 켬"}[action]
            else:
                raise web.HTTPNotFound(text="모르는 명령\n")
        except (RuntimeError, ValueError, OSError) as error:
            self.log(f"live {action} 실패: {error}")
            return web.Response(status=400, text=f"실패: {error}\n")
        return web.Response(text=f"{message}\n")

    async def serve_bridge(self):
        app = web.Application(client_max_size=2 << 20)
        app.router.add_get("/bridge", self.bridge_ws)
        app.router.add_get("/live-bridge.js", self.bridge_js)
        app.router.add_get("/local/status", self.local_status)
        app.router.add_get("/local/logs", self.local_logs)
        app.router.add_post("/local/{action}", self.local_action)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", self.bridge_port).start()

    async def main(self):
        self.log(f"Live Agent {VERSION} 시작 (id {self.id})")
        ensure_wrappers()
        await self.serve_bridge()
        await self.listen_beacon()
        if self.state["running"] and self.state["manifest"]:
            asyncio.create_task(self.handle({"type": "start"}))
        await asyncio.gather(self.central_loop(), self.status_loop(), self.hw_loop())


async def run():
    await Agent().main()


if __name__ == "__main__":
    asyncio.run(run())
