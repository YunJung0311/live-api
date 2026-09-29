"""Live Control: central dashboard + WebSocket hub for every Raspberry Pi on the LAN."""

import argparse
import asyncio
import io
import json
import random
import re
import secrets
import shutil
import socket
import time
import uuid
import zipfile
from collections import deque
from pathlib import Path

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
DEVICE_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
ACTIONS = {"start", "stop", "restart", "deploy", "identify", "update_agent", "reboot", "poweroff", "ping"}

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
}


# ---------- 저장소 ----------

class Store:
    def __init__(self):
        DATA.mkdir(exist_ok=True)
        UPLOADS.mkdir(exist_ok=True)
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


# ---------- 프로젝트 (repo 폴더 + 업로드한 zip) ----------

def list_projects():
    projects = []
    for folder in sorted(REPO.iterdir(), key=lambda p: p.name.lower()):
        if folder.is_dir() and folder.name not in SKIP_DIRS and not folder.name.startswith("."):
            projects.append({"id": f"repo:{folder.name}", "label": folder.name})
    for folder in sorted((SERVER / "examples").glob("*/")):
        projects.append({"id": f"example:{folder.name}", "label": f"예제 · {folder.name}"})
    for folder in sorted(UPLOADS.glob("*/")):
        projects.append({"id": f"upload:{folder.name}", "label": f"업로드 · {folder.name}"})
    return projects


def project_path(project_id):
    kind, _, name = (project_id or "").partition(":")
    base = {"repo": REPO, "example": SERVER / "examples", "upload": UPLOADS}.get(kind)
    if not base or not name or "/" in name or name.startswith("."):
        raise web.HTTPBadRequest(text="프로젝트를 먼저 선택하세요")
    path = base / name
    if not path.is_dir():
        raise web.HTTPNotFound(text="프로젝트 폴더가 없음")
    return path


def guess_manifest(folder):
    """live.json이 없으면 폴더 모양을 보고 실행 방법을 추측한다."""
    live = folder / "live.json"
    if live.exists():
        return json.loads(live.read_text())
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
    if "key.example.js" in names:
        manifest["files"]["key.js"] = 'window.GEMINI_KEY = "${GEMINI_API_KEY}";\n'
    if "config.example.js" in names:
        manifest["files"]["config.local.js"] = 'window.LOCAL_CONFIG = { apiKey: "${GEMINI_API_KEY}" };\n'
    return manifest


def build_bundle(folder):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(folder.rglob("*")):
            rel = path.relative_to(folder)
            if any(part in SKIP_DIRS for part in rel.parts) or path.name in SKIP_FILES:
                continue
            if path.is_file():
                archive.write(path, rel.as_posix())
    return buffer.getvalue()


