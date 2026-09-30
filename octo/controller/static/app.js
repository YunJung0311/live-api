const $ = (selector, root = document) => root.querySelector(selector);
const cards = new Map();
let state = null;

const QUIET = new Set(["get_prompt", "get_logs", "audio", "ping"]);
const SKIP_DIRS = new Set([".git", "node_modules", ".venv", "venv", "__pycache__", "__MACOSX"]);
const JUNK = new Set([".DS_Store", "Thumbs.db", "desktop.ini"]);
const DROP_HINT = "← 카드에 끌어다 놓아도 됨 · 올리면 이 Pi에 바로 설치";

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
  const same =
    select.options.length === options.length &&
    [...select.options].every((o, i) => o.value === options[i][0] && o.text === options[i][1]);
  if (!same) select.replaceChildren(...options.map(([value, label]) => new Option(label, value)));
}

// http:// 주소(보안 컨텍스트 아님)에서는 navigator.clipboard가 없어서 예전 방식으로 복사
async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
  const box = document.createElement("textarea");
  box.value = text;
  box.style.cssText = "position:fixed;top:0;left:0;opacity:0";
  document.body.append(box);
  box.select();
  const ok = typeof document.execCommand === "function" && document.execCommand("copy");
  box.remove();
  if (!ok) throw new Error("복사 실패 — 글자를 직접 드래그해서 복사하세요");
}

// ---------- 실시간 연결 ----------

