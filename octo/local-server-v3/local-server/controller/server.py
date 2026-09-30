"""Live Control: central dashboard + WebSocket hub for every Raspberry Pi on the LAN."""

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import random
import re
import secrets
import shutil
import socket
import tempfile
import time
import unicodedata
import uuid
import zipfile
from collections import deque
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

from aiohttp import WSMsgType, web

HERE = Path(__file__).resolve().parent
SERVER = HERE.parent
REPO = SERVER.parent
DATA = HERE / "data"
UPLOADS = DATA / "uploads"
STORE = DATA / "state.json"
STATIC = HERE / "static"
AGENT_FILES = {
    "agent.py": SERVER / "agent" / "agent.py",
    "live-bridge.js": SERVER / "bridge" / "live-bridge.js",
}
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", "__MACOSX", "local-server"}
SKIP_FILES = {".DS_Store", ".env", "key.js", "config.local.js"}
UPLOAD_SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", "__MACOSX"}
JUNK_FILES = {".DS_Store", "Thumbs.db", "desktop.ini"}
STORED_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp3", ".mp4", ".m4a", ".ogg", ".webm",
              ".mov", ".zip", ".gz", ".woff", ".woff2", ".glb"}
DEVICE_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
PROJECT_NAME = re.compile(r"^[\w-]{1,40}$")
ACTIONS = {"start", "stop", "restart", "deploy", "identify", "update_agent", "reboot", "poweroff", "ping"}
QUIET_COMMANDS = {"get_prompt", "get_logs", "audio", "ping"}
OPEN_PATHS = {"/join", "/ws/pair"}  # 토큰 없이 열리는 곳: Pi 설치 스크립트(토큰 없음)와 연결 요청
BEACON_PORT = 8701
UPLOAD_LIMIT = 500 * 1024 * 1024

DEFAULT_KICKOFF = (
    "(진행자 메시지) 지금 전시를 보러 온 관람객이 \"{utterance}\"라고 말했어. "
    "먼저 관람객이 한 말을 네 말투 그대로 주변 AI 친구들에게 전해주고, "
    "\"{topic}\"에 대한 네 생각을 한두 문장으로 말해. "
    "그 다음부터는 주변에서 들리는 다른 AI 친구들의 말에 자연스럽게 반응해."
)
DEFAULT_SETTINGS = {
    "default_key": "",
    "target": "random",
    "kickoff": DEFAULT_KICKOFF,
    "others_delay": 1.5,
    "max_seconds": 180,
    "screen_rotate": "keep",   # 2인치 화면 방향 (모든 Pi): keep | 0 | 90 | 180 | 270
    "screen_only": False,      # True면 작품 켤 때 모니터를 끄고 2인치 화면만 씀
}
ROTATE_VALUES = {"keep", "0", "90", "180", "270"}
SCREEN_DEFAULT = {"target": "auto", "rotate": "default"}


# ---------- 저장소 ----------

class Store:
    def __init__(self):
        DATA.mkdir(exist_ok=True)
        UPLOADS.mkdir(exist_ok=True)
        # 서버가 도중에 꺼졌을 때 남은 임시 파일 정리
        for leftover in [*DATA.glob(".bundle-*"), *UPLOADS.glob(".incoming-*"), *UPLOADS.glob(".old-*")]:
            if leftover.is_dir():
                shutil.rmtree(leftover, ignore_errors=True)
            else:
                leftover.unlink(missing_ok=True)
        try:
            data = json.loads(STORE.read_text())
        except (FileNotFoundError, ValueError):
            data = {}
        self.token = data.get("token") or secrets.token_hex(8)
        self.settings = DEFAULT_SETTINGS | data.get("settings", {})
        self.devices = data.get("devices", {})
        self.save()

    def save(self):
        tmp = STORE.with_suffix(".tmp")
        tmp.write_text(json.dumps(
            {"token": self.token, "settings": self.settings, "devices": self.devices},
            ensure_ascii=False, indent=2,
        ))
        tmp.chmod(0o600)
        tmp.replace(STORE)

    def device(self, dev_id, name=None):
        if dev_id not in self.devices:
            self.devices[dev_id] = {
                "name": name or dev_id, "role": "agent", "project": None,
                "manifest": None, "env": {}, "prompt": None,
            }
            self.save()
        return self.devices[dev_id]


# ---------- 프로젝트 (repo 폴더 + 업로드한 것) ----------

def subfolders(base):
    try:
        return sorted((p for p in base.iterdir() if p.is_dir() and not p.name.startswith(".")),
                      key=lambda p: p.name.lower())
    except OSError:
        return []


def list_projects():
    projects = []
    # local-server를 홈 폴더에 바로 두면 Desktop·Downloads 같은 게 다 뜨니까 그때는 repo 목록을 안 봄
    if REPO not in (Path.home(), Path("/")):
        for folder in subfolders(REPO):
            if folder.name not in SKIP_DIRS:
                projects.append({"id": f"repo:{folder.name}", "label": folder.name})
    for folder in subfolders(SERVER / "examples"):
        projects.append({"id": f"example:{folder.name}", "label": f"예제 · {folder.name}"})
    for folder in subfolders(UPLOADS):
        projects.append({"id": f"upload:{folder.name}", "label": f"업로드 · {folder.name}"})
    return projects


def project_path(project_id):
    kind, _, name = (project_id or "").partition(":")
    base = {"repo": REPO, "example": SERVER / "examples", "upload": UPLOADS}.get(kind)
    if not base or not name or "/" in name or "\\" in name or name.startswith("."):
        raise web.HTTPBadRequest(text="프로젝트를 먼저 선택하세요")
    path = base / name
    if not path.is_dir():
        raise web.HTTPNotFound(text="프로젝트 폴더가 없음")
    return path


