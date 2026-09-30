// Demo Agent — 중앙 제어(live-bridge)에 맞춘 가장 작은 Live API 페이지.
// agent 역할: persona.txt로 대화. master 역할: host.txt + start_chat/stop_chat 도구로 쇼 진행.
import { AudioPlayer } from "./audio.js";

const MODEL = "gemini-3.8-live";
const ENDPOINT =
  "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key=";
const HOST_TOOLS = [
  {
    functionDeclarations: [
      {
        name: "start_chat",
        description: "관람객이 AI 친구들에게 어떤 주제로 대화해보라고 요청했을 때 호출한다.",
        parameters: {
          type: "OBJECT",
          properties: {
            topic: { type: "STRING", description: "대화 주제" },
            utterance: { type: "STRING", description: "관람객이 한 말 그대로" },
          },
          required: ["topic", "utterance"],
        },
      },
      { name: "stop_chat", description: "관람객이 그만/멈춰/조용히 하라고 했을 때 호출한다." },
    ],
  },
];

const bridge = window.LiveBridge;
const $ = (id) => document.getElementById(id);
let session = null;
let level = 0;
let speaking = false;

function isHost() {
  return bridge?.role === "master";
}

function status(text) {
  $("status").textContent = text;
}

function toBase64(buffer) {
  let binary = "";
  const bytes = new Uint8Array(buffer);
  for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
  return btoa(binary);
}

async function start() {
  if (session) return session.ready;
  if (!window.GEMINI_KEY) throw new Error("key.js에 GEMINI_KEY가 없음");
  const persona = await (await fetch(isHost() ? "host.txt" : "persona.txt", { cache: "no-store" })).text();
  const context = new AudioContext();
  await context.resume();
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
  });
  await context.audioWorklet.addModule("pcm-worklet.js");
  const source = context.createMediaStreamSource(stream);
  const worklet = new AudioWorkletNode(context, "microphone-pcm");
  source.connect(worklet);
  worklet.connect(context.destination);

  const ws = new WebSocket(ENDPOINT + encodeURIComponent(window.GEMINI_KEY));
  const player = new AudioPlayer(context, (on) => {
    speaking = on;
    bridge?.speaking(on);
    status(on ? "speaking" : "listening");
  });
  let resolveReady, rejectReady;
  const ready = new Promise((resolve, reject) => ([resolveReady, rejectReady] = [resolve, reject]));
  session = { ws, context, stream, source, worklet, player, ready, live: false };

  ws.onopen = () => {
    const setup = {
      model: `models/${MODEL}`,
      generationConfig: { responseModalities: ["AUDIO"] },
      systemInstruction: { parts: [{ text: persona }] },
      outputAudioTranscription: {},
    };
    if (isHost()) setup.tools = HOST_TOOLS;
    ws.send(JSON.stringify({ setup }));
  };
  ws.onmessage = async (event) => {
    const msg = JSON.parse(typeof event.data === "string" ? event.data : await event.data.text());
    if (msg.setupComplete) {
      session.live = true;
      status("listening");
      resolveReady();
    }
    if (msg.toolCall) handleTools(msg.toolCall.functionCalls || []);
    const content = msg.serverContent;
    if (!content) return;
    if (content.interrupted) player.clear();
    if (content.outputTranscription?.text) $("caption").textContent = content.outputTranscription.text;
    const muted = isHost() && bridge.mode === "chat";
    for (const part of content.modelTurn?.parts || []) {
      if (part.inlineData && !muted) player.enqueue(part.inlineData.data, part.inlineData.mimeType);
    }
  };
  ws.onclose = (event) => {
    rejectReady(new Error(`세션 닫힘 ${event.code} ${event.reason}`));
    if (session?.ws === ws) {
      cleanup();
      bridge?.ended();
    }
  };
  worklet.port.onmessage = ({ data }) => {
    level = level * 0.8 + data.level * 0.2;
    if (session?.live && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ realtimeInput: { audio: { data: toBase64(data.pcm), mimeType: "audio/pcm;rate=16000" } } }));
    }
  };
  return ready;
}

function handleTools(calls) {
  const responses = calls.map((call) => {
    if (call.name === "start_chat") bridge?.startChat(call.args?.topic, call.args?.utterance);
    if (call.name === "stop_chat") bridge?.stopChat();
    return { id: call.id, name: call.name, response: { result: "ok" } };
  });
  session?.ws.send(JSON.stringify({ toolResponse: { functionResponses: responses } }));
}

function cleanup() {
  if (!session) return;
  const { ws, context, stream, worklet, source, player } = session;
  session = null;
  player.clear();
  worklet.port.onmessage = null;
  source.disconnect();
  worklet.disconnect();
  stream.getTracks().forEach((track) => track.stop());
  context.close();
  if (ws.readyState <= WebSocket.OPEN) ws.close();
  status("sleep");
  $("caption").textContent = "";
}

async function stop() {
  cleanup();
}

function say(text) {
  session?.ws.send(JSON.stringify({ realtimeInput: { text } }));
}

bridge?.connect({ start, stop, say });
bridge?.on("hello", (msg) => ($("name").textContent = msg.name));
bridge?.on("connected", () => ($("start").hidden = true));
$("start").addEventListener("click", () => (session ? stop() : start().catch((e) => status(e.message))));

// ---------- 2인치 화면용 아주 단순한 creative code 자리 ----------
const canvas = $("face");
const pen = canvas.getContext("2d");
function draw(time) {
  const w = (canvas.width = innerWidth * devicePixelRatio);
  const h = (canvas.height = innerHeight * devicePixelRatio);
  const base = Math.min(w, h) * 0.22;
  const radius = session
    ? base * (1 + (speaking ? 0.25 * Math.abs(Math.sin(time / 90)) : 0) + Math.min(level * 4, 0.4))
    : base * (0.8 + 0.03 * Math.sin(time / 800));
  pen.fillStyle = "#000";
  pen.fillRect(0, 0, w, h);
  pen.fillStyle = !session ? "#333" : speaking ? "#ff9a3c" : "#4fd08a";
  pen.beginPath();
  pen.arc(w / 2, h / 2, radius, 0, Math.PI * 2);
  pen.fill();
  requestAnimationFrame(draw);
}
requestAnimationFrame(draw);