function connect() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/dashboard`);
  ws.onopen = () => ($("#conn").textContent = "");
  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.type === "state") render(msg.state);
    if (msg.type === "log") appendLog(msg.device, msg.line);
    if (msg.type === "result" && !QUIET.has(msg.command)) {
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
  $("#install-ip").textContent = state.install_ip || "";
  $("#install-ip-box").hidden = !state.install_ip;
  $("#join").textContent = state.join;
  $("#join-ip").textContent = state.join_ip || "";
  $("#join-ip-box").hidden = !state.join_ip;
  renderPairing();
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
    show.mode !== "idle"
      ? `주제 “${show.topic}” · 첫 발언자 ${deviceName(show.target)} · ${show.active?.length || 0}대 활성 · ${show.mode === "fading" ? "순차 종료 중" : show.phase === "starting" ? "첫 발언자 준비 중" : "대화 중"}`
      : "대기 중 — 마스터만 마이크가 켜져 있음";

  const target = $("#target");
  syncOptions(target, [["random", "랜덤"], ...state.devices.filter((d) => d.role === "agent").map((d) => [d.id, d.name])]);
  setValue(target, settings.target);
  setValue($("#others_delay"), settings.others_delay);
  setValue($("#max_seconds"), settings.max_seconds);
  setValue($("#fade_seconds"), settings.fade_seconds);
  setValue($("#kickoff"), settings.kickoff);
  setValue($("#screen_rotate"), settings.screen_rotate);
  if (document.activeElement !== $("#screen_only")) $("#screen_only").checked = !!settings.screen_only;
  const rot = settings.screen_rotate === "keep" ? "방향 그대로" : `${settings.screen_rotate}°`;
  $("#screen-summary").textContent = `(${rot}${settings.screen_only ? " · 모니터 끔" : ""})`;
}

function renderPairing() {
  const list = state.pairing || [];
  $("#pairing").hidden = !list.length;
  const rows = list.map((p) => {
    const row = document.createElement("div");
    row.className = "row pair";
    const label = document.createElement("span");
    label.className = "pair-name";
    const ago = Math.max(0, Math.round(Date.now() / 1000 - p.since));
    label.textContent = `${p.name} · ${p.ip} · ${ago}초 전${p.known ? ` · 전에 연결됐던 기기 (${p.known})` : ""}`;
    const approve = document.createElement("button");
    approve.className = "primary";
    approve.textContent = "허용";
    approve.dataset.pair = "approve";
    approve.dataset.id = p.id;
    const deny = document.createElement("button");
    deny.className = "danger";
    deny.textContent = "거절";
    deny.dataset.pair = "deny";
    deny.dataset.id = p.id;
    row.append(label, approve, deny);
    return row;
  });
  $("#pairing-list").replaceChildren(...rows);
}

function createCard(id) {
  const card = $("#card").content.firstElementChild.cloneNode(true);
  card.dataset.id = id;
  const field = (name) => $(`[data-f="${name}"]`, card);

  field("name").addEventListener("change", (e) => run(() => updateDevice(id, { name: e.target.value })));
  field("project").addEventListener("change", (e) =>
    run(async () => {
      await updateDevice(id, { project: e.target.value });
      if (e.target.value) await api(`/api/devices/${id}/deploy`, { method: "POST" });
    }, "작품 변경 요청됨 — 오프라인이면 연결될 때 설치"),
  );
  field("more").addEventListener("toggle", (e) => {
    if (e.target.open) openDetails(card, id);
  });
  card.addEventListener("click", (e) => {
    const action = e.target.closest("[data-a]")?.dataset.a;
    if (action) run(() => cardAction(card, id, action));
  });

  // 작품 올리기: 버튼(파일/폴더 고르기) 또는 카드에 끌어다 놓기
  field("zip-input").addEventListener("change", (e) => {
    const file = e.target.files[0];
    e.target.value = "";
    if (file) run(() => uploadToDevice(card, id, [{ file }]));
  });
  field("dir-input").addEventListener("change", (e) => {
    const items = filesFromInput(e.target);
    e.target.value = "";
    run(() => uploadToDevice(card, id, items));
  });
  card.addEventListener("dragover", (e) => {
    if (![...e.dataTransfer.types].includes("Files")) return;
    e.preventDefault();
    card.classList.add("drop");
  });
  card.addEventListener("dragleave", (e) => {
    if (!card.contains(e.relatedTarget)) card.classList.remove("drop");
  });
  card.addEventListener("drop", (e) => {
    e.preventDefault();
    card.classList.remove("drop");
    const dropped = takeDrop(e); // drop 이벤트 안에서 바로 꺼내야 함
    run(async () => uploadToDevice(card, id, await collectDrop(dropped)));
  });

  for (const name of ["sink-vol", "source-vol"]) {
    field(name).addEventListener("input", () => (field(`${name}-text`).textContent = `${field(name).value}%`));
  }
  return card;
}

// 카드 둘째 줄: 작품이 뜨는 화면 + 스피커/마이크
function hwLine(device) {
  const s = device.status || {};
  if (!device.online) return "";
  const parts = [];
  if (s.screens?.length) {
    const target = s.screens.find((d) => d.name === s.screen);
    const others = s.screens.filter((d) => d !== target && d.enabled);
    if (target) {
      const small = target.w * target.h <= 800 * 600;
      parts.push(`작품 화면: ${target.name} ${target.w}×${target.h}${small ? "" : " — 2인치 화면이 안 보임 (드라이버 확인)"}`);
    } else {
      parts.push("작품 화면: 기본 화면");
    }
    if (others.length) parts.push(`모니터: ${others.map((d) => d.name).join(", ")}`);
  } else if (s.screen_error) {
    parts.push(`화면 정보 없음 (${s.screen_error})`);
  }
  if (s.audio?.error) parts.push("소리 장치 정보 없음");
  else if (s.audio) parts.push(`스피커: ${s.audio.speaker || "없음"} · 마이크: ${s.audio.mic || "없음"}`);
  return parts.join(" · ");
}

function updateCard(card, device) {
  const field = (name) => $(`[data-f="${name}"]`, card);
  const s = device.status || {};
  card.classList.toggle("online", device.online);
  card.classList.toggle("offline", !device.online);
  card.classList.toggle("target", state.show.target === device.id);
  setValue(field("name"), device.name);
  field("role").textContent = device.role === "master" ? "마스터 · 입력 전용" : "슬레이브";
  field("project").disabled = device.role === "master";
  for (const action of ["upload-zip", "upload-folder"]) $(`[data-a="${action}"]`, card).hidden = device.role === "master";

  const meta = [s.hostname, s.ip, s.temp != null && `${s.temp}°C`, s.load != null && `load ${s.load}`, s.version && `v${s.version}`];
  const pending = device.deploy_pending ? " · 올려둔 작품: 켜지면 자동 설치" : "";
  const local = device.online && s.project?.startsWith("local:") ? ` · Pi에서 직접 올린 작품: ${s.project.slice(6)}` : "";
  field("meta").textContent = (device.online ? meta.filter(Boolean).join(" · ") : `offline · ${device.id}`) + pending + local;
  field("hw").textContent = hwLine(device);
  field("hw").hidden = !field("hw").textContent;

  const screens = s.screens || [];
  const screen = device.screen || { target: "auto", rotate: "default" };
  const targets = [
    ["auto", "자동 (제일 작은 화면)"],
    ...screens.map((d) => [d.name, `${d.name} · ${d.w}×${d.h}${d.enabled ? "" : " (꺼짐)"}`]),
    ["off", "옮기지 않음 (기본 화면)"],
  ];
  if (!targets.some(([value]) => value === screen.target)) targets.splice(1, 0, [screen.target, `${screen.target} (지금 안 보임)`]);
  syncOptions(field("screen-target"), targets);
  setValue(field("screen-target"), screen.target);
  setValue(field("screen-rotate"), screen.rotate);
  field("screen-note").textContent = s.screen ? `· 지금 ${s.screen}에 띄움` : "";

  const project = field("project");
  syncOptions(project, device.role === "master" ? [["builtin:master", "내장 마스터 입력기"]] : [["", "— 프로젝트 선택 —"], ...state.projects.map((p) => [p.id, p.label])]);
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
  const needsOnline = ["start", "stop", "restart", "deploy", "identify", "audio-load", "audio-save", "audio-test-speaker", "audio-test-mic"];
  for (const button of card.querySelectorAll("button[data-a]")) {
    if (needsOnline.includes(button.dataset.a)) button.disabled = !device.online;
  }
}

async function openDetails(card, id) {
  const device = state.devices.find((d) => d.id === id);
  const { lines } = await api(`/api/devices/${id}/logs`);
  const pre = $('[data-f="logs"]', card);
  pre.textContent = lines.join("\n");
  pre.scrollTop = pre.scrollHeight;
  await showManifest(card, device);
  if (device.online) await loadAudio(card, id);
  else $('[data-f="audio-note"]', card).textContent = "· 기기가 켜져 있어야 볼 수 있음";
}

async function showManifest(card, device) {
  const box = $('[data-f="manifest"]', card);
  const note = $('[data-f="manifest-note"]', card);
  if (device.manifest) {
    box.value = JSON.stringify(device.manifest, null, 2);
    note.textContent = "· 직접 수정한 값";
  } else if (device.project) {
    try {
      const guess = await api(`/api/manifest?project=${encodeURIComponent(device.project)}`);
      box.value = JSON.stringify(guess, null, 2);
      note.textContent = "· live.json 또는 자동 추측";
    } catch (error) {
      box.value = "";
      note.textContent = `· ${error.message}`;
    }
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

// ---------- 작품 올리기 ----------

function keepPath(path) {
  const parts = path.split("/");
  const file = parts[parts.length - 1];
  return !parts.slice(0, -1).some((p) => SKIP_DIRS.has(p)) && !JUNK.has(file) && !file.startsWith("._");
}

// <input webkitdirectory> → [{file, path}]
function filesFromInput(input) {
  return [...input.files]
    .map((file) => ({ file, path: file.webkitRelativePath || file.name }))
    .filter((item) => keepPath(item.path));
}

function takeDrop(event) {
  const items = [...(event.dataTransfer.items || [])].filter((i) => i.kind === "file");
  return {
    entries: items.map((i) => (i.webkitGetAsEntry ? i.webkitGetAsEntry() : null)).filter(Boolean),
    files: [...event.dataTransfer.files],
  };
}

function readBatch(reader) {
  return new Promise((resolve, reject) => reader.readEntries(resolve, reject));
}

async function walk(entry, prefix, out) {
  const path = prefix ? `${prefix}/${entry.name}` : entry.name;
  if (entry.isDirectory) {
    if (SKIP_DIRS.has(entry.name)) return;
    const reader = entry.createReader();
    for (let batch = await readBatch(reader); batch.length; batch = await readBatch(reader)) {
      for (const child of batch) await walk(child, path, out);
    }
  } else if (entry.isFile && keepPath(path)) {
    out.push({ path, file: await new Promise((resolve, reject) => entry.file(resolve, reject)) });
  }
}

// 끌어다 놓은 것 → zip/html 하나면 [{file}], 폴더면 [{file, path}...]
async function collectDrop({ entries, files }) {
  if (entries.length === 1 && entries[0].isFile) return [{ file: files[0] }];
  if (!entries.length) return files.length === 1 ? [{ file: files[0] }] : files.map((f) => ({ file: f, path: f.name }));
  const out = [];
  for (const entry of entries) await walk(entry, "", out);
  return out;
}

function describe(items) {
  if (items.length === 1 && !items[0].path) return items[0].file.name;
  const top = items[0].path.split("/")[0];
  const sameTop = items.every((i) => i.path.startsWith(`${top}/`));
  return sameTop ? `${top} 폴더 (파일 ${items.length}개)` : `파일 ${items.length}개`;
}

function uploadForm(items, name) {
  const form = new FormData();
  if (name) form.append("name", name);
  if (items.length === 1 && !items[0].path) {
    form.append("file", items[0].file);
  } else {
    form.append("paths", JSON.stringify(items.map((i) => i.path)));
    for (const item of items) form.append("f", item.file, item.file.name);
  }
  return form;
}

// fetch는 업로드 진행률을 못 줘서 XMLHttpRequest 사용
function sendForm(url, form, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress?.(e.loaded / e.total);
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) resolve(JSON.parse(xhr.responseText));
      else reject(new Error(xhr.responseText || `업로드 실패 (${xhr.status})`));
    };
    xhr.onerror = () => reject(new Error("업로드 실패 — 중앙 서버와 연결이 끊김"));
    xhr.send(form);
  });
}

async function uploadToDevice(card, id, items) {
  if (!items.length) throw new Error("올릴 파일이 없음 (.git, node_modules 같은 건 빼고 올림)");
  const device = state.devices.find((d) => d.id === id);
  const what = describe(items);
  if (device.project && !confirm(`${device.name}에 “${what}”을(를) 올려서 설치할까요?\n지금 작품은 이걸로 바뀝니다.`)) return;
  const note = $('[data-f="upload-note"]', card);
  card.classList.add("busy");
  try {
    const result = await sendForm(`/api/devices/${id}/upload`, uploadForm(items), (p) => {
      note.textContent = p < 1 ? `올리는 중 ${Math.round(p * 100)}%` : "중앙에 저장 중…";
    });
    toast(
      result.deployed
        ? `“${result.label}” → ${device.name} 설치 시작 (로그에서 진행 확인)`
        : `“${result.label}” 저장됨 — ${device.name}이(가) 켜지면 자동 설치`,
    );
  } finally {
    card.classList.remove("busy");
    note.textContent = DROP_HINT;
  }
}

// ---------- 오디오 ----------

function fillAudio(card, audio) {
  const field = (name) => $(`[data-f="${name}"]`, card);
  for (const [kind, list] of [["sink", audio.sinks], ["source", audio.sources]]) {
    const options = list.map((d) => [d.name, d.label]);
    if (audio[kind] && !options.some(([value]) => value === audio[kind])) options.unshift([audio[kind], audio[kind]]);
    if (!options.length) options.push(["", "(장치 없음)"]);
    syncOptions(field(kind), options);
    field(kind).value = audio[kind] || "";
    const volume = audio[`${kind}_volume`];
    field(`${kind}-vol`).value = volume ?? 100;
    field(`${kind}-vol-text`).textContent = volume == null ? "?" : `${volume}%`;
  }
  card.dataset.audio = "loaded";
}

async function loadAudio(card, id) {
  const note = $('[data-f="audio-note"]', card);
  note.textContent = "· 불러오는 중…";
  try {
    fillAudio(card, await api(`/api/devices/${id}/audio`));
    note.textContent = "· 이 Pi의 기본 장치 (바꾸면 작품 Restart 필요)";
  } catch (error) {
    note.textContent = `· ${error.message}`;
  }
}

// ---------- 동작 ----------

function updateDevice(id, patch) {
  return api(`/api/devices/${id}`, { method: "POST", body: patch });
}

async function cardAction(card, id, action) {
  const field = (name) => $(`[data-f="${name}"]`, card);
  const device = state.devices.find((d) => d.id === id);
  const audio = (body) => api(`/api/devices/${id}/audio`, { method: "POST", body });
  switch (action) {
    case "upload-zip":
      return field("zip-input").click();
    case "upload-folder":
      return field("dir-input").click();
    case "key-save":
      await updateDevice(id, { key: field("key-input").value });
      field("key-input").value = "";
      if (!device.online) return toast("키 저장 — 기기가 켜지면 전달됨");
      await api(`/api/devices/${id}/restart`, { method: "POST" });
      return toast("키 저장 · 재시작 중");
    case "screen-save":
      await updateDevice(id, { screen: { target: field("screen-target").value, rotate: field("screen-rotate").value } });
      return toast(device.online ? `${device.name} 화면 설정 적용 — 작품이 다시 켜짐` : "저장 — 기기가 켜지면 적용됨");
    case "audio-load":
      return loadAudio(card, id);
    case "audio-save":
      if (card.dataset.audio !== "loaded") throw new Error("먼저 ‘불러오기’를 눌러 지금 값을 가져오세요");
      fillAudio(
        card,
        await audio({
          op: "set",
          sink: field("sink").value,
          source: field("source").value,
          sink_volume: Number(field("sink-vol").value),
          source_volume: Number(field("source-vol").value),
        }),
      );
      return toast(`${device.name} 오디오 적용 — 작품에 반영하려면 Restart`);
    case "audio-test-speaker":
      await audio({ op: "test", what: "speaker" });
      return toast(`${device.name}: “띵동” 소리 났으면 OK`);
    case "audio-test-mic": {
      field("mic-result").textContent = "2초 녹음 중… 마이크에 대고 말해보세요";
      try {
        const r = await audio({ op: "test", what: "mic" });
        const verdict = r.peak < 3 ? "거의 안 들림 → 마이크 선택/감도 확인" : r.peak > 97 ? "너무 큼(소리 찢어짐) → 감도 낮추기" : "OK";
        field("mic-result").textContent = `마이크 레벨: 최대 ${r.peak}% · 평균 ${r.rms}% — ${verdict}`;
      } catch (error) {
        field("mic-result").textContent = "";
        throw error;
      }
      return;
    }
    case "prompt-load": {
      const { prompt } = await api(`/api/devices/${id}/prompt`);
      field("prompt").value = prompt;
      return;
    }
    case "prompt-save":
      await updateDevice(id, { prompt: field("prompt").value });
      if (!device.online) return toast("프롬프트 저장 — 기기가 켜지면 전달됨");
      await api(`/api/devices/${id}/restart`, { method: "POST" });
      return toast("프롬프트 저장 · 재시작 중");
    case "prompt-reset":
      if (!confirm("대시보드에서 바꾼 프롬프트를 버리고 프로젝트 원래 파일로 돌릴까요? (Deploy 필요)")) return;
      await updateDevice(id, { prompt: null });
      return toast("원래대로 — Deploy하면 적용됨");
    case "manifest-save": {
      let manifest;
      try {
        manifest = JSON.parse(field("manifest").value);
      } catch (error) {
        throw new Error(`manifest JSON 문법 오류: ${error.message}`);
      }
      await updateDevice(id, { manifest });
      return toast("manifest 저장 — Deploy하면 적용됨");
    }
    case "manifest-auto":
      await updateDevice(id, { manifest: null });
      return showManifest(card, { ...device, manifest: null });
    case "forget":
      if (!confirm(`${device.name}을(를) 목록에서 지울까요?`)) return;
      return api(`/api/devices/${id}`, { method: "DELETE" });
    case "poweroff":
      if (!confirm(`${device.name} 전원을 끕니다. 다시 켜려면 전원 공급을 다시 연결해야 합니다.`)) return;
      break;
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
$("#chat-fade").addEventListener("click", () => run(() => api("/api/show/fade", { method: "POST" }), "차례로 잠재우기"));
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
          fade_seconds: Number($("#fade_seconds").value),
          kickoff: $("#kickoff").value,
        },
      }),
    "쇼 설정 저장",
  ),
);
$("#target").addEventListener("change", (e) =>
  run(() => api("/api/settings", { method: "POST", body: { target: e.target.value } })),
);

$("#screen-save").addEventListener("click", () =>
  run(
    () =>
      api("/api/settings", {
        method: "POST",
        body: { screen_rotate: $("#screen_rotate").value, screen_only: $("#screen_only").checked },
      }),
    "2인치 화면 설정 저장 — 바뀐 Pi는 작품이 다시 켜짐",
  ),
);

$("#default-key-save").addEventListener("click", () =>
  run(async () => {
    await api("/api/settings", { method: "POST", body: { default_key: $("#default-key-input").value } });
    $("#default-key-input").value = "";
  }, "공용 키 저장 — 각 기기 Restart 필요"),
);

$("#install-copy").addEventListener("click", () => run(() => copyText(state.install), "복사됨"));
$("#join-copy").addEventListener("click", () => run(() => copyText(state.join), "복사됨"));
$("#join-ip-copy").addEventListener("click", () => run(() => copyText(state.join_ip), "복사됨"));
$("#pairing-list").addEventListener("click", (e) => {
  const button = e.target.closest("[data-pair]");
  if (!button) return;
  const { pair, id } = button.dataset;
  button.disabled = true;
  run(
    () => api(`/api/pair/${encodeURIComponent(id)}/${pair}`, { method: "POST" }),
    pair === "approve" ? "허용함 — 곧 아래 기기 목록에 뜸" : "거절함",
  );
});
$("#install-ip-copy").addEventListener("click", () => run(() => copyText(state.install_ip), "복사됨"));

async function uploadToList(items) {
  if (!items.length) throw new Error("올릴 파일이 없음");
  const note = $("#upload-note");
  try {
    const result = await sendForm("/api/upload", uploadForm(items, $("#upload-name").value.trim()), (p) => {
      note.textContent = p < 1 ? `올리는 중 ${Math.round(p * 100)}%` : "저장 중…";
    });
    $("#upload-name").value = "";
    toast(`“${result.label}” 업로드 완료 — 기기 카드의 프로젝트 목록에 생김`);
  } finally {
    note.textContent = "";
  }
}
$("#upload-file-btn").addEventListener("click", () => $("#upload-file").click());
$("#upload-dir-btn").addEventListener("click", () => $("#upload-dir").click());
$("#upload-file").addEventListener("change", (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  if (file) run(() => uploadToList([{ file }]));
});
$("#upload-dir").addEventListener("change", (e) => {
  const items = filesFromInput(e.target);
  e.target.value = "";
  run(() => uploadToList(items));
});

// 카드 밖에 파일을 떨어뜨려도 브라우저가 그 파일을 열어버리지 않게
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("drop", (e) => e.preventDefault());

setInterval(() => state?.show.mode === "chat" && renderShow(), 1000);
connect();
