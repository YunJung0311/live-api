"""Loopback-only HTTP/WebSocket relay for the reference Live API project."""

import argparse
import asyncio
import base64
import json
import os
import ssl
from pathlib import Path
from urllib.parse import urlencode

import certifi
from aiohttp import ClientConnectorCertificateError, ClientSession, ClientTimeout, WSMsgType, web
from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
MODEL = "gemini-3.8-live"
ENDPOINT = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
)
ASSETS = {
    "/": "index.html",
    "/style.css": "style.css",
    "/app.js": "app.js",
    "/audio.js": "audio.js",
    "/pcm-worklet.js": "pcm-worklet.js",
}

VOICES = frozenset(
    "Zephyr Puck Charon Kore Fenrir Leda Orus Aoede Callirrhoe Autonoe "
    "Enceladus Iapetus Umbriel Algieba Despina Erinome Algenib Rasalgethi "
    "Laomedeia Achernar Alnilam Schedar Gacrux Pulcherrima Achird "
    "Zubenelgenubi Vindemiatrix Sadachbia Sadaltager Sulafat".split()
)
START_SENSITIVITIES = frozenset(("START_SENSITIVITY_HIGH", "START_SENSITIVITY_LOW"))
END_SENSITIVITIES = frozenset(("END_SENSITIVITY_HIGH", "END_SENSITIVITY_LOW"))
ACTIVITY_HANDLING = frozenset(("START_OF_ACTIVITY_INTERRUPTS", "NO_INTERRUPTION"))

DEFAULT_SETTINGS = {
    "voiceName": "Kore",
    "topP": None,
    "topK": None,
    "presencePenalty": None,
    "frequencyPenalty": None,
    "temperature": None,
    "maxOutputTokens": None,
    "startOfSpeechSensitivity": "START_SENSITIVITY_HIGH",
    "endOfSpeechSensitivity": "END_SENSITIVITY_HIGH",
    "prefixPaddingMs": 20,
    "silenceDurationMs": 700,
    "activityHandling": "START_OF_ACTIVITY_INTERRUPTS",
    "inputTranscription": True,
    "outputTranscription": True,
    "systemInstruction": "",
    "promptInstruction": "",
}


def read_key():
    values = dotenv_values(ENV_PATH)
    return (os.environ.get("GEMINI_API_KEY") or values.get("GEMINI_API_KEY") or "").strip()


def _choice(settings, name, choices):
    value = settings[name]
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f"invalid {name}")
    return value


def _integer(settings, name, minimum, maximum):
    value = settings[name]
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"invalid {name}")
    return value


def parse_settings(raw):
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("invalid settings")
    unknown = set(raw) - set(DEFAULT_SETTINGS)
    if unknown:
        raise ValueError("unknown settings")

    settings = DEFAULT_SETTINGS | raw
    parsed = {
        "voiceName": _choice(settings, "voiceName", VOICES),
        "startOfSpeechSensitivity": _choice(
            settings, "startOfSpeechSensitivity", START_SENSITIVITIES
        ),
        "endOfSpeechSensitivity": _choice(
            settings, "endOfSpeechSensitivity", END_SENSITIVITIES
        ),
        "activityHandling": _choice(settings, "activityHandling", ACTIVITY_HANDLING),
        # ponytail: practical UI bounds, widen only if device measurements need it.
        "prefixPaddingMs": _integer(settings, "prefixPaddingMs", 0, 500),
        "silenceDurationMs": _integer(settings, "silenceDurationMs", 100, 2000),
        "maxOutputTokens": None,
        "temperature": None,
    }
    for name in ("inputTranscription", "outputTranscription"):
        if type(settings[name]) is not bool:
            raise ValueError(f"invalid {name}")
        parsed[name] = settings[name]

    temperature = settings["temperature"]
    if temperature is not None:
        if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
            raise ValueError("invalid temperature")
        temperature = float(temperature)
        if not 0 <= temperature <= 2:
            raise ValueError("invalid temperature")
        parsed["temperature"] = temperature

    max_tokens = settings["maxOutputTokens"]
    if max_tokens is not None:
        if type(max_tokens) is not int or not 1 <= max_tokens <= 65536:
            raise ValueError("invalid maxOutputTokens")
        parsed["maxOutputTokens"] = max_tokens

    # Optional sampling experiments: blank preserves the model default.
    # These are classroom bounds, not a claim about every model's API limits.
    for name, minimum, maximum in (("topP", 0.01, 1),
                                   ("presencePenalty", -2, 2),
                                   ("frequencyPenalty", -2, 2)):
        value = settings[name]
        if value is not None and (type(value) not in (int, float)
                                  or not minimum <= value <= maximum):
            raise ValueError(f"invalid {name}")
        parsed[name] = value
    parsed["topK"] = None if settings["topK"] is None else _integer(settings, "topK", 1, 100)

    instruction = settings["systemInstruction"]
    if not isinstance(instruction, str) or len(instruction) > 8000:
        raise ValueError("invalid systemInstruction")
    parsed["systemInstruction"] = instruction.strip() or (ROOT / "persona.txt").read_text()
    extra = settings["promptInstruction"]
    if not isinstance(extra, str) or len(extra) > 2000:
        raise ValueError("invalid promptInstruction")
    if extra.strip():
        parsed["systemInstruction"] += "\n\n" + extra.strip()
    return parsed


