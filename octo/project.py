"""Project packaging rules shared by the controller and installed Pi endpoint."""

import json
import re
import shlex
import shutil
import unicodedata
import zipfile
from pathlib import PurePosixPath

SKIP_DIRS = {'.git', '.venv', 'venv', '__pycache__', 'node_modules', '__MACOSX', 'octo', 'local-server'}
SKIP_FILES = {'.DS_Store', 'Thumbs.db', 'desktop.ini', 'key.js', 'config.local.js'}
LIMIT = 500 * 1024 * 1024

# Hooks execute in the student's own script scope, only in the deployment copy.
STUDENTS = {
    'minseo': ('app.js', 'connect', 'disconnect(); teardown()', 'sessionActive', 'ws', 'isSpeaking()', 'sysPrompt'),
    'sunny': ('app.js', 'connect', 'disconnect(); cleanup()', 'ready', 'ws', 'isSpeaking()', 'SYSTEM_INSTRUCTION'),
    'Eunseol': ('app.js', 'connect', 'disconnect(); cleanup()', 'ready', 'ws', 'isSpeaking()', 'SYSTEM_INSTRUCTION'),
    'HuhGaeun': ('index.html', 'startSession', 'stopSession("octo")', 'sessionReady', 'ws', 'isAgentSpeaking()', 'document.getElementById("systemPrompt").value'),
    'ParkSoyeon': ('index.html', 'connectSafely', 'stopSession(); cleanupAudio()', 'sessionActive', 'ws', 'currentSources.length > 0', 'SYSTEM_INSTRUCTION'),
    'seoyoungchae': ('index.html', 'startSession', 'stopSession()', 'isReady', 'ws', 'isSpeaking()', 'buildPersonaPrompt()'),
    'Yunjung': ('app.js', 'start', 'if (current) finish(current)', 'current?.ready', 'current?.socket', 'current?.player?.speaking', '(await (await fetch("persona.txt")).text())'),
}


def safe_parts(raw):
    name = unicodedata.normalize('NFC', str(raw).replace('\\', '/'))
    parts = PurePosixPath(name).parts
    if name.startswith('/') or '..' in parts or re.match(r'^[A-Za-z]:', name):
        raise ValueError(f'폴더 밖 경로: {raw}')
    if not parts or any(p in SKIP_DIRS for p in parts) or parts[-1] in SKIP_FILES or parts[-1].startswith(('._', '.env')):
        return None
    return parts


def extract_zip(src, dest):
    with zipfile.ZipFile(src) as archive:
        infos = archive.infolist()
        if len(infos) > 10000 or sum(i.file_size for i in infos) > LIMIT:
            raise ValueError('압축을 푼 작품은 500MB / 10,000파일까지')
        for info in infos:
            name = info.filename
            if not info.flag_bits & 0x800:
                for encoding in ('utf-8', 'cp949'):
                    try:
                        name = info.filename.encode('cp437').decode(encoding)
                        break
                    except UnicodeError:
                        pass
            parts = safe_parts(name)
            if not parts or info.is_dir():
                continue
            if info.external_attr >> 16 & 0o170000 == 0o120000:
                raise ValueError('작품 zip에 심볼릭 링크를 넣을 수 없음')
            target = dest.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, open(target, 'wb') as out:
                shutil.copyfileobj(source, out)


def validate_manifest(manifest):
    if not isinstance(manifest, dict):
        raise ValueError('live.json은 JSON 객체여야 함')
    if not isinstance(manifest.get('run', []), list) or any(not isinstance(c, str) for c in manifest.get('run', [])):
        raise ValueError('run은 명령 문자열의 배열')
    if not isinstance(manifest.get('setup', ''), str):
        raise ValueError('setup은 명령 문자열')
    browser = manifest.get('browser')
    if browser is not None and (not isinstance(browser, dict) or not isinstance(browser.get('url'), str)):
        raise ValueError('browser.url이 필요함')
    files = manifest.get('files') or {}
    if not isinstance(files, dict) or any(not isinstance(v, str) for v in files.values()):
        raise ValueError('files는 경로와 템플릿 문자열의 객체')
    for rel in [*files, *([manifest['prompt_file']] if manifest.get('prompt_file') else [])]:
        name = str(rel).replace('\\', '/')
        if not name or name.startswith('/') or '..' in PurePosixPath(name).parts or re.match(r'^[A-Za-z]:', name):
            raise ValueError(f'잘못된 manifest 경로: {rel}')
    return manifest


