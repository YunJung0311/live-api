// Microphone-only host. All speech output belongs to the student's own project.
import { VisitorAudio } from './audio.js';
const bridge = window.LiveBridge;
const status = (text) => { document.getElementById('status').textContent = text; };
const tools = [{ functionDeclarations: [
  { name: 'start_chat', description: '관람객이 AI들에게 주제에 대해 대화하라고 요청함',
    parameters: { type: 'OBJECT', properties: { topic: { type: 'STRING' }, utterance: { type: 'STRING' } }, required: ['topic', 'utterance'] } },
  { name: 'stop_chat', description: '관람객이 대화를 멈추라고 요청함' },
] }];
let session = null;
const capture = new VisitorAudio();
const encode = (buffer) => {
  const bytes = new Uint8Array(buffer);
  let text = '';
  for (const byte of bytes) text += String.fromCharCode(byte);
  return btoa(text);
};

async function start() {
  if (session) return session.ready;
  if (!window.GEMINI_KEY) throw new Error('대시보드에서 마스터 API 키를 넣으세요');
  const s = { done: false, live: false };
  session = s;
  status('마이크 연결 중');
  try {
    const response = await fetch('persona.txt', { cache: 'no-store' });
    if (!response.ok) throw new Error('마스터 프롬프트를 읽지 못함');
    const persona = await response.text();
    s.stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
    if (s.done) { s.stream.getTracks().forEach((t) => t.stop()); return; }
    s.context = new AudioContext();
    await s.context.resume();
    await s.context.audioWorklet.addModule('pcm-worklet.js');
    s.source = s.context.createMediaStreamSource(s.stream);
    s.worklet = new AudioWorkletNode(s.context, 'microphone-pcm');
    s.source.connect(s.worklet);
    s.worklet.connect(s.context.destination); // processor outputs silence, never microphone sound
    capture.clear();
    s.ws = new WebSocket('wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key=' + encodeURIComponent(window.GEMINI_KEY));
    s.ready = new Promise((resolve, reject) => { s.resolve = resolve; s.reject = reject; });
    s.timer = setTimeout(() => { s.reject(new Error('마스터 세션 준비 시간 초과')); cleanup(s); }, 30000);
    s.ws.onopen = () => s.ws.send(JSON.stringify({ setup: {
      model: 'models/' + (window.OCTO_MODEL || 'gemini-3.8-live'),
      generationConfig: { responseModalities: ['AUDIO'] },
      systemInstruction: { parts: [{ text: persona }] }, tools,
      inputAudioTranscription: {}, contextWindowCompression: { slidingWindow: {} },
    } }));
    s.ws.onmessage = async ({ data }) => {
      if (s.done) return;
      try {
        const msg = JSON.parse(typeof data === 'string' ? data : await data.text());
        if (msg.setupComplete) { s.live = true; clearTimeout(s.timer); s.resolve(); status('관람객의 말을 기다리는 중'); }
        if (msg.error) throw new Error('Gemini 설정·키·할당량을 확인하세요');
        if (msg.serverContent?.inputTranscription?.text) document.getElementById('caption').textContent = msg.serverContent.inputTranscription.text;
        // Model audio is deliberately never decoded or played on the master.
        if (msg.toolCall) {
          const responses = msg.toolCall.functionCalls.map((call) => {
            if (call.name === 'start_chat' && bridge.mode === 'idle') bridge.startChat(call.args?.topic, call.args?.utterance, capture.take());
            else if (call.name === 'stop_chat') { capture.clear(); bridge.stopChat(); }
            return { id: call.id, name: call.name, response: { result: 'routed' } };
          });
          s.ws.send(JSON.stringify({ toolResponse: { functionResponses: responses } }));
        }
      } catch (error) { bridge.log(error.message); cleanup(s); bridge.ended(); }
    };
    s.ws.onclose = () => { cleanup(s); bridge.ended(); };
    s.worklet.port.onmessage = ({ data }) => {
      if (!s.live || s.done || s.ws.readyState !== WebSocket.OPEN) return;
      if (bridge.mode === 'idle') capture.push(data.pcm, data.level);
      s.ws.send(JSON.stringify({ realtimeInput: { audio: { data: encode(data.pcm), mimeType: 'audio/pcm;rate=16000' } } }));
    };
    return await s.ready;
  } catch (error) { cleanup(s); throw error; }
}
function cleanup(s) {
  if (s.done) return;
  s.done = true;
  clearTimeout(s.timer);
  s.reject?.(new Error('마스터 세션 종료'));
  s.stream?.getTracks().forEach((t) => t.stop());
  if (s.worklet) s.worklet.port.onmessage = null;
  s.source?.disconnect(); s.worklet?.disconnect(); s.context?.close(); s.ws?.close();
  if (session === s) session = null;
  capture.clear(); status('마이크 꺼짐');
}
bridge.connect({ start, stop: async () => { if (session) cleanup(session); } });
bridge.on('mode', () => { capture.clear(); if (session?.live) status(bridge.mode === 'idle' ? '관람객의 말을 기다리는 중' : '슬레이브 대화 중 · 그만이라고 말하면 종료'); });