def guess_manifest(folder):
    """live.json이 없으면 폴더 모양을 보고 실행 방법을 추측한다."""
    live = folder / "live.json"
    if live.exists():
        try:
            manifest = json.loads(live.read_text())
        except ValueError as error:
            raise web.HTTPBadRequest(text=f"{folder.name}/live.json 문법 오류: {error}")
        if not isinstance(manifest, dict):
            raise web.HTTPBadRequest(text=f"{folder.name}/live.json은 {{ }} 객체여야 함")
        return manifest
    names = {p.name for p in folder.iterdir()}
    manifest = {"name": folder.name, "setup": "", "run": [], "browser": None, "files": {}, "prompt_file": None}
    manifest["prompt_file"] = next((n for n in ("persona.txt", "prompt.txt") if n in names), None)
    scripts = sorted(n for n in names if n.endswith(".sh"))
    htmls = sorted(n for n in names if n.endswith(".html"))
    if "server.py" in names:
        python = "python3"
        if "requirements.txt" in names:
            manifest["setup"] = "python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
            python = ".venv/bin/python"
        manifest["run"] = [f"{python} server.py --port 8000"]
        manifest["browser"] = {"url": "http://localhost:8000/"}
        manifest["files"][".env"] = "GEMINI_API_KEY=${GEMINI_API_KEY}\n"
    elif scripts:
        manifest["run"] = [f"bash {scripts[0]}"]
    elif htmls:
        page = "" if "index.html" in names else htmls[0]
        manifest["run"] = ["python3 -m http.server 8080 --bind 127.0.0.1"]
        manifest["browser"] = {"url": f"http://localhost:8080/{page}"}
    if names & {"key.example.js", "key.js"}:
        manifest["files"]["key.js"] = 'window.GEMINI_KEY = "${GEMINI_API_KEY}";\n'
    if names & {"config.example.js", "config.local.js"}:
        manifest["files"]["config.local.js"] = 'window.LOCAL_CONFIG = { apiKey: "${GEMINI_API_KEY}" };\n'
    return manifest


def build_bundle(folder):
    """프로젝트 폴더 → 임시 zip 파일 (큰 프로젝트도 메모리에 다 안 올림)."""
    with tempfile.NamedTemporaryFile(dir=DATA, prefix=".bundle-", suffix=".zip", delete=False) as tmp:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=5) as archive:
            for path in sorted(folder.rglob("*")):
                rel = path.relative_to(folder)
                if any(part in SKIP_DIRS for part in rel.parts) or path.name in SKIP_FILES:
                    continue
                if path.is_file():
                    stored = path.suffix.lower() in STORED_EXT
                    archive.write(path, rel.as_posix(),
                                  compress_type=zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED)
    return Path(tmp.name)


# ---------- 업로드 (zip / 폴더 / html 하나) ----------

def clean_name(raw):
    """파일·폴더 이름 → 프로젝트 이름 (한글 OK, 공백·특수문자는 -)."""
    stem = PurePosixPath(str(raw or "").replace("\\", "/")).name
    stem = re.sub(r"\.(zip|html?)$", "", unicodedata.normalize("NFC", stem), flags=re.I)
    return re.sub(r"[^\w-]+", "-", stem).strip("-_")[:40].strip("-_")


def safe_parts(rel):
    """업로드 안의 경로 → 안전한 경로 조각. 버릴 파일(.DS_Store, .git/ 등)이면 None."""
    rel = unicodedata.normalize("NFC", str(rel).replace("\\", "/"))  # 맥 한글 파일명(자모 분리) → 보통 한글
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        return None
    if any(p in UPLOAD_SKIP_DIRS for p in parts[:-1]) or parts[-1] in JUNK_FILES or parts[-1].startswith("._"):
        return None
    return parts


def extract_zip(src, dest):
    """zip 풀기. 맥/윈도우에서 만든 zip의 한글 파일명도 안 깨지게."""
    with zipfile.ZipFile(src) as archive:
        for info in archive.infolist():
            name = info.filename
            if not info.flag_bits & 0x800:  # 'UTF-8' 표시가 없는 zip
                raw = name.encode("cp437")
                for encoding in ("utf-8", "cp949"):
                    try:
                        name = raw.decode(encoding)
                        break
                    except UnicodeDecodeError:
                        pass
            parts = safe_parts(name)
            if not parts or info.is_dir():
                continue
            target = dest.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, open(target, "wb") as out:
                shutil.copyfileobj(source, out)


def unwrap(folder):
    """안에 폴더 하나만 들어 있으면 그 폴더가 프로젝트. → (프로젝트 폴더, 벗긴 폴더 이름)"""
    top = ""
    while True:
        entries = [p for p in folder.iterdir() if p.name not in JUNK_FILES]
        if len(entries) == 1 and entries[0].is_dir() and entries[0].name not in UPLOAD_SKIP_DIRS:
            folder = entries[0]
            top = top or folder.name
        else:
            return folder, top


