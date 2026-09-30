"""No Pi or Gemini key required: real HTTP/WebSocket plus installation failure checks."""
import asyncio
import base64
import importlib.util
import io
import json
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import project


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


controller = module('controller', ROOT / 'controller/server.py')
agent = module('agent', ROOT / 'agent/agent.py')


class OctoCheck(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.work = Path(self.temp.name)
        self.data = self.work / 'data'
        self.patch = patch.multiple(controller, DATA=self.data, UPLOADS=self.data / 'uploads', STORE=self.data / 'state.json')
        self.patch.start()
        self.app = controller.create_app('http://127.0.0.1:8700')
        self.app.cleanup_ctx.clear()  # The UDP broadcast belongs on the exhibition LAN.
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.hub = self.app['hub']
        self.headers = {'X-Token': self.hub.store.token}

    async def asyncTearDown(self):
        await self.hub.stop_chat()
        await self.client.close()
        await asyncio.sleep(.25)  # Drain coalesced dashboard updates.
        self.patch.stop()
        self.temp.cleanup()

    async def test_install_auth_fixed_roles_and_validation(self):
        self.assertEqual((await self.client.get('/api/state')).status, 401)
        response = await self.client.get('/join')
        script = await response.text()
        self.assertEqual(response.status, 200)
        self.assertNotIn(self.hub.store.token, script)
        self.assertNotIn('__PROJECT_PY__', script)
        compile(controller.agent_source(), 'agent.py', 'exec')
        self.hub.store.device('pi1')
        master = self.hub.store.device('master')
        self.assertEqual(master['role'], 'master')
        self.assertEqual(master['project'], 'builtin:master')
        response = await self.client.post('/api/devices/pi1', headers=self.headers, json={'name': 'changed', 'role': 'master'})
        self.assertEqual(response.status, 400)
        self.assertEqual(self.hub.store.devices['pi1']['name'], 'pi1')
        response = await self.client.post('/api/settings', headers=self.headers, json={'default_key': 'test-key', 'max_seconds': 'NaN'})
        self.assertEqual(response.status, 400)
        self.assertEqual(self.hub.store.settings['default_key'], '')
        self.assertNotIn('repo:octo', [p['id'] for p in controller.list_projects()])
        self.assertNotIn('repo:Hayeon', [p['id'] for p in controller.list_projects()])

    async def test_pair_and_agent_wire_protocol(self):
        ws = await self.client.ws_connect('/ws/pair?id=pi1&name=one')
        self.assertEqual((await ws.receive_json())['type'], 'pair_wait')
        response = await self.client.post('/api/pair/pi1/approve', headers=self.headers)
        self.assertEqual(response.status, 200)
        self.assertEqual((await ws.receive_json())['token'], self.hub.store.token)
        await ws.close()
        ws = await self.client.ws_connect('/ws/device?id=pi1', headers=self.headers)
        await ws.send_json({'type': 'hello', 'bridge': 1, 'state': 'running'})
        messages = [await ws.receive_json() for _ in range(3)]
        self.assertEqual(messages[0]['type'], 'config')
        self.assertEqual(messages[2]['msg']['type'], 'sleep')
        await ws.send_json({'type': 'event', 'name': 'start_chat', 'topic': 'unauthorized slave'})
        await asyncio.sleep(.02)
        self.assertEqual(self.hub.show['mode'], 'idle')
        await ws.close()

    async def test_bundle_hooks_and_immutable_selection(self):
        originals = {}
        for name in project.STUDENTS:
            folder = controller.REPO / name
            for p in folder.rglob('*'):
                if p.is_file(): originals[p] = p.read_bytes()
            path = controller.build_bundle(folder)
            with zipfile.ZipFile(path) as archive:
                self.assertIn('octo-student.js', archive.namelist())
                self.assertIn('octo-config.js', archive.read('index.html').decode())
                self.assertNotIn('key.js', archive.namelist())
                source = project.STUDENTS[name][0]
                self.assertIn('OctoStudent.connect', archive.read(source).decode())
                if name == 'Yunjung':
                    compile(archive.read('server.py'), 'server.py', 'exec')
                if name == 'ParkSoyeon':
                    self.assertNotIn('if (localApiKey) connectSafely(true);', archive.read('index.html').decode())
            path.unlink()
        self.assertTrue(all(p.read_bytes() == data for p, data in originals.items()))
        rec = self.hub.store.device('pi1')
        rec['project'] = 'repo:minseo'
        message = self.hub.deploy_message('pi1')
        rec['project'] = 'repo:sunny'
        response = await self.client.get(message['path'], headers=self.headers)
        self.assertEqual(response.status, 200)
        with zipfile.ZipFile(io.BytesIO(await response.read())) as archive:
            self.assertIn('말꼬리', archive.read('app.js').decode())
        rec['project'] = 'repo:minseo'
        response = await self.client.post('/api/devices/pi1/deploy', headers=self.headers)
        self.assertEqual((await response.json())['queued'], True)
        self.assertTrue(rec['deploy_pending'])

    async def test_show_ack_audio_fade_and_stop(self):
        class Endpoint:
            def __init__(self, id): self.id, self.messages = id, []
            async def send_json(endpoint, msg):
                endpoint.messages.append(msg)
                if msg.get('msg', {}).get('type') == 'kickoff':
                    owner, future = self.hub.pending[msg['req']]
                    self.assertEqual(owner, endpoint.id)
                    future.set_result({'ok': True})
        for id in ('master', 'pi1', 'pi2', 'pi3'):
            self.hub.store.device(id)
            self.hub.sockets[id] = Endpoint(id)
            self.hub.live[id] = {'bridge': 1}
        self.hub.store.settings.update(others_delay=0, max_seconds=10, fade_seconds=.03)
        audio = {'mimeType': 'audio/pcm;rate=16000', 'chunks': [base64.b64encode(b'\x01\x00' * 512).decode()]}
        await self.hub.start_chat('weather', target='pi1', audio=audio)
        with self.assertRaises(web.HTTPConflict): await self.hub.start_chat('another')
        await asyncio.sleep(.02)
        self.assertEqual(set(self.hub.show['active']), {'pi1', 'pi2', 'pi3'})
        kickoff = next(m['msg'] for m in self.hub.sockets['pi1'].messages if m.get('msg', {}).get('type') == 'kickoff')
        self.assertEqual(kickoff['audio'], audio)
        await self.hub.fade_chat()
        await asyncio.sleep(.08)
        self.assertEqual(self.hub.show['mode'], 'idle')
        for id in ('pi1', 'pi2', 'pi3'):
            self.assertEqual(self.hub.voice_for(id)['type'], 'sleep')
        self.assertEqual(self.hub.voice_for('master')['type'], 'wake')
        await self.hub.start_chat('restart', target='pi2')
        await self.hub.stop_chat()
        await asyncio.sleep(.03)
        self.assertEqual(self.hub.show['active'], [])
        for bad in ({'mimeType': 'audio/wav', 'chunks': []}, {'mimeType': audio['mimeType'], 'chunks': ['!!!!']}):
            with self.assertRaises(web.HTTPBadRequest): controller.validate_audio(bad)
        self.hub.sockets.clear()

    async def test_zip_safety_manifest_and_original_prompt_restore(self):
        bad = self.work / 'bad.zip'
        with zipfile.ZipFile(bad, 'w') as archive: archive.writestr('../escape', 'no')
        with self.assertRaises(ValueError): project.extract_zip(bad, self.work / 'out')
        with self.assertRaises(ValueError): project.validate_manifest({'files': {'../key.js': 'bad'}})
        local = self.work / 'endpoint'
        local.mkdir()
        art = local / 'project'
        art.mkdir()
        (art / 'persona.txt').write_text('student original')
        with patch.multiple(agent, HOME=local, PROJECT=art, CONFIG=local/'config.json', STATE=local/'state.json', SETUP_MARK=local/'setup.done'):
            endpoint = agent.Agent()
            endpoint.state.update(manifest={'prompt_file': 'persona.txt'}, prompt='edited')
            endpoint.render_files()
            self.assertEqual((art/'persona.txt').read_text(), 'edited')
            endpoint.state['prompt'] = None
            endpoint.render_files()
            self.assertEqual((art/'persona.txt').read_text(), 'student original')

    async def test_failed_setup_restores_previous_project(self):
        local = self.work / 'endpoint'
        local.mkdir()
        art = local/'project'
        art.mkdir()
        (art/'old.txt').write_text('keep me')
        new = io.BytesIO()
        with zipfile.ZipFile(new, 'w') as archive: archive.writestr('new.txt', 'new')
        download = web.Application()
        download.router.add_get('/new.zip', lambda _: web.Response(body=new.getvalue()))
        server = TestServer(download)
        await server.start_server()
        try:
            with patch.multiple(agent, HOME=local, PROJECT=art, CONFIG=local/'config.json', STATE=local/'state.json', SETUP_MARK=local/'setup.done'):
                endpoint = agent.Agent()
                endpoint.config['token'] = 'test'
                endpoint.base = str(server.make_url('/')).rstrip('/')
                endpoint.state.update(project='old', manifest={'run': []}, running=True)
                endpoint.start = AsyncMock(side_effect=[RuntimeError('failed setup'), None])
                with self.assertRaisesRegex(RuntimeError, 'failed setup'):
                    await endpoint.deploy({'project': 'new', 'path': '/new.zip', 'manifest': {'run': []}})
                self.assertEqual(endpoint.state['project'], 'old')
                self.assertEqual((art/'old.txt').read_text(), 'keep me')
                self.assertFalse((art/'new.txt').exists())
                self.assertEqual(endpoint.start.await_count, 2)
        finally:
            await server.close()


if __name__ == '__main__':
    unittest.main()
