import { AudioPlayer } from "./audio.js";

const VOICES = [
  ["Zephyr", "Bright"], ["Puck", "Upbeat"], ["Charon", "Informative"],
  ["Kore", "Firm"], ["Fenrir", "Excitable"], ["Leda", "Youthful"],
  ["Orus", "Firm"], ["Aoede", "Breezy"], ["Callirrhoe", "Easy-going"],
  ["Autonoe", "Bright"], ["Enceladus", "Breathy"], ["Iapetus", "Clear"],
  ["Umbriel", "Easy-going"], ["Algieba", "Smooth"], ["Despina", "Smooth"],
  ["Erinome", "Clear"], ["Algenib", "Gravelly"], ["Rasalgethi", "Informative"],
  ["Laomedeia", "Upbeat"], ["Achernar", "Soft"], ["Alnilam", "Firm"],
  ["Schedar", "Even"], ["Gacrux", "Mature"], ["Pulcherrima", "Forward"],
  ["Achird", "Friendly"], ["Zubenelgenubi", "Casual"],
  ["Vindemiatrix", "Gentle"], ["Sadachbia", "Lively"],
  ["Sadaltager", "Knowledgeable"], ["Sulafat", "Warm"],
];

const $ = (id) => document.getElementById(id);
const configForm = $("config");
const startButton = $("start");
const stopButton = $("stop");
let current = null;
let resumeHandle = null;
let resumeExpires = 0;

for (const [name, tone] of VOICES) {
  const option = document.createElement("option");
  option.value = name;
  option.textContent = `${name} — ${tone}`;
  option.selected = name === "Kore";
  $("voice").append(option);
}

function state(name, message) {
  document.body.dataset.state = name;
  $("status").textContent = {
    idle: "연결 전",
    connecting: "연결 중",
    listening: current?.mode === "chat" ? "메시지 대기 중" : "듣는 중",
    speaking: "에이전트가 말하는 중",
    error: "확인 필요",
  }[name];
  $("hint").textContent = message ?? {
    idle: "설정을 확인하고 대화를 시작하세요.",
    connecting: "로컬 중계 서버와 Gemini 세션을 준비하고 있습니다.",
    listening: current?.mode === "chat" ? "아래 입력란으로 메시지를 보내세요." : "편하게 말을 걸어보세요.",
    speaking: "답변 중에도 말하면 재생이 즉시 멈춥니다.",
    error: "안내를 확인한 뒤 다시 시작하세요.",
  }[name];
}

function notice(message = "") {
  $("notice").hidden = !message;
  $("notice").textContent = message;
}

function setConfigDisabled(disabled) {
  for (const control of configForm.elements) control.disabled = disabled;
}

function optionalNumber(id) {
  const raw = $(id).value.trim();
  return raw === "" ? null : Number(raw);
}

function settings() {
  return {
    voiceName: $("voice").value,
    topP: optionalNumber("top-p"),
    topK: optionalNumber("top-k"),
    presencePenalty: optionalNumber("presence-penalty"),
    frequencyPenalty: optionalNumber("frequency-penalty"),
    temperature: optionalNumber("temperature"),
    maxOutputTokens: optionalNumber("max-tokens"),
    startOfSpeechSensitivity: $("start-sensitivity").value,
    endOfSpeechSensitivity: $("end-sensitivity").value,
    prefixPaddingMs: Number($("prefix-padding").value),
    silenceDurationMs: Number($("silence-duration").value),
    activityHandling: $("activity-handling").value,
    inputTranscription: $("input-transcription").checked,
    outputTranscription: $("output-transcription").checked,
    systemInstruction: $("system-instruction").value.trim(),
    promptInstruction: promptText(),
  };
}

function promptText() {
  if (!$("persona-enabled").checked) return "";
  return `캐릭터 지침(행동의 목표이며 정확한 비율을 보장하지 않음):\n다정함 ${$("warmth").value}/100: 0은 담백하고 간결하게, 100은 따뜻한 공감 표현을 적극 사용.\n유머 ${$("humor").value}/100: 0은 진지하게, 100은 상황에 맞는 가벼운 농담을 자주 사용.\n답변은 대체로 ${$("sentences").value}문장 이내로 말해 주세요.\n답변 언어: ${$("reply-language").value.trim() || "사용자의 언어"}.`;
}

configForm.addEventListener("input", () => {
  $("warmth-value").textContent = $("warmth").value;
  $("humor-value").textContent = $("humor").value;
  $("prompt-preview").value = promptText();
});