def import_zip(name, data):
    """업로드한 zip을 풀어서 폴더 하나로 정리 (zip 안 최상위 폴더 하나면 벗겨냄)."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", name):
        raise web.HTTPBadRequest(text="이름은 영어/숫자/-/_ 만")
    target = UPLOADS / name
    staging = UPLOADS / f".{name}.tmp"
    shutil.rmtree(staging, ignore_errors=True)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        archive.extractall(staging)
    shutil.rmtree(staging / "__MACOSX", ignore_errors=True)
    entries = [p for p in staging.iterdir() if p.name != ".DS_Store"]
    root = entries[0] if len(entries) == 1 and entries[0].is_dir() else staging
    shutil.rmtree(target, ignore_errors=True)
    shutil.move(str(root), target)
    shutil.rmtree(staging, ignore_errors=True)


# ---------- 기기 연결 허브 ----------

def mask(value):
    return f"…{value[-4:]}" if value else ""


class Hub:
    def __init__(self, store, public_url):
        self.store = store
        self.public_url = public_url
        self.sockets = {}
        self.live = {}
        self.logs = {}
        self.pending = {}
        self.dashboards = set()
        self.show = {"mode": "idle", "topic": None, "target": None, "started": None}
        self.show_tasks = []
        self.push_scheduled = False

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
                "online": dev_id in self.sockets,
                "status": live,
            })
        settings = dict(self.store.settings, default_key=mask(self.store.settings["default_key"]))
        return {
            "devices": devices,
            "projects": list_projects(),
            "settings": settings,
            "show": self.show,
            "install": f'curl -fsSL "{self.public_url}/install.sh?t={self.store.token}" | bash',
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

    # --- 기기로 보내기 ---

    async def send(self, dev_id, msg):
        ws = self.sockets.get(dev_id)
        if not ws:
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
        return {"type": "config", "name": rec["name"], "role": rec["role"], "env": env, "prompt": rec["prompt"]}

    async def push_config(self, dev_id):
        await self.send(dev_id, self.device_config(dev_id))

    def deploy_message(self, dev_id):
        rec = self.store.devices[dev_id]
        folder = project_path(rec["project"])
        manifest = rec["manifest"] or guess_manifest(folder)
        return {"type": "deploy", "project": rec["project"], "path": f"/bundle/{dev_id}.zip", "manifest": manifest}

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
        for dev_id in self.sockets:
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
    if not secrets.compare_digest(token_of(request), store.token):
        if request.path == "/":
            return web.Response(
                text="토큰이 필요함. 중앙 컴퓨터 터미널에 출력된 주소(…/?t=토큰)로 들어오세요.",
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


async def install_sh(request):
    hub = request.app["hub"]
    script = (SERVER / "agent" / "install.sh").read_text()
    script = script.replace("__CONTROLLER__", hub.public_url).replace("__TOKEN__", hub.store.token)
    return web.Response(text=script, content_type="text/x-shellscript")


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
    data = await asyncio.to_thread(build_bundle, project_path(rec["project"]))
    return web.Response(body=data, content_type="application/zip")


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
    msg = hub.deploy_message(dev_id) if action == "deploy" else {"type": action}
    await hub.command(dev_id, msg)
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
    for name, low, high in (("others_delay", 0, 30), ("max_seconds", 10, 3600)):
        if name in body:
            settings[name] = min(max(float(body[name]), low), high)
    hub.store.save()
    if "default_key" in body:
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
    reader = await request.multipart()
    name, data = None, None
    async for part in reader:
        if part.name == "name":
            name = (await part.text()).strip()
        elif part.name == "file":
            data = await part.read()
    if not name or not data:
        raise web.HTTPBadRequest(text="이름과 zip 파일 필요")
    try:
        await asyncio.to_thread(import_zip, name, data)
    except zipfile.BadZipFile:
        raise web.HTTPBadRequest(text="zip 파일이 아님")
    request.app["hub"].push_state()
    return web.json_response({"ok": True, "project": f"upload:{name}"})


# ---------- WebSocket ----------

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
    if old:
        await old.close()
    rec = hub.store.device(dev_id)
    hub.sockets[dev_id] = ws
    try:
        async for message in ws:
            if message.type != WSMsgType.TEXT:
                continue
            data = json.loads(message.data)
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


# ---------- 시작 ----------

def lan_ip():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def create_app(public_url):
    store = Store()
    app = web.Application(middlewares=[auth], client_max_size=200 * 1024 * 1024)
    app["store"] = store
    app["hub"] = Hub(store, public_url)
    app.router.add_get("/", index)
    app.router.add_static("/static", STATIC)
    app.router.add_get("/install.sh", install_sh)
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
    app.router.add_post("/api/devices/{dev_id}/{action}", api_action)
    app.router.add_get("/ws/dashboard", dashboard_ws)
    app.router.add_get("/ws/device", device_ws)
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Live Control — 라즈베리 파이 중앙 제어 서버")
    parser.add_argument("--port", type=int, default=8700)
    parser.add_argument("--public-url", help="Pi가 접속할 주소 (기본: 이 컴퓨터의 LAN IP)")
    args = parser.parse_args()
    public_url = (args.public_url or f"http://{lan_ip()}:{args.port}").rstrip("/")
    app = create_app(public_url)
    token = app["store"].token
    print(f"\n  대시보드:  {public_url}/?t={token}")
    print(f"  Pi 설치:   curl -fsSL \"{public_url}/install.sh?t={token}\" | bash\n")
    web.run_app(app, host="0.0.0.0", port=args.port, access_log=None, print=None)
