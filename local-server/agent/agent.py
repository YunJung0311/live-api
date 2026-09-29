"""Live Agent: runs on every Raspberry Pi.

- Keeps one WebSocket open to the controller (reconnects forever).
- Downloads, installs and keeps one student project alive.
- Serves ws://127.0.0.1:8765/bridge so the project page can be told to wake/sleep.
"""

import asyncio
import json
import os
import shutil
import signal
import socket
import string
import subprocess
import sys
import time
import zipfile
from collections import deque
from pathlib import Path
from urllib.parse import urlparse

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web

VERSION = "0.1.0"
HOME = Path(__file__).resolve().parent
CONFIG = HOME / "config.json"
STATE = HOME / "state.json"
PROJECT = HOME / "project"
SETUP_MARK = HOME / "setup.done"
BRIDGE_JS = HOME / "live-bridge.js"
BRIDGE_PORT = 8765
CHROMIUM_FLAGS = [
    "--kiosk",
    "--noerrdialogs",
    "--disable-infobars",
    "--disable-session-crashed-bubble",
    "--disable-features=Translate",
    "--check-for-update-interval=31536000",
    "--password-store=basic",
    "--use-fake-ui-for-media-stream",
    "--autoplay-policy=no-user-gesture-required",
]


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


def device_id(config):
    if config.get("id"):
        return config["id"]
    try:
        return Path("/etc/machine-id").read_text().strip()[:10]
    except OSError:
        return socket.gethostname()


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