async def receive_upload(request):
    """업로드 받기 → (작업 폴더, 풀린 프로젝트 폴더, 이름 후보).
    받는 것: zip 1개 / html 1개 / 폴더 (파일 여러 개 + 'paths' 목록)."""
    reader = await request.multipart()
    work = Path(tempfile.mkdtemp(prefix=".incoming-", dir=UPLOADS))
    try:
        name, paths, files, total = "", None, [], 0
        while (part := await reader.next()) is not None:
            if part.name == "name":
                name = (await part.text()).strip()
            elif part.name == "paths":
                try:
                    paths = json.loads(await part.text())
                except ValueError:
                    raise web.HTTPBadRequest(text="폴더 업로드 정보가 이상함 (paths)")
            elif part.name in ("file", "f"):
                raw = work / f"raw-{len(files)}"
                with open(raw, "wb") as out:
                    while chunk := await part.read_chunk(1 << 16):
                        total += len(chunk)
                        if total > UPLOAD_LIMIT:
                            raise web.HTTPBadRequest(text="너무 큼 (한 번에 500MB까지)")
                        out.write(chunk)
                filename = part.filename or ""
                if re.search(r"%[0-9A-Fa-f]{2}", filename):  # 일부 프로그램은 한글 파일명을 %ED%97.. 로 보냄
                    filename = unquote(filename)
                files.append((filename, raw))
        if not files:
            raise web.HTTPBadRequest(text="올린 파일이 없음")
        project = work / "project"
        project.mkdir()
        if paths is not None:
            if not isinstance(paths, list) or len(paths) != len(files):
                raise web.HTTPBadRequest(text="폴더 업로드 정보가 이상함 (paths)")
            for rel, (_, raw) in zip(paths, files):
                parts = safe_parts(rel)
                if parts:
                    target = project.joinpath(*parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    raw.replace(target)
            guess = ""
        else:
            filename, raw = files[0]
            if zipfile.is_zipfile(raw):
                try:
                    await asyncio.to_thread(extract_zip, raw, project)
                except zipfile.BadZipFile:
                    raise web.HTTPBadRequest(text="zip 파일이 깨졌음")
                raw.unlink()
                guess = clean_name(filename)
                if guess.lower().startswith("archive"):  # 맥에서 여러 개를 묶으면 'Archive.zip'
                    guess = ""
            elif filename.lower().endswith((".html", ".htm")):
                raw.replace(project / (safe_parts(filename) or ["index.html"])[-1])
                guess = clean_name(filename)
            else:
                raise web.HTTPBadRequest(text="zip 파일, html 파일, 또는 폴더를 올려주세요")
        root, top = unwrap(project)
        if not any(root.iterdir()):
            raise web.HTTPBadRequest(text="빈 폴더/zip 임")
        return work, root, clean_name(name) or guess or clean_name(top)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise


def install_upload(work, root, name):
    """받은 프로젝트를 data/uploads/<이름>으로 (같은 이름이 있으면 새 버전으로 교체)."""
    if not PROJECT_NAME.match(name):
        shutil.rmtree(work, ignore_errors=True)
        raise web.HTTPBadRequest(text=f"이름으로 쓸 수 없음: {name}")
    target = UPLOADS / name
    old = UPLOADS / f".old-{uuid.uuid4().hex[:8]}"
    if target.exists():
        target.rename(old)
    root.rename(target)
    shutil.rmtree(old, ignore_errors=True)
    shutil.rmtree(work, ignore_errors=True)


# ---------- Pi 설치 스크립트 ----------

def render_setup(controller, token=""):
    """Pi 설치 스크립트 (agent 파일을 안에 넣음 → 중앙에서 따로 받을 필요 없음).
    token이 있으면 바로 연결, 없으면 Pi가 '연결 요청'을 보내고 대시보드에서 허용."""
    script = (SERVER / "agent" / "install.sh").read_text()
    for placeholder, name in (("__AGENT_PY__", "agent.py"), ("__BRIDGE_JS__", "live-bridge.js")):
        script = script.replace(placeholder, base64.b64encode(AGENT_FILES[name].read_bytes()).decode())
    return script.replace("__CONTROLLER__", controller.rstrip("/")).replace("__TOKEN__", token)


# ---------- 기기 연결 허브 ----------

def mask(value):
    return f"…{value[-4:]}" if value else ""


def lan_ip():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def beacon_sign(token, host, port):
    return hmac.new(token.encode(), f"{host}:{port}".encode(), hashlib.sha256).hexdigest()[:24]


class Hub:
    def __init__(self, store, public_url, port):
        self.store = store
        self.public_url = public_url
        self.port = port
        self.sockets = {}
        self.live = {}
        self.logs = {}
        self.pending = {}
        self.dashboards = set()
        self.show = {"mode": "idle", "topic": None, "target": None, "started": None}
        self.show_tasks = []
        self.push_scheduled = False
        self.pairing = {}

    # --- 주소 ---

    def ip_url(self):
        return f"http://{lan_ip()}:{self.port}"

    def install_command(self, base):
        return f'curl -fsSL "{base}/install.sh?t={self.store.token}" | bash'

    @staticmethod
    def join_command(base):
        return f"curl -fsSL {base}/join | bash"

    def addresses(self):
        ip_url = self.ip_url()
        return {
            "join": self.join_command(self.public_url),
            "join_ip": self.join_command(ip_url) if ip_url != self.public_url else None,
            "dashboard": f"{self.public_url}/?t={self.store.token}",
            "dashboard_ip": f"{ip_url}/?t={self.store.token}" if ip_url != self.public_url else None,
            "install": self.install_command(self.public_url),
            "install_ip": self.install_command(ip_url) if ip_url != self.public_url else None,
        }

    # --- 대시보드로 보내기 ---

    def snapshot(self):
        devices = []
        for dev_id, rec in self.store.devices.items():
            live = self.live.get(dev_id, {})
            devices.append({
                "id": dev_id,
                "name": rec["name"],
                "role": rec["role"],
                "project": rec["project"],
                "manifest": rec["manifest"],
                "key": mask(rec["env"].get("GEMINI_API_KEY", "")),
                "custom_prompt": rec["prompt"] is not None,
                "deploy_pending": bool(rec.get("deploy_pending")),
                "screen": SCREEN_DEFAULT | (rec.get("screen") or {}),
                "online": dev_id in self.sockets,
                "status": live,
            })
        settings = dict(self.store.settings, default_key=mask(self.store.settings["default_key"]))
        addresses = self.addresses()
        return {
            "devices": devices,
            "projects": list_projects(),
            "settings": settings,
            "show": self.show,
            "install": addresses["install"],
            "install_ip": addresses["install_ip"],
            "join": addresses["join"],
            "join_ip": addresses["join_ip"],
            "pairing": [
                {"id": dev_id, "name": p["name"], "ip": p["ip"], "since": p["since"],
                 "known": (self.store.devices.get(dev_id) or {}).get("name")}
                for dev_id, p in self.pairing.items()
            ],
        }

    def push_state(self):
        if self.push_scheduled:
            return
        self.push_scheduled = True

        async def later():
            await asyncio.sleep(0.2)
            self.push_scheduled = False
            await self.to_dashboards({"type": "state", "state": self.snapshot()})

        asyncio.get_running_loop().create_task(later())

    async def to_dashboards(self, msg):
        for ws in list(self.dashboards):
            try:
                await ws.send_json(msg)
            except ConnectionError:
                self.dashboards.discard(ws)

    def add_log(self, dev_id, line):
        self.logs.setdefault(dev_id, deque(maxlen=500)).append(line)
        asyncio.get_running_loop().create_task(
            self.to_dashboards({"type": "log", "device": dev_id, "line": line})
        )

    def note(self, dev_id, text):
        self.add_log(dev_id, f"{time.strftime('%H:%M:%S')} [중앙] {text}")

    # --- 기기로 보내기 ---

    async def send(self, dev_id, msg):
        ws = self.sockets.get(dev_id)
        if ws is None:  # 'if not ws' 쓰면 안 됨: 옛 aiohttp(apt 버전)에서는 연결된 ws도 False로 나옴
            return False
        try:
            await ws.send_json(msg)
            return True
        except ConnectionError:
            return False

    async def command(self, dev_id, msg, wait=None):
        msg = dict(msg, req=uuid.uuid4().hex)
        future = asyncio.get_running_loop().create_future()
        if wait is not None:
            self.pending[msg["req"]] = future
        if not await self.send(dev_id, msg):
            self.pending.pop(msg["req"], None)
            raise web.HTTPConflict(text="기기가 오프라인")
        if wait is None:
            return None
        try:
            return await asyncio.wait_for(future, wait)
        except asyncio.TimeoutError:
            raise web.HTTPGatewayTimeout(text="기기가 응답하지 않음")
        finally:
            self.pending.pop(msg["req"], None)

    def device_config(self, dev_id):
        rec = self.store.devices[dev_id]
        env = dict(rec["env"])
        if not env.get("GEMINI_API_KEY") and self.store.settings["default_key"]:
            env["GEMINI_API_KEY"] = self.store.settings["default_key"]
        screen = SCREEN_DEFAULT | (rec.get("screen") or {})
        rotate = self.store.settings["screen_rotate"] if screen["rotate"] == "default" else screen["rotate"]
        return {
            "type": "config", "name": rec["name"], "role": rec["role"], "env": env, "prompt": rec["prompt"],
            "screen": {"target": screen["target"], "rotate": rotate, "only": bool(self.store.settings["screen_only"])},
        }

    async def push_config(self, dev_id):
        await self.send(dev_id, self.device_config(dev_id))

    def deploy_message(self, dev_id):
        rec = self.store.devices[dev_id]
        folder = project_path(rec["project"])
        manifest = rec["manifest"] or guess_manifest(folder)
        return {"type": "deploy", "project": rec["project"], "path": f"/bundle/{dev_id}.zip", "manifest": manifest}

    async def deploy(self, dev_id):
        """지금 켜져 있으면 바로 설치, 꺼져 있으면 켜지는 순간 설치. → 바로 보냈으면 True"""
        rec = self.store.devices[dev_id]
        message = self.deploy_message(dev_id)  # 프로젝트/manifest 문제는 여기서 바로 에러로
        try:
            await self.command(dev_id, message)
            rec["deploy_pending"] = False
            sent = True
        except web.HTTPConflict:
            rec["deploy_pending"] = True
            sent = False
        self.store.save()
        return sent

    def voice_for(self, dev_id):
        role = self.store.devices[dev_id]["role"]
        if role == "master" or self.show["mode"] == "chat":
            return {"type": "wake"}
        return {"type": "sleep"}

    async def voice(self, dev_id, msg):
        await self.send(dev_id, {"type": "voice", "msg": msg})

    # --- 쇼(대화) 진행 ---

    def ready_agents(self):
        return [
            dev_id for dev_id in self.sockets
            if self.store.devices[dev_id]["role"] == "agent" and self.live.get(dev_id, {}).get("bridge")
        ]

    async def start_chat(self, topic, utterance="", target=None):
        topic = (topic or "").strip()[:200]
        if not topic:
            raise web.HTTPBadRequest(text="주제가 비어 있음")
        agents = self.ready_agents()
        if not agents:
            raise web.HTTPConflict(text="대화할 수 있는 agent 기기가 없음 (온라인 + 페이지 bridge 연결 필요)")
        await self.stop_chat(quiet=True)
        pick = target or self.store.settings["target"]
        if pick not in agents:
            pick = random.choice(agents)
        utterance = (utterance or "").strip()[:500] or f"{topic}에 대해 대화해봐"
        text = self.store.settings["kickoff"].replace("{topic}", topic).replace("{utterance}", utterance)
        self.show = {"mode": "chat", "topic": topic, "target": pick, "started": time.time()}
        for dev_id in list(self.sockets):
            await self.voice(dev_id, {"type": "mode", "mode": "chat"})
        await self.voice(pick, {"type": "kickoff", "text": text})
        self.add_log(pick, f"{time.strftime('%H:%M:%S')} [show] 첫 발언자로 선택됨 · 주제: {topic}")

        async def wake_others():
            await asyncio.sleep(float(self.store.settings["others_delay"]))
            for dev_id in self.ready_agents():
                if dev_id != pick:
                    await self.voice(dev_id, {"type": "wake"})

        async def time_limit():
            await asyncio.sleep(float(self.store.settings["max_seconds"]))
            await self.stop_chat()

        self.show_tasks = [asyncio.create_task(wake_others()), asyncio.create_task(time_limit())]
        self.push_state()

    async def stop_chat(self, quiet=False):
        current = asyncio.current_task()
        for task in self.show_tasks:
            if task is not current:
                task.cancel()
        self.show_tasks = []
        was_chatting = self.show["mode"] == "chat"
        self.show = {"mode": "idle", "topic": None, "target": None, "started": None}
        if quiet and not was_chatting:
            return
        for dev_id in list(self.sockets):
            await self.voice(dev_id, {"type": "mode", "mode": "idle"})
            if self.store.devices[dev_id]["role"] == "agent":
                await self.voice(dev_id, {"type": "sleep"})
        self.push_state()


# ---------- HTTP ----------

def token_of(request):
    return request.query.get("t") or request.headers.get("X-Token") or request.cookies.get("live_token") or ""


@web.middleware
async def auth(request, handler):
    store = request.app["store"]
    if request.path in OPEN_PATHS:
        return await handler(request)
    if not secrets.compare_digest(token_of(request), store.token):
        if request.path == "/":
            return web.Response(
                text="토큰이 필요함. 중앙 컴퓨터에서 url.sh를 실행하거나 터미널에 출력된 주소(…/?t=토큰)로 들어오세요.",
                status=401,
            )
        raise web.HTTPUnauthorized(text="bad token")
    if request.path == "/" and "t" in request.query:
        response = web.Response(status=302, headers={"Location": "/"})
        response.set_cookie("live_token", store.token, httponly=True, samesite="Strict", max_age=60 * 60 * 24 * 90)
        return response
    return await handler(request)


async def index(_request):
    return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


def request_base(request):
    # Pi가 이 스크립트를 받아간 주소(.local 이름 또는 IP)를 그대로 중앙 주소로 저장
    return f"{request.scheme}://{request.host}" if request.host else request.app["hub"].public_url


async def install_sh(request):
    """토큰 포함 설치 (허용 필요 없음)."""
    script = render_setup(request_base(request), request.app["hub"].store.token)
    return web.Response(text=script, content_type="text/x-shellscript")


async def join_sh(request):
    """토큰 없는 설치: Pi가 설치 후 '연결 요청' → 대시보드에서 허용."""
    return web.Response(text=render_setup(request_base(request)), content_type="text/x-shellscript")


async def agent_file(request):
    path = AGENT_FILES.get(request.match_info["name"])
    if not path:
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Cache-Control": "no-store"})


async def bundle(request):
    hub = request.app["hub"]
    dev_id = request.match_info["dev_id"]
    rec = hub.store.devices.get(dev_id)
    if not rec:
        raise web.HTTPNotFound()
    path = await asyncio.to_thread(build_bundle, project_path(rec["project"]))
    try:
        response = web.StreamResponse(headers={"Content-Type": "application/zip",
                                               "Content-Length": str(path.stat().st_size)})
        await response.prepare(request)
        with open(path, "rb") as source:
            while chunk := source.read(1 << 16):
                await response.write(chunk)
        await response.write_eof()
        return response
    finally:
        path.unlink(missing_ok=True)


async def api_state(request):
    return web.json_response(request.app["hub"].snapshot())


async def api_update_device(request):
    hub = request.app["hub"]
    dev_id = request.match_info["dev_id"]
    rec = hub.store.devices.get(dev_id)
    if not rec:
        raise web.HTTPNotFound()
    body = await request.json()
    if "name" in body:
        rec["name"] = str(body["name"]).strip()[:40] or dev_id
    if body.get("role") in ("agent", "master"):
        rec["role"] = body["role"]
    if "project" in body:
        rec["project"] = body["project"] or None
        rec["manifest"] = None
        rec["deploy_pending"] = False
    if "manifest" in body:
        manifest = body["manifest"]
        if manifest is not None and not isinstance(manifest, dict):
            raise web.HTTPBadRequest(text="manifest는 JSON 객체")
        rec["manifest"] = manifest
    if "key" in body:
        key = str(body["key"]).strip()
        if key:
            rec["env"]["GEMINI_API_KEY"] = key
        else:
            rec["env"].pop("GEMINI_API_KEY", None)
    if "screen" in body:
        screen = body["screen"] or {}
        if not isinstance(screen, dict):
            raise web.HTTPBadRequest(text="screen은 객체")
        target = str(screen.get("target") or "auto").strip()[:40]
        rotate = str(screen.get("rotate") or "default")
        if rotate not in ROTATE_VALUES | {"default"}:
            raise web.HTTPBadRequest(text="방향은 default/keep/0/90/180/270")
        rec["screen"] = {"target": target, "rotate": rotate}
    if "prompt" in body:
        rec["prompt"] = None if body["prompt"] is None else str(body["prompt"])[:20000]
    hub.store.save()
    await hub.push_config(dev_id)
    if "role" in body:
        await hub.voice(dev_id, hub.voice_for(dev_id))
    hub.push_state()
    return web.json_response({"ok": True})


async def api_forget(request):
    hub = request.app["hub"]
    dev_id = request.match_info["dev_id"]
    if dev_id in hub.sockets:
        raise web.HTTPConflict(text="온라인 기기는 목록에서 뺄 수 없음")
    hub.store.devices.pop(dev_id, None)
    hub.store.save()
    hub.push_state()
    return web.json_response({"ok": True})


async def api_action(request):
    hub = request.app["hub"]
    dev_id = request.match_info["dev_id"]
    action = request.match_info["action"]
    if dev_id not in hub.store.devices or action not in ACTIONS:
        raise web.HTTPNotFound()
    if action == "deploy":
        await hub.command(dev_id, hub.deploy_message(dev_id))
        hub.store.devices[dev_id]["deploy_pending"] = False
        hub.store.save()
    else:
        await hub.command(dev_id, {"type": action})
    return web.json_response({"ok": True})


async def api_all(request):
    hub = request.app["hub"]
    action = request.match_info["action"]
    if action not in ACTIONS:
        raise web.HTTPNotFound()
    sent, skipped = [], []
    for dev_id in list(hub.sockets):
        try:
            msg = hub.deploy_message(dev_id) if action == "deploy" else {"type": action}
            await hub.command(dev_id, msg)
            sent.append(dev_id)
        except web.HTTPException:
            skipped.append(dev_id)
    return web.json_response({"ok": True, "sent": sent, "skipped": skipped})


async def api_prompt(request):
    hub = request.app["hub"]
    dev_id = request.match_info["dev_id"]
    result = await hub.command(dev_id, {"type": "get_prompt"}, wait=10)
    if not result.get("ok"):
        raise web.HTTPConflict(text=result.get("message") or "실패")
    return web.json_response({"prompt": result.get("data") or ""})


async def api_audio(request):
    """이 기기의 스피커/마이크: GET = 목록·볼륨, POST {op:set|test, ...}"""
    hub = request.app["hub"]
    dev_id = request.match_info["dev_id"]
    if dev_id not in hub.store.devices:
        raise web.HTTPNotFound()
    msg = {"type": "audio", "op": "get"}
    if request.method == "POST":
        body = await request.json()
        if body.get("op") not in ("set", "test"):
            raise web.HTTPBadRequest(text="op는 set 또는 test")
        keys = ("op", "sink", "source", "sink_volume", "source_volume", "what")
        msg.update({k: body[k] for k in keys if k in body})
    result = await hub.command(dev_id, msg, wait=20)
    if not result.get("ok"):
        raise web.HTTPConflict(text=result.get("message") or "실패")
    return web.json_response(result.get("data") or {})


async def api_logs(request):
    hub = request.app["hub"]
    return web.json_response({"lines": list(hub.logs.get(request.match_info["dev_id"], []))})


async def api_manifest(request):
    return web.json_response(guess_manifest(project_path(request.query.get("project"))))


async def api_settings(request):
    hub = request.app["hub"]
    body = await request.json()
    settings = hub.store.settings
    if "default_key" in body:
        settings["default_key"] = str(body["default_key"]).strip()
    if "target" in body:
        settings["target"] = str(body["target"])
    if "kickoff" in body:
        settings["kickoff"] = str(body["kickoff"])[:4000] or DEFAULT_KICKOFF
    screen_changed = False
    if "screen_rotate" in body:
        rotate = str(body["screen_rotate"])
        if rotate not in ROTATE_VALUES:
            raise web.HTTPBadRequest(text="방향은 keep/0/90/180/270")
        screen_changed |= settings["screen_rotate"] != rotate
        settings["screen_rotate"] = rotate
    if "screen_only" in body:
        only = bool(body["screen_only"])
        screen_changed |= settings["screen_only"] != only
        settings["screen_only"] = only
    for name, low, high in (("others_delay", 0, 30), ("max_seconds", 10, 3600)):
        if name in body:
            settings[name] = min(max(float(body[name]), low), high)
    hub.store.save()
    if "default_key" in body or screen_changed:
        for dev_id in list(hub.sockets):
            await hub.push_config(dev_id)
    hub.push_state()
    return web.json_response({"ok": True})


async def api_show_start(request):
    body = await request.json()
    await request.app["hub"].start_chat(body.get("topic"), body.get("utterance"), body.get("target"))
    return web.json_response({"ok": True})


async def api_show_stop(request):
    await request.app["hub"].stop_chat()
    return web.json_response({"ok": True})


async def api_upload(request):
    """전체 제어 패널의 업로드: 목록에만 추가 (기기에 안 보냄)."""
    work, root, name = await receive_upload(request)
    name = name or f"project-{time.strftime('%m%d-%H%M%S')}"
    await asyncio.to_thread(install_upload, work, root, name)
    request.app["hub"].push_state()
    return web.json_response({"ok": True, "project": f"upload:{name}", "label": name})


async def api_device_upload(request):
    """기기 카드에 올리기: 저장 → 이 기기의 프로젝트로 지정 → 바로 설치 (꺼져 있으면 켜질 때)."""
    hub = request.app["hub"]
    dev_id = request.match_info["dev_id"]
    rec = hub.store.devices.get(dev_id)
    if not rec:
        raise web.HTTPNotFound()
    work, root, name = await receive_upload(request)
    name = name or clean_name(rec["name"]) or f"device-{dev_id}"[:40]
    await asyncio.to_thread(install_upload, work, root, name)
    project = f"upload:{name}"
    if rec["project"] != project:
        rec["manifest"] = None  # 다른 작품이면 실행 방법을 새로 추측 (같은 작품 새 버전이면 고친 manifest 유지)
    rec["project"] = project
    hub.store.save()
    deployed = await hub.deploy(dev_id)
    hub.note(dev_id, f"작품 업로드: {name} → {'설치 시작' if deployed else '기기가 켜지면 자동 설치'}")
    hub.push_state()
    return web.json_response({"ok": True, "project": project, "label": name, "deployed": deployed})


async def api_pair(request):
    """대시보드의 '연결 요청' 허용/거절."""
    hub = request.app["hub"]
    dev_id = request.match_info["dev_id"]
    action = request.match_info["action"]
    entry = hub.pairing.get(dev_id)
    if entry is None or action not in ("approve", "deny"):
        raise web.HTTPNotFound(text="연결 요청이 없음 (이미 처리됐거나 Pi 연결이 끊김)")
    hub.pairing.pop(dev_id, None)
    ws = entry["ws"]
    try:
        if action == "approve":
            hub.store.device(dev_id, entry["name"])
            await ws.send_json({"type": "paired", "token": hub.store.token})
        else:
            await ws.send_json({"type": "pair_denied"})
    except ConnectionError:
        raise web.HTTPConflict(text="Pi 연결이 끊겼음 — Pi가 곧 다시 요청함")
    finally:
        await ws.close()
        hub.push_state()
    return web.json_response({"ok": True})


# ---------- WebSocket ----------

async def pair_ws(request):
    """토큰 없는 Pi의 '연결 요청'. 대시보드에서 허용하면 토큰을 넘겨줌."""
    hub = request.app["hub"]
    dev_id = request.query.get("id", "")
    if not DEVICE_ID.match(dev_id):
        raise web.HTTPBadRequest()
    if dev_id not in hub.pairing and len(hub.pairing) >= 20:
        raise web.HTTPTooManyRequests(text="연결 요청이 너무 많음")
    ws = web.WebSocketResponse(heartbeat=15, max_msg_size=4096)
    await ws.prepare(request)
    old = hub.pairing.get(dev_id)
    if old is not None:
        await old["ws"].close()
    name = re.sub(r"\s+", " ", request.query.get("name", "")).strip()[:40] or dev_id
    hub.pairing[dev_id] = {"ws": ws, "name": name, "ip": request.remote or "", "since": time.time()}
    hub.push_state()
    try:
        await ws.send_json({"type": "pair_wait"})
        async for _ in ws:
            pass
    finally:
        if (hub.pairing.get(dev_id) or {}).get("ws") is ws:
            del hub.pairing[dev_id]
            hub.push_state()
    return ws


async def dashboard_ws(request):
    hub = request.app["hub"]
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    hub.dashboards.add(ws)
    await ws.send_json({"type": "state", "state": hub.snapshot()})
    try:
        async for _ in ws:
            pass
    finally:
        hub.dashboards.discard(ws)
    return ws


async def device_ws(request):
    hub = request.app["hub"]
    dev_id = request.query.get("id", "")
    if not DEVICE_ID.match(dev_id):
        raise web.HTTPBadRequest()
    ws = web.WebSocketResponse(heartbeat=15, max_msg_size=1 << 20)
    await ws.prepare(request)
    old = hub.sockets.get(dev_id)
    if old is not None:
        await old.close()
    rec = hub.store.device(dev_id)
    hub.sockets[dev_id] = ws
    try:
        async for message in ws:
            if message.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(message.data)
            except ValueError:
                continue
            kind = data.get("type")
            if kind == "hello":
                if rec["name"] == dev_id and data.get("name"):
                    rec["name"] = str(data["name"])[:40]
                    hub.store.save()
                hub.live[dev_id] = {k: v for k, v in data.items() if k not in ("type", "id", "name", "role")}
                hub.live[dev_id]["seen"] = time.time()
                await hub.push_config(dev_id)
                await hub.voice(dev_id, {"type": "mode", "mode": hub.show["mode"]})
                await hub.voice(dev_id, hub.voice_for(dev_id))
                if rec.get("deploy_pending") and rec.get("project"):
                    try:
                        await hub.deploy(dev_id)
                        hub.note(dev_id, "올려둔 작품 자동 설치 시작")
                    except web.HTTPException as error:
                        rec["deploy_pending"] = False
                        hub.store.save()
                        hub.note(dev_id, f"자동 설치 못 함: {error.text}")
            elif kind == "status":
                was_ready = bool(hub.live.get(dev_id, {}).get("bridge"))
                hub.live[dev_id] = {k: v for k, v in data.items() if k != "type"} | {"seen": time.time()}
                if data.get("bridge") and not was_ready:
                    await hub.voice(dev_id, hub.voice_for(dev_id))
            elif kind == "log":
                hub.add_log(dev_id, str(data.get("line", ""))[:2000])
                continue
            elif kind == "result":
                future = hub.pending.get(data.get("req"))
                if future and not future.done():
                    future.set_result(data)
                if data.get("command") not in QUIET_COMMANDS:
                    await hub.to_dashboards({"type": "result", "device": dev_id, **data})
            elif kind == "event":
                try:
                    if data.get("name") == "start_chat" and hub.show["mode"] == "chat":
                        hub.add_log(dev_id, f"{time.strftime('%H:%M:%S')} [show] 대화 중이라 start_chat 무시")
                    elif data.get("name") == "start_chat":
                        await hub.start_chat(data.get("topic"), data.get("utterance"))
                    elif data.get("name") == "stop_chat":
                        await hub.stop_chat()
                except web.HTTPException as error:
                    hub.add_log(dev_id, f"{time.strftime('%H:%M:%S')} [show] {error.text}")
            hub.push_state()
    finally:
        if hub.sockets.get(dev_id) is ws:
            del hub.sockets[dev_id]
            hub.push_state()
    return ws


# ---------- 중앙 위치 알리기 (UDP) ----------

async def beacon(hub):
    """3초마다 같은 네트워크에 '중앙 여기 있음'을 뿌림 → 중앙 IP가 바뀌어도 Pi들이 다시 찾아옴."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setblocking(False)
    try:
        while True:
            ip = lan_ip()
            if ip != "127.0.0.1":
                payload = json.dumps({"live_control": 1, "port": hub.port,
                                      "sig": beacon_sign(hub.store.token, ip, hub.port)}).encode()
                for target in {"255.255.255.255", ip.rsplit(".", 1)[0] + ".255"}:
                    try:
                        sock.sendto(payload, (target, BEACON_PORT))
                    except OSError:
                        pass
            await asyncio.sleep(3)
    finally:
        sock.close()


async def background(app):
    task = asyncio.create_task(beacon(app["hub"]))
    yield
    task.cancel()


# ---------- 시작 ----------

def create_app(public_url, port=8700):
    store = Store()
    app = web.Application(middlewares=[auth], client_max_size=200 * 1024 * 1024)
    app["store"] = store
    app["hub"] = Hub(store, public_url, port)
    app.cleanup_ctx.append(background)
    app.router.add_get("/", index)
    app.router.add_static("/static", STATIC)
    app.router.add_get("/install.sh", install_sh)
    app.router.add_get("/join", join_sh)
    app.router.add_get("/ws/pair", pair_ws)
    app.router.add_post("/api/pair/{dev_id}/{action}", api_pair)
    app.router.add_get("/agent/{name}", agent_file)
    app.router.add_get("/bundle/{dev_id}.zip", bundle)
    app.router.add_get("/api/state", api_state)
    app.router.add_get("/api/manifest", api_manifest)
    app.router.add_post("/api/settings", api_settings)
    app.router.add_post("/api/upload", api_upload)
    app.router.add_post("/api/show/start", api_show_start)
    app.router.add_post("/api/show/stop", api_show_stop)
    app.router.add_post("/api/all/{action}", api_all)
    app.router.add_post("/api/devices/{dev_id}", api_update_device)
    app.router.add_delete("/api/devices/{dev_id}", api_forget)
    app.router.add_get("/api/devices/{dev_id}/prompt", api_prompt)
    app.router.add_get("/api/devices/{dev_id}/logs", api_logs)
    app.router.add_get("/api/devices/{dev_id}/audio", api_audio)
    app.router.add_post("/api/devices/{dev_id}/audio", api_audio)
    app.router.add_post("/api/devices/{dev_id}/upload", api_device_upload)
    app.router.add_post("/api/devices/{dev_id}/{action}", api_action)  # 이건 맨 마지막 (위의 것들을 먼저 매칭)
    app.router.add_get("/ws/dashboard", dashboard_ws)
    app.router.add_get("/ws/device", device_ws)
    return app


def banner(hub):
    a = hub.addresses()
    lines = [
        "",
        f"  대시보드:      {a['dashboard']}",
    ]
    if a["dashboard_ip"]:
        lines.append(f"    └ 안 열리면: {a['dashboard_ip']}")
    lines.append(f"  새 Pi 추가:    {a['join']}   (→ 대시보드에서 '허용')")
    if a["join_ip"]:
        lines.append(f"    └ .local이 안 되는 네트워크면: {a['join_ip']}")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Live Control — 라즈베리 파이 중앙 제어 서버")
    parser.add_argument("--port", type=int, default=8700)
    parser.add_argument("--public-url", help="Pi가 접속할 주소 (기본: 이 컴퓨터의 LAN IP). 예: http://live-control.local:8700")
    parser.add_argument("--make-setup", metavar="FILE",
                        help="중앙 없이 Pi에서 바로 쓰는 설치 파일(pi-setup.sh)만 만들고 끝냄 (토큰 없음 → Pi가 연결 요청)")
    args = parser.parse_args()
    if args.make_setup:
        url = (args.public_url or f"http://live-control.local:{args.port}").rstrip("/")
        Path(args.make_setup).write_text(render_setup(url))
        Path(args.make_setup).chmod(0o755)
        print(f"{args.make_setup} 만듦 (중앙 주소 {url}) → Pi에서: bash {Path(args.make_setup).name} [작품 폴더]")
        raise SystemExit(0)
    public_url = (args.public_url or f"http://{lan_ip()}:{args.port}").rstrip("/")
    app = create_app(public_url, args.port)
    (DATA / "server.json").write_text(json.dumps({"public_url": public_url, "port": args.port}))
    print(banner(app["hub"]), flush=True)
    web.run_app(app, host="0.0.0.0", port=args.port, access_log=None, print=None)