function transcript(session, role, text) {
  if (!text) return;
  const log = $("transcript");
  log.querySelector(".empty")?.remove();
  if (!session.lines[role]) {
    const entry = document.createElement("div");
    entry.className = `utterance ${role}`;
    const label = document.createElement("strong");
    label.textContent = role === "user" ? "나" : "에이전트";
    const line = document.createElement("p");
    entry.append(label, line);
    log.append(entry);
    session.lines[role] = line;
    while (log.children.length > 100) log.firstElementChild.remove();
  }
  session.lines[role].textContent += text;
  log.scrollTop = log.scrollHeight;
}

function endSuppression(session) {
  clearTimeout(session.suppressTimer);
  session.suppressed = false;
}

function suppressOldAudio(session) {
  session.suppressed = true;
  clearTimeout(session.suppressTimer);
  session.suppressTimer = setTimeout(() => {
    session.suppressed = false;
  }, 3000);
}

function finish(session, message = "") {
  if (session.done) return;
  session.done = true;
  clearTimeout(session.connectTimer);
  endSuppression(session);
  session.abort.abort();
  session.stream?.getTracks().forEach((track) => track.stop());
  if (session.worklet) {
    session.worklet.port.onmessage = null;
    session.worklet.port.close();
    session.worklet.disconnect();
  }
  session.source?.disconnect();
  session.player?.clear();
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
    setConfigDisabled(false);
    startButton.disabled = false;
    stopButton.disabled = true;
    $("end-audio").disabled = true;
    $("resume").disabled = !resumeHandle || Date.now() >= resumeExpires;
    $("chat-input").disabled = true;
    $("send").disabled = true;
    $("level").value = 0;
    $("mic-state").textContent = "꺼짐";
    state(message ? "error" : "idle", message || "대화가 끝났습니다.");
    notice(message);
  }
}

function receive(session, event) {
  if (session.done) return;
  try {
    const message = JSON.parse(event.data);
    if (message.type === "resumption") {
      if (message.update.resumable && message.update.newHandle) {
        resumeHandle = message.update.newHandle;
        resumeExpires = Date.now() + 2 * 60 * 60 * 1000;
      }
      return;
    }
    if (message.type === "error") return finish(session, message.message);
    if (message.type === "notice") return notice(message.message);
    if (message.type === "usage") {
      const total = message.usage?.totalTokenCount;
      if (Number.isFinite(total)) $("usage").textContent = `누적 토큰 ${total.toLocaleString()}`;
      return;
    }
    if (message.type === "ready") {
      clearTimeout(session.connectTimer);
      session.ready = true;
      state("listening");
      $("model").textContent = message.model;
      $("mic-state").textContent = session.mode === "chat" ? "사용 안 함" : "켜짐";
      $("chat-input").disabled = false;
      $("send").disabled = false;
      $("end-audio").disabled = session.mode !== "voice";
      if (session.mode === "chat") $("chat-input").focus();
      return;
    }
    if (message.type !== "content") return;

    const content = message.content;
    const interrupted = content.interrupted === true;
    if (interrupted) {
      session.player.clear();
      endSuppression(session);
      session.lines = {};
      notice("끼어들기를 감지해 이전 응답 재생을 비웠습니다.");
    }
    for (const [key, role] of [["inputTranscription", "user"], ["outputTranscription", "agent"]]) {
      if (content[key]?.text) transcript(session, role, content[key].text);
    }
    if (!interrupted) {
      for (const part of content.modelTurn?.parts ?? []) {
        if (part.inlineData && !session.suppressed) {
          session.player.enqueue(part.inlineData.data, part.inlineData.mimeType);
        }
      }
    }
    if (content.turnComplete) {
      endSuppression(session);
      session.lines = {};
    }
  } catch {
    finish(session, "오디오 응답을 처리하지 못했습니다. 다시 시작해 주세요.");
  }
}

