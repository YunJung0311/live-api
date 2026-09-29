import { AudioPlayer } from './audio.js';
import { CharacterActivity, animateCharacter } from './character.js';

const $ = (id) => document.getElementById(id);
const startButton = $('start');
const stopButton = $('stop');
let current = null;

function state(name, message) {
  document.body.dataset.state = name;
  $('character-mood').textContent = { idle: '비트 타는 중', connecting: '세션 연결 중', listening: current?.mode === 'chat' ? '다음 한마디를 기다리는 중' : '네 플로우에 집중하는 중', processing: '다음 한마디를 고르는 중', speaking: '지금은 비트의 턴. 스껄!', error: '잠깐, 연결을 확인해 줘' }[name];
  const chat = current?.mode === 'chat';
  $('status').textContent = { idle: 'STANDBY · 연결 전', connecting: 'CONNECTING · 연결 중', processing: 'SAMPLING · 응답 준비 중', listening: chat ? 'YOUR TURN · 메시지 대기' : 'LISTENING · 듣는 중', speaking: 'ON AIR · 비트가 말하는 중', error: 'CHECK · 연결 확인 필요' }[name];
  $('hint').textContent = message ?? { idle: '시작 버튼을 누르면 마이크를 연결해요.', connecting: chat ? '채팅 대화를 준비하고 있어요.' : '마이크와 대화를 준비하고 있어요.', processing: '네 말을 받아 다음 응답을 준비하고 있어요.', listening: chat ? '아래 입력란으로 메시지를 보내주세요.' : '편하게 말을 걸어주세요.', speaking: '답변 중에도 편하게 말씀하세요.', error: '안내를 확인하고 다시 시작해 주세요.' }[name];
}

function notice(message = '') {
  $('notice').hidden = !message;
  $('notice').textContent = message;
}

function finish(session, message = '') {
  if (session.done) return;
  session.done = true;
  session.apiKey = '';
  clearTimeout(session.timer);
  session.abort.abort();
  session.stream?.getTracks().forEach((track) => track.stop());
  if (session.worklet) {
    if (session.worklet) session.worklet.port.onmessage = null;
    session.worklet.port.close();
    session.worklet.disconnect();
  }
  session.source?.disconnect();
  session.stopAnimation?.();
  session.player?.clear();
  session.analyser?.disconnect();
  if (session.socket) {
    session.socket.onmessage = null;
    session.socket.onopen = null;
    session.socket.onclose = null;
    session.socket.onerror = null;
    session.socket.close();
  }
  session.context?.close().catch(() => {});
  if (current === session) {
    current = null;
    startButton.disabled = false;
    stopButton.disabled = true;
    $('api-key').disabled = false;
    $('input-mode').disabled = false;
    $('chat-input').disabled = true;
    $('send').disabled = true;
    $('level').value = 0;
    $('mic-state').textContent = '꺼짐';
    state(message ? 'error' : 'idle', message ? undefined : '대화가 끝났어요. 다음에 또 만나요.');
    notice(message);
  }
}

function transcript(session, role, text, finished) {
  if (!text) {
    if (finished) delete session.lines[role];
    return;
  }
  const log = $('transcript');
  log.querySelector('.empty')?.remove();
  if (!session.lines[role]) {
    const entry = document.createElement('div');
    entry.className = `utterance ${role}`;
    const label = document.createElement('strong');
    label.textContent = role === 'user' ? '나' : '비트';
    const line = document.createElement('p');
    entry.append(label, line);
    log.append(entry);
    session.lines[role] = line;
    if (log.children.length > 100) log.firstElementChild.remove();
  }
  session.lines[role].textContent += text;
  log.scrollTop = log.scrollHeight;
  if (finished) delete session.lines[role];
}

function receive(session, event) {
  if (session.done) return;
  try {
    const message = JSON.parse(event.data);
    if (message.type === 'error') return finish(session, message.message);
    if (message.type === 'notice') return notice(message.message);
    if (message.type === 'ready') {
      clearTimeout(session.timer);
      session.ready = true;
      state('listening');
      $('mic-state').textContent = session.mode === 'chat' ? '사용 안 함' : '켜짐';
      $('chat-input').disabled = false;
      $('send').disabled = false;
      if (session.mode === 'chat') $('chat-input').focus();
    }
    if (message.type !== 'content') return;
    const content = message.content;
    if (content.interrupted) {
      session.activity.interrupted();
      session.player.clear();
      session.lines = {};
    }
    // Process every field: audio and transcripts may arrive in the same event.
    for (const [key, role] of [['inputTranscription', 'user'], ['outputTranscription', 'agent']]) {
      if (content[key]) transcript(session, role, content[key].text, content[key].finished);
    }
    if (!content.interrupted && content.inputTranscription?.finished) {
      session.activity.submitted(performance.now());
    }
    if (!content.interrupted) {
      for (const part of content.modelTurn?.parts ?? []) {
        if (part.inlineData) session.player.enqueue(part.inlineData.data, part.inlineData.mimeType);
      }
    }
    if (content.turnComplete) {
      session.lines = {};
      session.activity.complete();
    }
  } catch {
    finish(session, '오디오 응답을 처리하지 못했습니다. 다시 시작해 주세요.');
  }
}