def setup_message(settings, handle=None):
    generation = {
        "responseModalities": ["AUDIO"],
        "speechConfig": {
            "voiceConfig": {
                "prebuiltVoiceConfig": {"voiceName": settings["voiceName"]}
            }
        },
    }
    if settings["temperature"] is not None:
        generation["temperature"] = settings["temperature"]
    if settings["maxOutputTokens"] is not None:
        generation["maxOutputTokens"] = settings["maxOutputTokens"]
    for name in ("topP", "topK", "presencePenalty", "frequencyPenalty"):
        if settings[name] is not None:
            generation[name] = settings[name]

    setup = {
        "model": f"models/{MODEL}",
        "sessionResumption": {"handle": handle} if handle else {},
        "generationConfig": generation,
        "systemInstruction": {"parts": [{"text": settings["systemInstruction"]}]},
        "realtimeInputConfig": {
            "automaticActivityDetection": {
                "disabled": False,
                "startOfSpeechSensitivity": settings["startOfSpeechSensitivity"],
                "endOfSpeechSensitivity": settings["endOfSpeechSensitivity"],
                "prefixPaddingMs": settings["prefixPaddingMs"],
                "silenceDurationMs": settings["silenceDurationMs"],
            },
            "activityHandling": settings["activityHandling"],
        },
        "contextWindowCompression": {
            "triggerTokens": 25000,
            "slidingWindow": {"targetTokens": 8000},
        },
    }
    if settings["inputTranscription"]:
        setup["inputAudioTranscription"] = {}
    if settings["outputTranscription"]:
        setup["outputAudioTranscription"] = {}
    return {"setup": setup}


@web.middleware
async def local_only(request, handler):
    if request.url.host not in ("localhost", "127.0.0.1"):
        raise web.HTTPForbidden()
    origin = request.headers.get("Origin")
    if origin and origin != f"{request.scheme}://{request.host}":
        raise web.HTTPForbidden()
    return await handler(request)


async def asset(request):
    response = web.FileResponse(ROOT / ASSETS[request.path])
    response.headers.update(
        {
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; object-src 'none'; frame-ancestors 'none'; "
                "base-uri 'none'; form-action 'self'"
            ),
        }
    )
    return response


async def health(_request):
    return web.json_response(
        {"configured": bool(read_key()), "model": MODEL},
        headers={"Cache-Control": "no-store"},
    )