async function start(resuming = false) {
  if (current) return;
  if (!configForm.reportValidity()) return;
  if (resuming && (!resumeHandle || Date.now() >= resumeExpires)) {
    $("resume").disabled = true;
    return notice("복구 지점이 만료되었습니다. 새 대화를 시작해 주세요.");
  }
  if (!resuming) resumeHandle = null;
  const setupSettings = settings();
  if (setupSettings.systemInstruction.length > 8000) return notice("시스템 지시문과 생성 문장을 합쳐 8,000자 이내로 줄여 주세요.");
  const session = {
    done: false,
    ready: false,
    lines: {},
    abort: new AbortController(),
    mode: document.querySelector('input[name="mode"]:checked').value,
    localThreshold: Number($("local-threshold").value),
    loudFrames: 0,
    suppressed: false,
  };
  current = session;
  setConfigDisabled(true);
  startButton.disabled = true;
  stopButton.disabled = false;
  $("resume").disabled = true;
  notice();
  state("connecting");
  if (!resuming) $("transcript").replaceChildren();
  $("usage").textContent = "토큰 집계 대기";
  session.connectTimer = setTimeout(
    () => finish(session, "연결 시간이 초과되었습니다. 서버와 네트워크를 확인해 주세요."),
    45000,
  );

  try {
    if (session.mode === "voice" && (!navigator.mediaDevices?.getUserMedia || !window.AudioWorkletNode)) {
      throw new Error("최신 Chrome에서 localhost 주소로 접속해 주세요.");
    }
    session.context = new AudioContext();
    await session.context.resume();
    const health = await fetch("/api/health", { signal: session.abort.signal });
    if (!health.ok) throw new Error("로컬 서버 연결을 확인해 주세요.");
    const config = await health.json();
    if (!config.configured) throw new Error("master/.env에 GEMINI_API_KEY를 설정해 주세요.");
    if (session.done) return;

    session.player = new AudioPlayer(session.context, (speaking) => {
      if (!session.done && session.ready) state(speaking ? "speaking" : "listening");
    });

    if (session.mode === "voice") {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });
      if (session.done) {
        stream.getTracks().forEach((track) => track.stop());
        return;
      }
      session.stream = stream;
      stream.getAudioTracks()[0].addEventListener("ended", () => {
        finish(session, "마이크 연결이 끊겼습니다. 장치를 확인해 주세요.");
      });
      await session.context.audioWorklet.addModule("/pcm-worklet.js");
      session.source = session.context.createMediaStreamSource(stream);
      session.worklet = new AudioWorkletNode(session.context, "microphone-pcm");
      session.worklet.onprocessorerror = () => {
        finish(session, "마이크 오디오 처리에 실패했습니다.");
      };
      session.source.connect(session.worklet);
      session.worklet.connect(session.context.destination);
    }

    const socket = new WebSocket(
      `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`,
    );
    session.socket = socket;
    socket.onopen = () => {
      if (!session.done) socket.send(JSON.stringify({ type: "start", settings: setupSettings, handle: resuming ? resumeHandle : null }));
    };
    socket.onmessage = (event) => receive(session, event);
    socket.onerror = () => finish(session, "로컬 중계 서버 연결에 실패했습니다.");
    socket.onclose = () => finish(session, "연결이 종료되었습니다. 새 세션을 시작해 주세요.");

    if (session.worklet) {
      session.worklet.port.onmessage = ({ data }) => {
        if (session.done || !session.ready || socket.readyState !== WebSocket.OPEN) return;
        $("level").value = Math.min(1, data.level * 5);

        if (setupSettings.activityHandling !== "NO_INTERRUPTION" && session.player.speaking && session.localThreshold > 0 && data.level > session.localThreshold) {
          session.loudFrames++;
          if (session.loudFrames >= 3 && !session.suppressed) {
            session.player.clear();
            suppressOldAudio(session);
            notice("로컬 마이크가 끼어들기를 먼저 감지했습니다.");
          }
        } else {
          session.loudFrames = 0;
        }

        if (socket.bufferedAmount > 64000) {
          finish(session, "음성 전송이 지연되었습니다. 네트워크를 확인해 주세요.");
          return;
        }
        socket.send(data.pcm);
      };
    }
  } catch (error) {
    if (session.done) return;
    const message = {
      NotAllowedError: "마이크 권한이 필요합니다. 브라우저 사이트 설정에서 허용해 주세요.",
      NotFoundError: "마이크를 찾지 못했습니다.",
      NotReadableError: "마이크를 사용할 수 없습니다. 다른 앱의 사용 여부를 확인해 주세요.",
    }[error.name] ?? error.message ?? "연결에 실패했습니다.";
    finish(session, message);
  }
}

startButton.addEventListener("click", () => start());
$("resume").addEventListener("click", () => start(true));
$("end-audio").addEventListener("click", () => {
  if (current?.ready && current.socket.readyState === WebSocket.OPEN) {
    current.socket.send(JSON.stringify({ type: "audioStreamEnd" }));
    notice("발화 종료 신호를 보냈습니다. 다음 오디오는 새 입력으로 이어집니다.");
  }
});
stopButton.addEventListener("click", () => current && finish(current));
window.addEventListener("pagehide", () => current && finish(current));

$("chat-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const session = current;
  const text = $("chat-input").value.trim();
  if (!text || !session?.ready || session.done || session.socket.readyState !== WebSocket.OPEN) return;
  if (session.player.speaking) {
    session.player.clear();
    suppressOldAudio(session);
  }
  session.socket.send(JSON.stringify({ type: "text", text }));
  session.lines = {};
  transcript(session, "user", text);
  session.lines = {};
  $("chat-input").value = "";
  $("chat-input").focus();
});

$("chat-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
    event.preventDefault();
    $("chat-form").requestSubmit();
  }
});

$("local-threshold").addEventListener("input", () => {
  $("local-threshold-value").textContent = Number($("local-threshold").value).toFixed(3);
});

state("idle");