async function start() {
  if (current) return;
  const session = { done: false, ready: false, lines: {}, abort: new AbortController(), apiKey: $('api-key').value.trim(), mode: document.querySelector('input[name="mode"]:checked').value };
  $('api-key').value = '';
  $('api-key').disabled = true;
  $('input-mode').disabled = true;
  current = session;
  startButton.disabled = true;
  stopButton.disabled = false;
  notice();
  state('connecting');
  $('transcript').replaceChildren();
  session.timer = setTimeout(() => finish(session, '연결 시간이 초과되었습니다. 마이크 권한과 네트워크를 확인해 주세요.'), 45000);
  try {
    if (session.mode === 'voice' && (!navigator.mediaDevices?.getUserMedia || !window.AudioWorkletNode)) {
      throw new Error('최신 Chrome에서 localhost 주소로 접속해 주세요.');
    }
    // Resume during the button gesture so browser autoplay rules allow playback.
    session.context = new AudioContext();
    await session.context.resume();
    if (session.done) return;
    const response = await fetch('/api/health', { signal: session.abort.signal });
    if (!response.ok) throw new Error('로컬 서버 연결을 확인해 주세요.');
    const config = await response.json();
    if (!config.configured && !session.apiKey) throw new Error('화면에 API 키를 입력하거나 Yunjung/.env에 GEMINI_API_KEY를 저장해 주세요.');
    if (session.done) return;
    session.activity = new CharacterActivity((name) => {
      if (!session.done && session.ready) state(name);
    });
    session.analyser = session.context.createAnalyser();
    session.analyser.fftSize = 256;
    session.analyser.connect(session.context.destination);
    session.player = new AudioPlayer(session.context, (speaking) => {
      if (!session.done && session.ready) session.activity.playback(speaking);
    }, session.analyser);
    session.stopAnimation = animateCharacter($('beat-character'), session);
    if (session.mode === 'voice') {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: {
        channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true,
      } });
      if (session.done) { stream.getTracks().forEach((track) => track.stop()); return; }
      session.stream = stream;
      stream.getAudioTracks()[0].addEventListener('ended', () => finish(session, '마이크 연결이 끊겼습니다. 장치를 확인해 주세요.'));
      await session.context.audioWorklet.addModule('/pcm-worklet.js');
      if (session.done) return;
      session.source = session.context.createMediaStreamSource(stream);
      session.worklet = new AudioWorkletNode(session.context, 'microphone-pcm');
      session.worklet.onprocessorerror = () => finish(session, '마이크 오디오 처리에 실패했습니다. 다시 시작해 주세요.');
      session.source.connect(session.worklet);
      session.worklet.connect(session.context.destination);
    }
    const socket = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`);
    session.socket = socket;
    socket.onopen = () => {
      if (session.done) return;
      socket.send(JSON.stringify({ type: 'start', apiKey: session.apiKey }));
      session.apiKey = '';
    };
    socket.onmessage = (event) => receive(session, event);
    socket.onerror = () => finish(session, '서버 연결에 실패했습니다. 로컬 서버가 실행 중인지 확인해 주세요.');
    socket.onclose = () => finish(session, '연결이 종료되었습니다. 대화 시작을 눌러 다시 연결해 주세요.');
    if (session.worklet) session.worklet.port.onmessage = ({ data }) => {
      if (session.done || !session.ready || socket.readyState !== WebSocket.OPEN) return;
      if (socket.bufferedAmount > 64000) {
        finish(session, '음성 전송이 지연되었습니다. 네트워크를 확인하고 다시 시작해 주세요.');
        return;
      }
      $('level').value = Math.min(1, data.level * 5);
      session.micLevel = $('level').value;
      session.lastMicAt = performance.now();
      session.activity.input(data.level, session.lastMicAt);
      socket.send(data.pcm);
    };
  } catch (error) {
    if (session.done) return;
    const message = {
      NotAllowedError: '마이크 권한이 필요합니다. 브라우저의 사이트 설정에서 허용해 주세요.',
      NotFoundError: '마이크를 찾지 못했습니다. 입력 장치를 연결해 주세요.',
      NotReadableError: '마이크를 사용할 수 없습니다. 다른 앱이나 장치 설정을 확인해 주세요.',
    }[error.name] ?? error.message ?? '연결에 실패했습니다. 다시 시작해 주세요.';
    finish(session, message);
  }
}

startButton.addEventListener('click', start);
stopButton.addEventListener('click', () => { if (current) finish(current); });
window.addEventListener('pagehide', () => { if (current) finish(current); });

$('chat-form').addEventListener('submit', (event) => {
  event.preventDefault();
  const session = current;
  const text = $('chat-input').value.trim();
  if (!text || !session?.ready || session.done || session.socket.readyState !== WebSocket.OPEN) return;
  if (text.length > 2000) { notice('메시지는 2,000자 이내로 입력해 주세요.'); return; }
  if (session.socket.bufferedAmount > 64000) { notice('전송이 지연되고 있어요. 잠시 후 다시 보내주세요.'); return; }
  session.socket.send(JSON.stringify({ type: 'text', text }));
  session.player.clear();
  session.activity.submitted(performance.now());
  session.lines = {};
  transcript(session, 'user', text, true);
  $('chat-input').value = '';
  $('chat-input').focus();
});
$('chat-input').addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
    event.preventDefault();
    $('chat-form').requestSubmit();
  }
});

const motionPreference = window.matchMedia('(prefers-reduced-motion: reduce)');
let motionEnabled = !motionPreference.matches;
function updateMotion() {
  const enabled = motionEnabled && !motionPreference.matches;
  $('beat-character').dataset.motion = enabled ? 'on' : 'off';
  $('motion-toggle').setAttribute('aria-pressed', String(enabled));
  $('motion-toggle').disabled = motionPreference.matches;
  $('motion-toggle').textContent = motionPreference.matches ? '동작 줄이기 적용 중' : enabled ? '움직임 끄기' : '움직임 켜기';
}
$('motion-toggle').addEventListener('click', () => { motionEnabled = !motionEnabled; updateMotion(); });
motionPreference.addEventListener('change', updateMotion);
updateMotion();
