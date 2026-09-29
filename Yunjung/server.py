"""Local-only HTTP/WebSocket relay. Never expose the repository as static files."""

import argparse
import asyncio
import base64
import json
import ssl

import certifi
from pathlib import Path
from urllib.parse import urlencode

from aiohttp import ClientSession, ClientTimeout, ClientConnectorCertificateError, WSMsgType, web
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / '.env'
MODEL = 'gemini-3.1-flash-live-preview'
ENDPOINT = ('wss://generativelanguage.googleapis.com/ws/'
            'google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent')
ASSETS = {'/': 'index.html', '/style.css': 'style.css', '/app.js': 'app.js',
          '/audio.js': 'audio.js', '/pcm-worklet.js': 'pcm-worklet.js',
          '/character.js': 'character.js', '/assets/beat-mascot.png': 'assets/beat-mascot.png'}


def read_key():
    # Reload on each connection so saving .env does not require a server restart.
    values = dotenv_values(ENV_PATH)
    for name in ('GEMINI_API_KEY', 'GOOGLE_API_KEY', 'API_KEY'):
        value = values.get(name)
        if value and value.strip():
            return value.strip()
    return ''


def setup_message():
    return {'setup': {
        'model': f'models/{MODEL}',
        'generationConfig': {'responseModalities': ['AUDIO']},
        'systemInstruction': {'parts': [{'text': (ROOT / 'persona.txt').read_text()}]},
        'inputAudioTranscription': {},
        'outputAudioTranscription': {},
        'realtimeInputConfig': {
            'automaticActivityDetection': {'disabled': False},
            'activityHandling': 'START_OF_ACTIVITY_INTERRUPTS',
        },
    }}


@web.middleware
async def local_only(request, handler):
    # Reject foreign Host/Origin headers, including cross-site WebSocket requests.
    if request.url.host not in ('localhost', '127.0.0.1'):
        raise web.HTTPForbidden()
    origin = request.headers.get('Origin')
    if origin and origin != f'{request.scheme}://{request.host}':
        raise web.HTTPForbidden()
    return await handler(request)


async def asset(request):
    response = web.FileResponse(ROOT / ASSETS[request.path])
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; object-src 'none'; frame-ancestors 'none'"
    )
    return response


async def health(request):
    return web.json_response({'configured': bool(read_key()), 'model': MODEL},
                             headers={'Cache-Control': 'no-store'})


async def relay(request):
    browser = web.WebSocketResponse(max_msg_size=16384, heartbeat=20)
    await browser.prepare(request)
    tasks = []

    async def error(message):
        if not browser.closed:
            await browser.send_json({'type': 'error', 'message': message})

    try:
        initial_request = await asyncio.wait_for(browser.receive(), timeout=15)
        if initial_request.type != WSMsgType.TEXT:
            raise ValueError('start message required')
        start = json.loads(initial_request.data)
        if not isinstance(start, dict) or start.get('type') != 'start':
            raise ValueError('invalid start message')
        supplied_key = start.get('apiKey', '')
        if not isinstance(supplied_key, str) or len(supplied_key) > 512:
            raise ValueError('invalid key')
        key = supplied_key.strip() or read_key()
        if not key:
            await error('화면에 API 키를 입력하거나 Yunjung/.env에 GEMINI_API_KEY를 저장해 주세요.')
            return browser
        # Only the server uses this URL. Never log upstream exceptions/close reasons.
        url = ENDPOINT + '?' + urlencode({'key': key})
        async with ClientSession(timeout=ClientTimeout(total=None, sock_connect=15)) as client:
            async with client.ws_connect(url, ssl=ssl.create_default_context(cafile=certifi.where()), max_msg_size=4 * 1024 * 1024) as upstream:
                await upstream.send_json(setup_message())
                initial = await asyncio.wait_for(upstream.receive(), timeout=20)
                if initial.type not in (WSMsgType.TEXT, WSMsgType.BINARY):
                    raise ValueError('setup closed')
                if 'setupComplete' not in json.loads(initial.data):
                    raise ValueError('setup rejected')
                await browser.send_json({'type': 'ready', 'model': MODEL})

                async def upload():
                    async for message in browser:
                        if message.type == WSMsgType.BINARY:
                            # Browser sends mono little-endian PCM16 in 32 ms chunks.
                            if not message.data or len(message.data) % 2:
                                raise ValueError('invalid PCM')
                            await upstream.send_json({'realtimeInput': {'audio': {
                                'data': base64.b64encode(message.data).decode('ascii'),
                                'mimeType': 'audio/pcm;rate=16000',
                            }}})
                        elif message.type == WSMsgType.TEXT:
                            data = json.loads(message.data)
                            if not isinstance(data, dict) or data.get('type') != 'text':
                                raise ValueError('invalid text message')
                            text = data.get('text')
                            if not isinstance(text, str) or not text.strip() or len(text) > 2000:
                                raise ValueError('invalid text length')
                            await upstream.send_json({'realtimeInput': {'text': text.strip()}})

                async def download():
                    async for message in upstream:
                        if message.type not in (WSMsgType.TEXT, WSMsgType.BINARY):
                            continue
                        payload = json.loads(message.data)
                        if 'error' in payload:
                            raise ValueError('upstream error')
                        content = payload.get('serverContent')
                        if content is not None:
                            await browser.send_json({'type': 'content', 'content': content})
                        if 'goAway' in payload:
                            await browser.send_json({'type': 'notice', 'message':
                                '세션이 곧 만료됩니다. 연결이 종료되면 다시 시작해 주세요.'})
                    await error('Gemini 연결이 종료되었습니다. 키, 모델 접근 권한, 할당량을 확인하고 다시 시작해 주세요.')

                tasks = [asyncio.create_task(upload()), asyncio.create_task(download())]
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
    except ClientConnectorCertificateError:
        await error('SSL 인증서 검증에 실패했습니다. Python 인증서 설정 또는 네트워크의 인증서를 확인해 주세요.')
    except Exception:
        # Upstream exceptions can contain the API key in their URL.
        await error('Gemini 연결에 실패했습니다. 인터넷 연결, .env의 키, 모델 접근 권한과 할당량을 확인해 주세요.')
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await browser.close()
    return browser


def create_app():
    app = web.Application(middlewares=[local_only])
    for path in ASSETS:
        app.router.add_get(path, asset)
    app.router.add_get('/api/health', health)
    app.router.add_get('/ws', relay)
    return app


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='비트 로컬 음성 에이전트')
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    web.run_app(create_app(), host='127.0.0.1', port=args.port, access_log=None)
