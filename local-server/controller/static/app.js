const $ = (selector, root = document) => root.querySelector(selector);
const cards = new Map();
let state = null;

async function api(path, { method = "GET", body } = {}) {
  const init = { method, headers: {} };
  if (body instanceof FormData) {
    init.body = body;
  } else if (body !== undefined) {
    init.body = JSON.stringify(body);
    init.headers["Content-Type"] = "application/json";
  }
  const response = await fetch(path, init);
  if (!response.ok) throw new Error((await response.text()) || response.statusText);
  return response.json();
}

function toast(text, bad = false) {
  const box = document.createElement("div");
  box.className = `toast${bad ? " bad" : ""}`;
  box.textContent = text;
  $("#toasts").append(box);
  setTimeout(() => box.remove(), bad ? 7000 : 3000);
}

async function run(task, done) {
  try {
    await task();
    if (done) toast(done);
  } catch (error) {
    toast(error.message, true);
  }
}

function deviceName(id) {
  return state?.devices.find((d) => d.id === id)?.name || id;
}

function setValue(el, value) {
  if (document.activeElement !== el) el.value = value ?? "";
}

function syncOptions(select, options) {
  const same = select.options.length === options.length && [...select.options].every((o, i) => o.value === options[i][0]);
  if (!same) select.replaceChildren(...options.map(([value, label]) => new Option(label, value)));
}

// ---------- 실시간 연결 ----------

