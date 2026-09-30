"""Local tests. No Google connection or real credential is used."""

import asyncio
import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

import server


class SettingsTests(unittest.TestCase):
    def test_defaults_and_supported_controls_reach_setup(self):
        settings = server.parse_settings(
            {
                "voiceName": "Leda",
                "topP": 0.9,
                "topK": 20,
                "presencePenalty": 0.3,
                "frequencyPenalty": 0.2,
                "promptInstruction": "다정하게 답하세요.",
                "temperature": 1.2,
                "maxOutputTokens": 1024,
                "startOfSpeechSensitivity": "START_SENSITIVITY_LOW",
                "endOfSpeechSensitivity": "END_SENSITIVITY_LOW",
                "prefixPaddingMs": 40,
                "silenceDurationMs": 800,
                "activityHandling": "NO_INTERRUPTION",
                "inputTranscription": False,
            }
        )
        setup = server.setup_message(settings)["setup"]
        generation = setup["generationConfig"]
        vad = setup["realtimeInputConfig"]["automaticActivityDetection"]
        self.assertEqual(generation["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"], "Leda")
        self.assertNotIn("thinkingConfig", generation)
        self.assertEqual(setup["model"], "models/gemini-3.8-live")
        self.assertEqual(generation["topP"], 0.9)
        self.assertEqual(generation["topK"], 20)
        self.assertEqual(generation["presencePenalty"], 0.3)
        self.assertEqual(generation["frequencyPenalty"], 0.2)
        self.assertTrue(setup["systemInstruction"]["parts"][0]["text"].endswith("다정하게 답하세요."))
        self.assertEqual(server.setup_message(settings, "resume-token")["setup"]["sessionResumption"], {"handle": "resume-token"})
        self.assertEqual(generation["temperature"], 1.2)
        self.assertEqual(generation["maxOutputTokens"], 1024)
        self.assertEqual(vad["startOfSpeechSensitivity"], "START_SENSITIVITY_LOW")
        self.assertEqual(vad["silenceDurationMs"], 800)
        self.assertEqual(setup["realtimeInputConfig"]["activityHandling"], "NO_INTERRUPTION")
        self.assertNotIn("inputAudioTranscription", setup)
        self.assertIn("outputAudioTranscription", setup)
        self.assertEqual(setup["contextWindowCompression"]["slidingWindow"]["targetTokens"], 8000)

    def test_invalid_settings_are_rejected(self):
        invalid = (
            {"voiceName": "not-a-voice"},
            {"temperature": 2.1},
            {"maxOutputTokens": 0},
            {"silenceDurationMs": 99},
            {"inputTranscription": "yes"},
            {"unexpected": True},
            {"topP": float("nan")},
            {"topK": True},
            {"frequencyPenalty": 3},
            {"thinkingLevel": "high"},
            {"promptInstruction": []},
        )
        for settings in invalid:
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                server.parse_settings(settings)

    def test_key_prefers_process_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / ".env"
            env.write_text("GEMINI_API_KEY=file-key\n")
            with patch.object(server, "ENV_PATH", env), patch.dict(
                server.os.environ, {"GEMINI_API_KEY": "process-key"}
            ):
                self.assertEqual(server.read_key(), "process-key")


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = TestClient(TestServer(server.create_app()))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_only_allowlisted_assets_are_served(self):
        for path in server.ASSETS:
            response = await self.client.get(path)
            self.assertEqual(response.status, 200, path)
            self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
            await response.read()
        for path in ("/.env", "/server.py", "/persona.txt", "/README.md", "/.env.example"):
            response = await self.client.get(path)
            self.assertEqual(response.status, 404, path)

    async def test_foreign_origins_and_hosts_are_rejected(self):
        for path in ("/", "/ws", "/api/health"):
            response = await self.client.get(path, headers={"Origin": "https://example.com"})
            self.assertEqual(response.status, 403)
        response = await self.client.get("/", headers={"Host": "attacker.example"})
        self.assertEqual(response.status, 403)

    async def test_missing_key(self):
        with patch.object(server, "read_key", return_value=""):
            response = await self.client.get("/api/health")
            self.assertEqual(await response.json(), {"configured": False, "model": server.MODEL})
            async with self.client.ws_connect("/ws") as socket:
                await socket.send_json({"type": "start", "settings": {}})
                result = await socket.receive_json()
                self.assertEqual(result["type"], "error")
                self.assertIn(".env", result["message"])

    async def test_audio_text_content_usage_and_interrupt(self):
        received = []
        closed = asyncio.Event()
        content = {
            "modelTurn": {
                "parts": [
                    {"inlineData": {"data": "AAA=", "mimeType": "audio/pcm;rate=24000"}}
                ]
            },
            "inputTranscription": {"text": "안녕"},
            "outputTranscription": {"text": "반가워요"},
        }

        async def mock_google(request):
            self.assertEqual(request.query["key"], "fake-secret")
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            received.append(await ws.receive_json())
            await ws.send_json({"setupComplete": {}})
            received.append(await ws.receive_json())
            received.append(await ws.receive_json())
            received.append(await ws.receive_json())
            await ws.send_json({"serverContent": content})
            await ws.send_json({"usageMetadata": {"totalTokenCount": 12}})
            await ws.send_bytes(b'{"serverContent":{"interrupted":true}}')
            await ws.send_json({"sessionResumptionUpdate": {"resumable": True, "newHandle": "resume-token"}})
            await ws.receive()
            closed.set()
            return ws

        mock_app = web.Application()
        mock_app.router.add_get("/live", mock_google)
        async with TestServer(mock_app) as upstream:
            with patch.object(server, "ENDPOINT", str(upstream.make_url("/live"))), patch.object(
                server, "read_key", return_value="fake-secret"
            ):
                async with self.client.ws_connect("/ws") as socket:
                    await socket.send_json(
                        {"type": "start", "handle": "earlier-token", "settings": {"voiceName": "Leda", "silenceDurationMs": 800}}
                    )
                    self.assertEqual((await socket.receive_json())["type"], "ready")
                    await socket.send_bytes(b"\x00\x00\xff\x7f")
                    await socket.send_json({"type": "text", "text": "안녕"})
                    await socket.send_json({"type": "audioStreamEnd"})
                    self.assertEqual((await socket.receive_json())["content"], content)
                    self.assertEqual((await socket.receive_json())["usage"]["totalTokenCount"], 12)
                    self.assertTrue((await socket.receive_json())["content"]["interrupted"])
                    self.assertEqual((await socket.receive_json())["update"]["newHandle"], "resume-token")
                await asyncio.wait_for(closed.wait(), 3)

        setup = received[0]["setup"]
        self.assertEqual(setup["model"], f"models/{server.MODEL}")
        self.assertEqual(
            setup["generationConfig"]["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"],
            "Leda",
        )
        self.assertEqual(setup["realtimeInputConfig"]["automaticActivityDetection"]["silenceDurationMs"], 800)
        audio = received[1]["realtimeInput"]["audio"]
        self.assertEqual(audio["mimeType"], "audio/pcm;rate=16000")
        self.assertEqual(base64.b64decode(audio["data"]), b"\x00\x00\xff\x7f")
        self.assertEqual(received[2], {"realtimeInput": {"text": "안녕"}})
        self.assertEqual(received[3], {"realtimeInput": {"audioStreamEnd": True}})
        self.assertEqual(setup["sessionResumption"], {"handle": "earlier-token"})

    async def test_upstream_error_does_not_expose_details(self):
        async def mock_google(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.receive()
            await ws.send_json({"error": {"message": "secret-should-never-leak"}})
            return ws

        mock_app = web.Application()
        mock_app.router.add_get("/live", mock_google)
        async with TestServer(mock_app) as upstream:
            with patch.object(server, "ENDPOINT", str(upstream.make_url("/live"))), patch.object(
                server, "read_key", return_value="secret-should-never-leak"
            ):
                async with self.client.ws_connect("/ws") as socket:
                    await socket.send_json({"type": "start", "settings": {}})
                    message = await socket.receive_json()
                    self.assertEqual(message["type"], "error")
                    self.assertNotIn("secret-should-never-leak", str(message))


if __name__ == "__main__":
    unittest.main()
