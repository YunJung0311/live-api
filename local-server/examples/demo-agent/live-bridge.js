// live-bridge.js — 학생 페이지 ↔ 라즈베리 파이 agent 연결 (중앙 제어용)
//
// 1) 이 파일을 자기 프로젝트 폴더에 복사하고 index.html에 추가:
//      <script src="live-bridge.js"></script>
// 2) 자기 코드에서 세 가지 함수만 알려주면 됨:
//      LiveBridge.connect({
//        start: async () => { /* 세션 열고 마이크 켜기. 준비(setupComplete)되면 resolve */ },
//        stop:  async () => { /* 세션 닫고 마이크 끄기 */ },
//        say:   (text) => { /* 열린 세션에 텍스트 보내기 (realtimeInput.text) */ },
//      });
// 3) 선택: 말하기 시작/끝날 때 LiveBridge.speaking(true/false), 세션이 혼자 끊기면 LiveBridge.ended()
//
// 맥에서 혼자 개발할 때는 agent가 없으니 조용히 아무 일도 안 함 (기존 버튼 그대로 쓰면 됨).
(function () {
  const URL = "ws://127.0.0.1:8765/bridge";
  const listeners = {};
  let socket = null;
  let handlers = null;
  let running = false;
  let queue = Promise.resolve();

  const bridge = {
    connected: false,
    id: null,
    name: null,
    role: null,
    mode: "idle",
    get running() {
      return running;
    },

    connect(h) {
      handlers = h;
      open();
      return bridge;
    },
    on(type, fn) {
      (listeners[type] ||= []).push(fn);
      return bridge;
    },
    speaking(on) {
      send({ type: "voice_state", state: on ? "speaking" : running ? "listening" : "sleep" });
    },
    ended() {
      running = false;
      send({ type: "voice_state", state: "sleep" });
    },
    log(...parts) {
      send({ type: "log", text: parts.map(String).join(" ") });
    },
    // 마스터 전용: 관람객 말에서 주제를 뽑으면 호출
    startChat(topic, utterance = "") {
      send({ type: "event", name: "start_chat", topic, utterance });
    },
    stopChat() {
      send({ type: "event", name: "stop_chat" });
    },
  };

  function send(msg) {
    if (socket && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(msg));
  }

  function emit(type, msg) {
    for (const fn of listeners[type] || []) {
      try {
        fn(msg);
      } catch (error) {
        bridge.log("listener error", error);
      }
    }
  }

  async function wake() {
    if (running || !handlers?.start) return;
    await handlers.start();
    running = true;
    send({ type: "voice_state", state: "listening" });
  }

  async function sleep() {
    if (!running || !handlers?.stop) return;
    running = false;
    await handlers.stop();
    send({ type: "voice_state", state: "sleep" });
  }

  const actions = {
    hello(msg) {
      Object.assign(bridge, { id: msg.id, name: msg.name, role: msg.role });
    },
    wake,
    sleep,
    async kickoff(msg) {
      await wake();
      handlers?.say?.(msg.text);
    },
    mode(msg) {
      bridge.mode = msg.mode;
    },
    identify(msg) {
      identify(msg.name || bridge.name);
    },
  };

  function handle(msg) {
    const action = actions[msg.type];
    queue = queue
      .then(() => action?.(msg))
      .then(() => emit(msg.type, msg))
      .catch((error) => bridge.log(`${msg.type} 실패:`, error?.message || error));
  }

  function open() {
    socket = new WebSocket(URL);
    socket.onopen = () => {
      bridge.connected = true;
      emit("connected", {});
    };
    socket.onmessage = (event) => handle(JSON.parse(event.data));
    socket.onclose = () => {
      bridge.connected = false;
      setTimeout(open, 2000);
    };
    socket.onerror = () => socket.close();
  }

  // 페이지 에러를 대시보드 로그로 보냄 (원격 디버깅용)
  for (const level of ["warn", "error"]) {
    const original = console[level].bind(console);
    console[level] = (...args) => {
      original(...args);
      bridge.log(`[console.${level}]`, ...args);
    };
  }
  window.addEventListener("error", (e) => bridge.log("[error]", e.message, `${e.filename}:${e.lineno}`));
  window.addEventListener("unhandledrejection", (e) => bridge.log("[promise]", e.reason?.message || e.reason));

  // 대시보드의 "Identify" 버튼: 어떤 기기인지 화면+소리로 알려줌
  function identify(name) {
    const box = document.createElement("div");
    box.textContent = name;
    box.style.cssText =
      "position:fixed;inset:0;z-index:2147483647;display:grid;place-items:center;" +
      "background:#ff0;color:#000;font:700 12vmin/1.1 system-ui;text-align:center";
    document.body.append(box);
    setTimeout(() => box.remove(), 5000);
    try {
      const ctx = new AudioContext();
      const osc = ctx.createOscillator();
      osc.frequency.value = 880;
      osc.connect(ctx.destination);
      osc.start();
      osc.stop(ctx.currentTime + 0.4);
    } catch {
      // 소리는 없어도 화면 표시는 됨
    }
  }

  window.LiveBridge = bridge;
})();