async def relay(request):
    browser = web.WebSocketResponse(max_msg_size=65536, heartbeat=20)
    await browser.prepare(request)
    tasks = []

    async def error(message):
        if not browser.closed:
            await browser.send_json({"type": "error", "message": message})

    try:
        initial = await asyncio.wait_for(browser.receive(), timeout=15)
        if initial.type != WSMsgType.TEXT:
            raise ValueError("start message required")
        start = json.loads(initial.data)
        if not isinstance(start, dict) or start.get("type") != "start":
            raise ValueError("invalid start message")
        settings = parse_settings(start.get("settings"))
        handle = start.get("handle")
        if handle is not None and (not isinstance(handle, str) or not 1 <= len(handle) <= 8192):
            raise ValueError("invalid session handle")
        key = read_key()
        if not key:
            await error("master/.env에 GEMINI_API_KEY를 설정해 주세요.")
            return browser

        url = ENDPOINT + "?" + urlencode({"key": key})
        timeout = ClientTimeout(total=None, sock_connect=15)
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        async with ClientSession(timeout=timeout) as client:
            async with client.ws_connect(
                url, ssl=ssl_context, max_msg_size=4 * 1024 * 1024
            ) as upstream:
                await upstream.send_json(setup_message(settings, handle))
                response = await asyncio.wait_for(upstream.receive(), timeout=20)
                if response.type not in (WSMsgType.TEXT, WSMsgType.BINARY):
                    raise ValueError("setup closed")
                payload = json.loads(response.data)
                if "setupComplete" not in payload:
                    raise ValueError("setup rejected")
                await browser.send_json({"type": "ready", "model": MODEL})

                async def upload():
                    async for message in browser:
                        if message.type == WSMsgType.BINARY:
                            if (
                                not message.data
                                or len(message.data) > 65536
                                or len(message.data) % 2
                            ):
                                raise ValueError("invalid PCM")
                            await upstream.send_json(
                                {
                                    "realtimeInput": {
                                        "audio": {
                                            "data": base64.b64encode(message.data).decode("ascii"),
                                            "mimeType": "audio/pcm;rate=16000",
                                        }
                                    }
                                }
                            )
                        elif message.type == WSMsgType.TEXT:
                            data = json.loads(message.data)
                            if isinstance(data, dict) and data == {"type": "audioStreamEnd"}:
                                await upstream.send_json({"realtimeInput": {"audioStreamEnd": True}})
                                continue
                            if not isinstance(data, dict) or data.get("type") != "text":
                                raise ValueError("invalid text message")
                            text = data.get("text")
                            if (
                                not isinstance(text, str)
                                or not text.strip()
                                or len(text) > 2000
                            ):
                                raise ValueError("invalid text length")
                            await upstream.send_json(
                                {"realtimeInput": {"text": text.strip()}}
                            )

                async def download():
                    async for message in upstream:
                        if message.type not in (WSMsgType.TEXT, WSMsgType.BINARY):
                            continue
                        payload = json.loads(message.data)
                        if "error" in payload:
                            raise ValueError("upstream error")
                        if "serverContent" in payload:
                            await browser.send_json(
                                {"type": "content", "content": payload["serverContent"]}
                            )
                        if "usageMetadata" in payload:
                            await browser.send_json(
                                {"type": "usage", "usage": payload["usageMetadata"]}
                            )
                        if "sessionResumptionUpdate" in payload:
                            await browser.send_json({"type": "resumption", "update": payload["sessionResumptionUpdate"]})
                        if "goAway" in payload:
                            await browser.send_json(
                                {
                                    "type": "notice",
                                    "message": "연결이 곧 종료됩니다. 종료 후 ‘이전 대화 이어가기’를 누르세요. 마지막 발화는 다시 말해야 할 수 있습니다.",
                                }
                            )
                    await error("Gemini 연결이 종료되었습니다. 다시 시작해 주세요.")

                tasks = [asyncio.create_task(upload()), asyncio.create_task(download())]
                done, _pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    task.result()
    except ClientConnectorCertificateError:
        await error("SSL 인증서 검증에 실패했습니다. Python 인증서 설정을 확인해 주세요.")
    except Exception:
        # Upstream errors may contain the credential-bearing URL. Never relay details.
        await error("Gemini 연결에 실패했습니다. 키·네트워크·모델 권한을 확인하세요. 샘플링 실험값을 비우거나 새 대화로 다시 시도하세요.")
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
    app.router.add_get("/api/health", health)
    app.router.add_get("/ws", relay)
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Gemini Live reference implementation")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    web.run_app(create_app(), host="127.0.0.1", port=args.port, access_log=None)
