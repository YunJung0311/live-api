"""Local mock integration tests; no Google connection or real API key is used."""

import asyncio
import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

import server


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = TestClient(TestServer(server.create_app()))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_only_public_assets_are_served(self):
        for path in server.ASSETS:
            response = await self.client.get(path)
            self.assertEqual(response.status, 200, path)
            self.assertIn("frame-ancestors 'none'", response.headers['Content-Security-Policy'])
            await response.read()
        for path in ('/.env', '/server.py', '/persona.txt', '/.venv/pyvenv.cfg', '/README.md', '/../.env', '/assets/beat-mascot-prompt.txt', '/character.test.mjs'):
            response = await self.client.get(path)
            self.assertEqual(response.status, 404, path)

    async def test_foreign_origins_and_hosts_are_rejected(self):
        for path in ('/', '/ws', '/api/health'):
            response = await self.client.get(path, headers={'Origin': 'https://example.com'})
            self.assertEqual(response.status, 403)
        response = await self.client.get('/', headers={'Host': 'attacker.example'})
        self.assertEqual(response.status, 403)

    async def test_missing_key_health_and_socket(self):
        with patch.object(server, 'read_key', return_value=''):
            response = await self.client.get('/api/health')
            self.assertEqual(await response.json(), {'configured': False, 'model': server.MODEL})
            async with self.client.ws_connect('/ws') as socket:
                await socket.send_json({'type': 'start'})
                result = await socket.receive_json()
                self.assertEqual(result['type'], 'error')
                self.assertIn('.env', result['message'])
                self.assertEqual((await socket.receive()).type, WSMsgType.CLOSE)

    async def test_env_reloads_and_supports_aliases(self):
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / '.env'
            with patch.object(server, 'ENV_PATH', env):
                self.assertEqual(server.read_key(), '')
                env.write_text('GOOGLE_API_KEY="test-first"\n')
                self.assertEqual(server.read_key(), 'test-first')
                env.write_text('GEMINI_API_KEY=test-second\n')
                self.assertEqual(server.read_key(), 'test-second')

    async def test_audio_setup_transcripts_interrupt_and_disconnect(self):
        received = []
        closed = asyncio.Event()
        content = {
            'modelTurn': {'parts': [{'inlineData': {'data': 'AAA=', 'mimeType': 'audio/pcm;rate=24000'}}]},
            'inputTranscription': {'text': '안녕'},
            'outputTranscription': {'text': '안녕하세요'},
        }

        async def mock_google(request):
            self.assertEqual(request.query['key'], 'fake-secret')
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            received.append(await ws.receive_json())
            await ws.send_json({'setupComplete': {}})
            received.append(await ws.receive_json())
            received.append(await ws.receive_json())
            await ws.send_json({'serverContent': content})
            await ws.send_bytes(b'{"serverContent":{"interrupted":true}}')
            await ws.receive()
            closed.set()
            return ws

        mock_app = web.Application()
        mock_app.router.add_get('/live', mock_google)
        async with TestServer(mock_app) as upstream:
            with patch.object(server, 'ENDPOINT', str(upstream.make_url('/live'))), patch.object(server, 'read_key', return_value='env-fallback-key'):
                async with self.client.ws_connect('/ws') as socket:
                    await socket.send_json({'type': 'start', 'apiKey': 'fake-secret'})
                    self.assertEqual((await socket.receive_json())['type'], 'ready')
                    await socket.send_bytes(b'\x00\x00\xff\x7f')
                    await socket.send_json({'type': 'text', 'text': '안녕, 비트'})
                    self.assertEqual((await socket.receive_json())['content'], content)
                    self.assertTrue((await socket.receive_json())['content']['interrupted'])
                await asyncio.wait_for(closed.wait(), 3)
        self.assertEqual(received[2], {'realtimeInput': {'text': '안녕, 비트'}})
        setup = received[0]['setup']
        self.assertEqual(setup['model'], 'models/gemini-3.1-flash-live-preview')
        self.assertEqual(setup['generationConfig']['responseModalities'], ['AUDIO'])
        audio = received[1]['realtimeInput']['audio']
        self.assertEqual(audio['mimeType'], 'audio/pcm;rate=16000')
        self.assertEqual(base64.b64decode(audio['data']), b'\x00\x00\xff\x7f')

    async def test_setup_failure_does_not_expose_upstream_details(self):
        async def mock_google(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.receive()
            await ws.send_json({'error': {'message': 'secret-should-never-leak'}})
            return ws

        mock_app = web.Application()
        mock_app.router.add_get('/live', mock_google)
        async with TestServer(mock_app) as upstream:
            with patch.object(server, 'ENDPOINT', str(upstream.make_url('/live'))), patch.object(server, 'read_key', return_value='secret-should-never-leak'):
                async with self.client.ws_connect('/ws') as socket:
                    await socket.send_json({'type': 'start'})
                    message = await socket.receive_json()
                    self.assertEqual(message['type'], 'error')
                    self.assertNotIn('secret-should-never-leak', str(message))


if __name__ == '__main__':
    unittest.main()