def guess_manifest(folder):
    live = folder / 'live.json'
    if live.exists():
        return validate_manifest(json.loads(live.read_text()))
    names = {p.name for p in folder.iterdir()}
    manifest = {'name': folder.name, 'setup': '', 'run': [], 'browser': None, 'files': {},
                'prompt_file': next((n for n in ('persona.txt', 'prompt.txt') if n in names), None)}
    if 'server.py' in names:
        python = 'python3'
        if 'requirements.txt' in names:
            manifest['setup'] = 'python3 -m venv .venv && .venv/bin/pip install -r requirements.txt'
            python = '.venv/bin/python'
        manifest['run'] = [f'{python} server.py --port 8000']
        manifest['browser'] = {'url': 'http://localhost:8000/'}
        manifest['files']['.env'] = 'GEMINI_API_KEY=${GEMINI_API_KEY}\n'
    elif (scripts := sorted(n for n in names if n.endswith('.sh'))):
        manifest['run'] = [f'bash {shlex.quote(scripts[0])}']
    elif (htmls := sorted(n for n in names if n.endswith('.html'))):
        from urllib.parse import quote
        page = '' if 'index.html' in names else quote(htmls[0])
        manifest['run'] = ['python3 -m http.server 8080 --bind 127.0.0.1']
        manifest['browser'] = {'url': f'http://localhost:8080/{page}'}
    if names & {'key.example.js', 'key.js'}:
        manifest['files']['key.js'] = 'window.GEMINI_KEY = "${GEMINI_API_KEY}";\n'
    if names & {'config.example.js', 'config.local.js'}:
        manifest['files']['config.local.js'] = 'window.LOCAL_CONFIG = { apiKey: "${GEMINI_API_KEY}" };\n'
    if folder.name in STUDENTS:
        manifest['student'] = folder.name
    return manifest


def deployment_files(folder, student, bridge_dir):
    """Return replacements for the archive; never write to the source project."""
    if student not in STUDENTS:
        return {}
    file, start, stop, ready, socket, speaking, prompt = STUDENTS[student]
    code = (folder / file).read_text()
    hook = f'''\n// Octo deployment hook (student source is unchanged).
OctoStudent.connect({{
  start: async () => {{
    const key = window.OCTO_CONFIG.key;
    for (const id of ["apiKeyInput", "apiKey", "api-key"]) {{
      const input = document.getElementById(id); if (input) input.value = key;
    }}
    {"if (key) localStorage.setItem(LS_KEY, key); params.greet = false;" if student == "minseo" else ""}
    {"document.querySelector('input[name=mode][value=voice]').checked = true;" if student == "Yunjung" else ""}
    await {start}();
  }},
  stop: async () => {{ {stop}; }},
  ready: () => !!({ready}), socket: () => {socket}, speaking: () => !!({speaking}),
  prompt: async () => {prompt}, relay: {str(student == 'Yunjung').lower()},
}});\n'''
    # Soyeon's original automatic connection would activate a slave microphone in idle.
    code = code.replace('if (localApiKey) connectSafely(true);', '')
    if file.endswith('.html'):
        index = code.rfind('</script>')
        if index < 0:
            raise ValueError(f'{student}: 연결 코드를 넣을 script가 없음')
        code = code[:index] + hook + code[index:]
        html = code
    else:
        code += hook
        html = (folder / 'index.html').read_text()
    scripts = '<script src="octo-config.js"></script><script src="live-bridge.js"></script><script src="octo-student.js"></script>'
    html = html.replace('</head>', scripts + '</head>', 1)
    changed = {file: code, 'index.html': html,
               'live-bridge.js': (bridge_dir / 'live-bridge.js').read_text(),
               'octo-student.js': (bridge_dir / 'octo-student.js').read_text()}
    if student == 'Yunjung':
        server = (folder / 'server.py').read_text()
        server = server.replace("ASSETS = {", "ASSETS = {'/octo-config.js': 'octo-config.js', '/live-bridge.js': 'live-bridge.js', '/octo-student.js': 'octo-student.js', ", 1)
        server = server.replace("connect-src 'self';", "connect-src 'self' ws://127.0.0.1:8765;", 1)
        anchor = "                            if not isinstance(data, dict) or data.get('type') != 'text':"
        if anchor not in server:
            raise ValueError('Yunjung: relay의 프로토콜이 바뀜; adapter를 확인하세요')
        server = server.replace(anchor, "                            if data == {'type': 'audioStreamEnd'}:\n                                await upstream.send_json({'realtimeInput': {'audioStreamEnd': True}})\n                                continue\n" + anchor, 1)
        changed['server.py'] = server
    return changed
