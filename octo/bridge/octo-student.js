// Loaded only into deployment copies; the student's audio and display code still runs.
(() => {
  window.GEMINI_KEY = window.OCTO_CONFIG.key;
  window.LOCAL_CONFIG = { apiKey: window.OCTO_CONFIG.key };
  window.OctoStudent = {
    connect(hooks) {
      const bridge = window.LiveBridge;
      let initializing = false;
      let injecting = false;
      const send = (payload) => hooks.socket().send(JSON.stringify(payload));
      bridge.connect({
        async start() {
          initializing = true;
          try {
            await hooks.start();
            const ws = hooks.socket();
            if (!ws) throw new Error("학생 세션을 시작하지 못함 (키·마이크·로그 확인)");
            const original = ws.send.bind(ws);
            ws.send = (payload) => {
              if (typeof payload === "string") {
                const msg = JSON.parse(payload);
                if (initializing && (msg.clientContent || msg.realtimeInput?.text)) return;
                if (msg.setup && window.OCTO_CONFIG.prompt !== null) {
                  msg.setup.systemInstruction = { parts: [{ text: window.OCTO_CONFIG.prompt }] };
                  payload = JSON.stringify(msg);
                }
                if (!injecting && msg.realtimeInput?.audio && ws.octoInjecting) return;
              } else if (!injecting && ws.octoInjecting) return;
              original(payload);
            };
            ws.addEventListener("close", () => bridge.ended());
            const until = Date.now() + 45000;
            while (!hooks.ready()) {
              if (ws.readyState >= WebSocket.CLOSING || Date.now() > until) throw new Error("Live 세션 준비 실패");
              await new Promise((resolve) => setTimeout(resolve, 100));
            }
          } catch (error) {
            await hooks.stop();
            throw error;
          } finally {
            initializing = false;
          }
        },
        stop: () => hooks.stop(),
        async say(text, audio) {
          const ws = hooks.socket();
          ws.octoInjecting = true;
          injecting = true;
          try {
            send(hooks.relay ? { type: "text", text } : { realtimeInput: { text } });
            if (audio) {
              for (const data of audio.chunks) {
                if (hooks.relay) ws.send(Uint8Array.from(atob(data), (c) => c.charCodeAt(0)));
                else send({ realtimeInput: { audio: { data, mimeType: audio.mimeType } } });
              }
              send(hooks.relay ? { type: "audioStreamEnd" } : { realtimeInput: { audioStreamEnd: true } });
            }
          } finally {
            injecting = false;
            // Keep live microphone packets out of the forwarded visitor's input turn.
            await new Promise((resolve) => setTimeout(resolve, 200));
            ws.octoInjecting = false;
          }
        },
      });
      bridge.on("connected", async () => {
        try { bridge.prompt(await hooks.prompt()); } catch (error) { bridge.log(error.message); }
      });
      let last = false;
      setInterval(() => {
        const on = hooks.ready() && hooks.speaking();
        if (on !== last) { last = on; bridge.speaking(on); }
      }, 100);
      // Built-in retry lives in LiveBridge; there is no student-specific reconnect loop.
    },
  };
})();