function connect() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/dashboard`);
  ws.onopen = () => ($("#conn").textContent = "");
  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.type === "state") render(msg.state);
    if (msg.type === "log") appendLog(msg.device, msg.line);
    if (msg.type === "result" && msg.command !== "get_prompt") {
      toast(`${deviceName(msg.device)} · ${msg.command} ${msg.ok ? "완료" : `실패: ${msg.message || ""}`}`, !msg.ok);
    }
  };
  ws.onclose = () => {
    $("#conn").textContent = "서버 연결 끊김 — 재연결 중…";
    setTimeout(connect, 2000);
  };
}

// ---------- 그리기 ----------

function render(next) {
  state = next;
  const online = state.devices.filter((d) => d.online).length;
  $("#count").textContent = `${online} / ${state.devices.length} online`;
  $("#install").textContent = state.install;
  $("#empty").hidden = state.devices.length > 0;
  $("#default-key").textContent = state.settings.default_key ? `(${state.settings.default_key})` : "(없음)";
  renderShow();

  const devices = [...state.devices].sort(
    (a, b) => (a.role === "master" ? -1 : 0) - (b.role === "master" ? -1 : 0) || a.name.localeCompare(b.name),
  );
  const list = $("#devices");
  for (const device of devices) {
    if (!cards.has(device.id)) cards.set(device.id, createCard(device.id));
    const card = cards.get(device.id);
    list.append(card);
    updateCard(card, device);
  }
  for (const [id, card] of cards) {
    if (!state.devices.some((d) => d.id === id)) {
      card.remove();
      cards.delete(id);
    }
  }
}

function renderShow() {
  const { show, settings } = state;
  const mode = $("#mode");
  mode.textContent = show.mode;
  mode.className = `badge ${show.mode}`;
  $("#show-info").textContent =
    show.mode === "chat"
      ? `주제 “${show.topic}” · 첫 발언자 ${deviceName(show.target)} · ${Math.round(Date.now() / 1000 - show.started)}초 전 시작`
      : "대기 중 — 마스터만 마이크가 켜져 있음";

  const target = $("#target");
  syncOptions(target, [["random", "랜덤"], ...state.devices.filter((d) => d.role === "agent").map((d) => [d.id, d.name])]);
  setValue(target, settings.target);
  setValue($("#others_delay"), settings.others_delay);
  setValue($("#max_seconds"), settings.max_seconds);
  setValue($("#kickoff"), settings.kickoff);
}

function createCard(id) {
  const card = $("#card").content.firstElementChild.cloneNode(true);
  card.dataset.id = id;
  const field = (name) => $(`[data-f="${name}"]`, card);

  field("name").addEventListener("change", (e) => run(() => updateDevice(id, { name: e.target.value })));
  field("role").addEventListener("change", (e) => run(() => updateDevice(id, { role: e.target.value })));
  field("project").addEventListener("change", (e) =>
    run(() => updateDevice(id, { project: e.target.value }), "프로젝트 지정됨 — Deploy를 누르면 설치"),
  );
  field("more").addEventListener("toggle", (e) => {
    if (e.target.open) openDetails(card, id);
  });
  card.addEventListener("click", (e) => {
    const action = e.target.closest("[data-a]")?.dataset.a;
    if (action) run(() => cardAction(card, id, action));
  });
  return card;
}

function updateCard(card, device) {
  const field = (name) => $(`[data-f="${name}"]`, card);
  const s = device.status || {};
  card.classList.toggle("online", device.online);
  card.classList.toggle("offline", !device.online);
  card.classList.toggle("target", state.show.target === device.id);
  setValue(field("name"), device.name);
  setValue(field("role"), device.role);

  const meta = [s.hostname, s.ip, s.temp != null && `${s.temp}°C`, s.load != null && `load ${s.load}`, s.version && `v${s.version}`];
  field("meta").textContent = device.online ? meta.filter(Boolean).join(" · ") : `offline · ${device.id}`;

  const project = field("project");
  syncOptions(project, [["", "— 프로젝트 선택 —"], ...state.projects.map((p) => [p.id, p.label])]);
  setValue(project, device.project || "");

  const phase = device.online ? s.state || "?" : "offline";
  field("state").textContent = phase;
  field("state").className = `badge ${phase}`;
  const voice = device.online && s.bridge ? s.voice : "no bridge";
  field("voice").textContent = device.online ? `mic: ${voice}` : "";
  field("voice").className = `badge ${voice}`;
  field("voice").hidden = !device.online;

  field("key").textContent = device.key ? `(${device.key})` : "(공용 키 사용)";
  field("prompt-note").textContent = device.custom_prompt ? "· 대시보드에서 바꾼 프롬프트 적용 중" : "";
  for (const button of card.querySelectorAll(".row > button[data-a]")) {
    if (["start", "stop", "restart", "deploy", "identify"].includes(button.dataset.a)) button.disabled = !device.online;
  }
}

async function openDetails(card, id) {
  const device = state.devices.find((d) => d.id === id);
  const { lines } = await api(`/api/devices/${id}/logs`);
  const pre = $('[data-f="logs"]', card);
  pre.textContent = lines.join("\n");
  pre.scrollTop = pre.scrollHeight;
  await showManifest(card, device);
}

async function showManifest(card, device) {
  const box = $('[data-f="manifest"]', card);
  const note = $('[data-f="manifest-note"]', card);
  if (device.manifest) {
    box.value = JSON.stringify(device.manifest, null, 2);
    note.textContent = "· 직접 수정한 값";
  } else if (device.project) {
    const guess = await api(`/api/manifest?project=${encodeURIComponent(device.project)}`);
    box.value = JSON.stringify(guess, null, 2);
    note.textContent = "· live.json 또는 자동 추측";
  } else {
    box.value = "";
    note.textContent = "· 프로젝트를 먼저 선택";
  }
}

function appendLog(id, line) {
  const pre = cards.get(id)?.querySelector('[data-f="logs"]');
  if (!pre) return;
  const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 20;
  pre.textContent += (pre.textContent ? "\n" : "") + line;
  if (atBottom) pre.scrollTop = pre.scrollHeight;
}

// ---------- 동작 ----------

function updateDevice(id, patch) {
  return api(`/api/devices/${id}`, { method: "POST", body: patch });
}

async function cardAction(card, id, action) {
  const field = (name) => $(`[data-f="${name}"]`, card);
  const device = state.devices.find((d) => d.id === id);
  switch (action) {
    case "key-save":
      await updateDevice(id, { key: field("key-input").value });
      field("key-input").value = "";
      await api(`/api/devices/${id}/restart`, { method: "POST" });
      return toast("키 저장 · 재시작 중");
    case "prompt-load": {
      const { prompt } = await api(`/api/devices/${id}/prompt`);
      field("prompt").value = prompt;
      return;
    }
    case "prompt-save":
      await updateDevice(id, { prompt: field("prompt").value });
      await api(`/api/devices/${id}/restart`, { method: "POST" });
      return toast("프롬프트 저장 · 재시작 중");
    case "prompt-reset":
      if (!confirm("대시보드에서 바꾼 프롬프트를 버리고 프로젝트 원래 파일로 돌릴까요? (Deploy 필요)")) return;
      await updateDevice(id, { prompt: null });
      return toast("원래대로 — Deploy하면 적용됨");
    case "manifest-save": {
      const manifest = JSON.parse(field("manifest").value);
      await updateDevice(id, { manifest });
      return toast("manifest 저장 — Deploy하면 적용됨");
    }
    case "manifest-auto":
      await updateDevice(id, { manifest: null });
      return showManifest(card, { ...device, manifest: null });
    case "forget":
      if (!confirm(`${device.name}을(를) 목록에서 지울까요?`)) return;
      return api(`/api/devices/${id}`, { method: "DELETE" });
    case "reboot":
      if (!confirm(`${device.name} 재부팅?`)) return;
      break;
  }
  await api(`/api/devices/${id}/${action}`, { method: "POST" });
  toast(`${device.name} · ${action} 보냄`);
}

document.querySelectorAll("[data-all]").forEach((button) =>
  button.addEventListener("click", () => {
    const action = button.dataset.all;
    if (["reboot", "poweroff", "deploy"].includes(action) && !confirm(`정말 전체 ${action}?`)) return;
    run(async () => {
      const { sent, skipped } = await api(`/api/all/${action}`, { method: "POST" });
      toast(`${action}: ${sent.length}대 보냄${skipped.length ? ` · ${skipped.length}대 건너뜀(프로젝트 없음/오프라인)` : ""}`);
    });
  }),
);

$("#chat-start").addEventListener("click", () =>
  run(() => api("/api/show/start", { method: "POST", body: { topic: $("#topic").value } }), "대화 시작"),
);
$("#topic").addEventListener("keydown", (e) => e.key === "Enter" && $("#chat-start").click());
$("#chat-stop").addEventListener("click", () => run(() => api("/api/show/stop", { method: "POST" }), "그만!"));

$("#settings-save").addEventListener("click", () =>
  run(
    () =>
      api("/api/settings", {
        method: "POST",
        body: {
          target: $("#target").value,
          others_delay: Number($("#others_delay").value),
          max_seconds: Number($("#max_seconds").value),
          kickoff: $("#kickoff").value,
        },
      }),
    "쇼 설정 저장",
  ),
);
$("#target").addEventListener("change", (e) =>
  run(() => api("/api/settings", { method: "POST", body: { target: e.target.value } })),
);

$("#default-key-save").addEventListener("click", () =>
  run(async () => {
    await api("/api/settings", { method: "POST", body: { default_key: $("#default-key-input").value } });
    $("#default-key-input").value = "";
  }, "공용 키 저장 — 각 기기 Restart 필요"),
);

$("#install-copy").addEventListener("click", () =>
  run(() => navigator.clipboard.writeText(state.install), "복사됨"),
);

$("#upload").addEventListener("click", () =>
  run(async () => {
    const file = $("#upload-file").files[0];
    const name = $("#upload-name").value.trim();
    if (!file || !name) throw new Error("이름과 zip 파일을 둘 다 넣어주세요");
    const form = new FormData();
    form.append("name", name);
    form.append("file", file);
    await api("/api/upload", { method: "POST", body: form });
  }, "업로드 완료 — 기기 카드의 프로젝트 목록에 생김"),
);

setInterval(() => state?.show.mode === "chat" && renderShow(), 1000);
connect();