class Agent:
    def __init__(self):
        self.config = load(CONFIG, {})
        self.id = device_id(self.config)
        self.state = load(STATE, {})
        self.state.setdefault("env", {})
        self.state.setdefault("role", "agent")
        self.state.setdefault("name", socket.gethostname())
        self.state.setdefault("prompt", None)
        self.state.setdefault("manifest", None)
        self.state.setdefault("project", None)
        self.state.setdefault("running", False)
        self.phase = "stopped" if self.state["manifest"] else "empty"
        self.want_running = False
        self.procs = set()
        self.keepers = []
        self.bridges = set()
        self.voice = "sleep"
        self.last_voice = {"type": "wake" if self.state["role"] == "master" else "sleep"}
        self.logs = deque(maxlen=300)
        self.outbox = asyncio.Queue(maxsize=1000)
        self.project_lock = asyncio.Lock()
        self.started_at = time.time()

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
        }

    def local_ip(self):
        host = urlparse(self.config.get("controller", "")).hostname or "8.8.8.8"
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect((host, 9))
                return s.getsockname()[0]
        except OSError:
            return None

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
        env.setdefault("DISPLAY", ":0")
        env.update({k: str(v) for k, v in self.state["env"].items()})
        env.update(self.template_values())
        return env

    def template_values(self):
        return {
            "PROJECT_DIR": str(PROJECT),
            "DEVICE_NAME": self.state["name"],
            "DEVICE_ROLE": self.state["role"],
            "LIVE_BRIDGE": f"ws://127.0.0.1:{BRIDGE_PORT}/bridge",
        }

    def render_files(self):
        manifest = self.state["manifest"] or {}
        values = {**self.state["env"], **self.template_values()}
        for rel, template in (manifest.get("files") or {}).items():
            path = inside_project(rel)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(string.Template(template).safe_substitute(values))
            path.chmod(0o600)
        if self.state["prompt"] is not None and manifest.get("prompt_file"):
            inside_project(manifest["prompt_file"]).write_text(self.state["prompt"])

    async def run_setup(self):
        manifest = self.state["manifest"]
        command = (manifest.get("setup") or "").strip()
        mark = json.dumps([self.state["project"], command])
        if not command or (SETUP_MARK.exists() and SETUP_MARK.read_text() == mark):
            return
        self.set_phase("installing")
        self.log(f"설치 시작: {command}")
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", command, cwd=PROJECT, env=self.env(),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        await self.pump(proc, "setup")
        if await proc.wait() != 0:
            raise RuntimeError("설치(setup) 명령이 실패함. 로그를 확인하세요.")
        SETUP_MARK.write_text(mark)
        self.log("설치 완료")

    async def pump(self, proc, label):
        async for raw in proc.stdout:
            self.log(f"[{label}] {raw.decode(errors='replace')}")

    async def spawn_command(self, command, label):
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", command, cwd=PROJECT, env=self.env(),
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
        args = [exe, *CHROMIUM_FLAGS, f"--user-data-dir={HOME / 'chromium'}", *browser.get("args", []), url]
        chromium_log = open(HOME / "chromium.log", "ab")
        return await asyncio.create_subprocess_exec(
            *args, cwd=PROJECT, env=self.env(), stdout=chromium_log, stderr=chromium_log,
            start_new_session=True,
        )

    async def wait_for_display(self):
        runtime = Path(self.env()["XDG_RUNTIME_DIR"])
        for _ in range(90):
            if list(runtime.glob("wayland-*")) or Path("/tmp/.X11-unix/X0").exists():
                return
            await asyncio.sleep(1)
        self.log("화면(데스크톱)을 못 찾음. 데스크톱 자동 로그인이 켜져 있는지 확인하세요.")

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
            code = await proc.wait()
            self.procs.discard(proc)
            if not self.want_running:
                return
            self.log(f"{label} 꺼짐 (code {code}) → 자동 재시작")
            await asyncio.sleep(2 if time.monotonic() - started > 30 else 5)

    async def start(self):
        manifest = self.state["manifest"]
        if not manifest or not PROJECT.exists():
            raise RuntimeError("설치된 프로젝트가 없음. 먼저 Deploy 하세요.")
        if self.want_running:
            return
        self.render_files()
        await self.run_setup()
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

    async def stop(self, remember=True):
        self.want_running = False
        if remember:
            self.state["running"] = False
            save(STATE, self.state)
        for proc in list(self.procs):
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        for proc in list(self.procs):
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except asyncio.TimeoutError:
                os.killpg(proc.pid, signal.SIGKILL)
        for task in self.keepers:
            task.cancel()
        self.keepers = []
        self.procs.clear()
        self.voice = "sleep"
        self.set_phase("stopped" if self.state["manifest"] else "empty")

    async def deploy(self, msg):
        await self.stop()
        self.set_phase("installing")
        url = self.config["controller"] + msg["path"]
        bundle = HOME / "bundle.zip"
        self.log(f"다운로드: {msg['project']}")
        async with ClientSession(timeout=ClientTimeout(total=600)) as http:
            async with http.get(url, headers={"X-Token": self.config["token"]}) as response:
                response.raise_for_status()
                bundle.write_bytes(await response.read())
        staging = HOME / "project.new"
        shutil.rmtree(staging, ignore_errors=True)
        with zipfile.ZipFile(bundle) as archive:
            archive.extractall(staging)
        bundle.unlink()
        for script in list(staging.rglob("*.sh")) + list(staging.rglob("*.command")):
            script.chmod(0o755)
        if (staging / "live-bridge.js").exists() and BRIDGE_JS.exists():
            shutil.copy(BRIDGE_JS, staging / "live-bridge.js")
        shutil.rmtree(PROJECT, ignore_errors=True)
        staging.rename(PROJECT)
        SETUP_MARK.unlink(missing_ok=True)
        self.state.update(project=msg["project"], manifest=msg["manifest"])
        save(STATE, self.state)
        self.log("파일 설치 완료")
        await self.start()

    # ---------- 중앙에서 온 명령 ----------

    async def handle(self, msg):
        kind = msg.get("type")
        data = None
        try:
            if kind == "config":
                changed = any(self.state.get(k) != msg.get(k) for k in ("env", "prompt", "role"))
                for key in ("name", "role", "env", "prompt"):
                    self.state[key] = msg.get(key, self.state[key])
                save(STATE, self.state)
                if changed and PROJECT.exists() and self.state["manifest"]:
                    self.render_files()
                await self.to_bridges({"type": "hello", **self.bridge_hello()})
                return
            if kind == "voice":
                await self.to_bridges(msg["msg"])
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
                    await self.stop()
            elif kind == "restart":
                async with self.project_lock:
                    await self.stop()
                    await self.start()
            elif kind == "get_prompt":
                data = self.read_prompt()
            elif kind == "get_logs":
                data = list(self.logs)
            elif kind == "identify":
                await self.to_bridges({"type": "identify", "name": self.state["name"]})
                if not self.bridges:
                    raise RuntimeError("페이지가 bridge에 연결되어 있지 않아서 화면 표시 불가")
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
        manifest = self.state["manifest"] or {}
        if not manifest.get("prompt_file"):
            raise RuntimeError("manifest에 prompt_file이 없음")
        path = inside_project(manifest["prompt_file"])
        return path.read_text() if path.exists() else ""

    async def update_self(self):
        async with ClientSession(timeout=ClientTimeout(total=60)) as http:
            for name in ("agent.py", "live-bridge.js"):
                async with http.get(f"{self.config['controller']}/agent/{name}",
                                    headers={"X-Token": self.config["token"]}) as response:
                    response.raise_for_status()
                    (HOME / name).write_bytes(await response.read())
        self.log("agent 업데이트 완료 → 재시작")
        self.send({"type": "result", "command": "update_agent", "ok": True})
        await asyncio.sleep(1)
        os._exit(0)  # systemd(Restart=always)가 새 코드로 다시 켜줌

    # ---------- 중앙 연결 ----------

    async def controller_loop(self):
        base = self.config["controller"].replace("http", "ws", 1)
        url = f"{base}/ws/device?id={self.id}&t={self.config['token']}"
        delay = 1
        while True:
            try:
                async with ClientSession() as http:
                    async with http.ws_connect(url, heartbeat=15) as ws:
                        delay = 1
                        self.log(f"중앙 연결됨: {self.config['controller']}")
                        await ws.send_json({"type": "hello", "id": self.id, "name": self.state["name"],
                                            "role": self.state["role"], **self.status()})
                        writer = asyncio.create_task(self.drain(ws))
                        try:
                            async for message in ws:
                                if message.type == WSMsgType.TEXT:
                                    asyncio.create_task(self.handle(json.loads(message.data)))
                        finally:
                            writer.cancel()
            except Exception as error:
                print(f"controller connection failed: {error}", flush=True)
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

    # ---------- 페이지(bridge) 연결 ----------

    def bridge_hello(self):
        return {"id": self.id, "name": self.state["name"], "role": self.state["role"]}

    async def to_bridges(self, msg):
        if msg.get("type") in ("wake", "sleep"):
            self.last_voice = msg
        elif msg.get("type") == "kickoff":
            self.last_voice = {"type": "wake"}
        for ws in list(self.bridges):
            try:
                await ws.send_json(msg)
            except ConnectionError:
                self.bridges.discard(ws)

    async def bridge_ws(self, request):
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        self.bridges.add(ws)
        self.log("페이지가 bridge에 연결됨")
        await ws.send_json({"type": "hello", **self.bridge_hello()})
        await ws.send_json(self.last_voice)
        self.send(self.status())
        try:
            async for message in ws:
                if message.type != WSMsgType.TEXT:
                    continue
                data = json.loads(message.data)
                kind = data.get("type")
                if kind == "voice_state":
                    self.voice = data.get("state", "sleep")
                    self.send(self.status())
                elif kind == "log":
                    self.log(f"[page] {str(data.get('text', ''))[:500]}")
                elif kind == "event":
                    self.send({"type": "event", **data})
        finally:
            self.bridges.discard(ws)
            self.voice = "sleep"
            self.send(self.status())
        return ws

    async def bridge_js(self, _request):
        return web.FileResponse(BRIDGE_JS, headers={"Cache-Control": "no-store"})

    async def serve_bridge(self):
        app = web.Application()
        app.router.add_get("/bridge", self.bridge_ws)
        app.router.add_get("/live-bridge.js", self.bridge_js)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", BRIDGE_PORT).start()

    async def main(self):
        if not self.config.get("controller"):
            sys.exit("config.json에 controller 주소가 없음. install.sh를 다시 실행하세요.")
        self.log(f"Live Agent {VERSION} 시작 (id {self.id})")
        await self.serve_bridge()
        if self.state["running"] and self.state["manifest"]:
            asyncio.create_task(self.handle({"type": "start"}))
        await asyncio.gather(self.controller_loop(), self.status_loop())


if __name__ == "__main__":
    asyncio.run(Agent().main())
