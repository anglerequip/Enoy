"use strict";
// Enoy chat UI. Plain JavaScript, no build step.
// One conversation view; proposed changes, checks and the "ask Claude?"
// question appear as cards inside the conversation.
// All model output is HTML-escaped: this page holds the API token.

const TOKEN = document.querySelector('meta[name="enoy-token"]').content;
const NAME = { codex: "Codex", qwen: "Qwen", claude: "Claude", team: "Team", enoy: "Enoy" };
const MODELS = {
  auto: { title: "Auto", desc: "ChatGPT evaluates the request and delegates to Codex or Claude." },
  codex: { title: "Codex", desc: "Uses your signed-in Codex CLI." },
  claude: { title: "Claude", desc: "Strongest reasoning. Uses your Claude plan." },
};
const S = {
  startingModel: null,
  status: null, projects: [], project: null, facts: null, chats: [], chatId: null, chat: null,
  model: lsGet("model") || "auto", attached: [], stream: {}, shown: {}, thinking: {}, drawer: false,
  tree: null, search: "", searchResults: null, preview: null, config: null, cfgMeta: null,
  settingsTab: "general", latestRelease: null, images: [], streamDone: {},
};
const MAX_IMAGES = 5;
const SLASH_COMMANDS = [
  { name: "/help", desc: "Show available commands" },
  { name: "/compact", desc: "Compact earlier context and reset provider sessions" },
  { name: "/new", desc: "Start a new chat" },
  { name: "/model", desc: "Choose who answers" },
  { name: "/status", desc: "Refresh provider status" },
  { name: "/resume", desc: "Resume a stopped or failed chat" },
];
function imgUrl(id) { return `/api/images/${id}?token=${encodeURIComponent(TOKEN)}`; }
// Chat mode = plain chats (no folder); Code mode = chats about a project folder.
function mode() { return S.project && (S.project.is_default || S.project.kind === "space") ? "chat" : "code"; }
function regularChat() { return mode() === "chat" && (S.config?.chat?.experience || "chatgpt") === "chatgpt"; }
async function openRegularChatGPT() {
  try { toast((await api("POST", "/api/chatgpt/open", {})).message); }
  catch (e) { toast(e.message, true); }
}
function codeProjects() { return S.projects.filter(p => !p.is_default && p.kind !== "space"); }

// ---------------------------------------------------------------- helpers

async function api(method, path, body) {
  const opts = { method, headers: { "X-Enoy-Token": TOKEN } };
  if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  const res = await fetch(path, opts);
  let data = {};
  try { data = await res.json(); } catch (e) { /* empty */ }
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}
function esc(s) { return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* ignore */ } }
const $ = id => document.getElementById(id);

// Markdown lives in markdown.js. Finished text is cached; streaming text is
// rendered block by block with MD.renderInto (see renderLive).
function md(text, streaming = false) { return streaming ? MD.render(text, true) : MD.cached(text); }

function clock(ts) { return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); }
// Qwen's mark: a "Q" drawn as a ring with a tail, in the tile's ink color (the tile flips with the theme).
const QWEN_MARK = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="11.5" r="6.8"/><path d="M13.2 13.6 18.4 19.6"/></svg>`;
// Outlined Codex terminal mark, matching the supplied reference.
const CODEX_MARK = `<svg viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path class="codex-outline" d="M15 6C18 3 23 3 26 5C31 2 38 6 38 12C44 14 46 21 42 26C44 32 40 39 34 39C31 45 24 46 19 42C13 45 6 41 6 35C1 32 1 25 5 21C2 14 7 7 15 6Z"/><path d="m15 18 5 6-5 6"/><path class="codex-cursor" d="M25 30h8"/></svg>`;
function avatar(b) {
  if (b === "codex") return `<span class="avatar codex">${CODEX_MARK}</span>`;
  if (b === "team") return `<span class="avatar team">◆</span>`;
  if (b === "enoy") return `<img class="avatar enoy" src="/static/logo.svg" alt="">`;
  return b === "claude" ? `<span class="avatar claude">✳</span>` : `<span class="avatar qwen">${QWEN_MARK}</span>`;
}

let toastTimer;
function toast(msg, bad = false) {
  const el = $("toast");
  el.textContent = msg;
  el.className = "toast" + (bad ? " bad" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), bad ? 7000 : 3000);
}
async function guarded(fn) { try { return await fn(); } catch (e) { toast(e.message, true); } }

function openModal(html, wide = false) {
  const box = $("modal-box");
  box.className = "modal-box" + (wide ? " wide" : "");
  box.innerHTML = html;
  $("modal").classList.remove("hidden");
}
function closeModal() { $("modal").classList.add("hidden"); }
function confirmBox(title, bodyHtml, okLabel = "OK", danger = false) {
  return new Promise(resolve => {
    openModal(`<h2>${esc(title)}</h2><div>${bodyHtml}</div>
      <div class="row" style="justify-content:flex-end"><button class="btn ghost" id="m-cancel">Cancel</button>
      <button class="btn ${danger ? "danger" : "primary"}" id="m-ok">${esc(okLabel)}</button></div>`);
    $("m-cancel").onclick = () => { closeModal(); resolve(false); };
    $("m-ok").onclick = () => { closeModal(); resolve(true); };
  });
}

// Popup menu anchored to a button. items: [{title, desc, action, data, checked, disabled}] or "-".
function openMenu(anchor, items, align = "left") {
  const menu = $("menu");
  menu.innerHTML = items.map(it => it === "-" ? "<hr>" : `
    <button ${it.disabled ? "disabled" : ""} data-action="${esc(it.action)}" ${Object.entries(it.data || {}).map(([k, v]) => `data-${k}="${esc(v)}"`).join(" ")}>
      ${it.checked !== undefined ? `<span class="check">${it.checked ? "✓" : ""}</span>` : ""}
      <span><div class="mi-title">${it.title}</div>${it.desc ? `<div class="mi-desc">${esc(it.desc)}</div>` : ""}</span></button>`).join("");
  menu.classList.remove("hidden");
  const r = anchor.getBoundingClientRect();
  const mw = menu.offsetWidth, mh = menu.offsetHeight;
  let left = align === "right" ? r.right - mw : r.left;
  let top = r.bottom + 6;
  if (top + mh > window.innerHeight - 8) top = r.top - mh - 6;
  menu.style.left = Math.max(8, Math.min(left, window.innerWidth - mw - 8)) + "px";
  menu.style.top = Math.max(8, top) + "px";
}
function closeMenu() { $("menu").classList.add("hidden"); }

// ---------------------------------------------------------------- data

async function loadStatus(refresh = false) {
  try { S.status = await api("GET", "/api/status" + (refresh ? "?refresh=1" : "")); } catch (e) { S.status = null; }
  if (S.status && S.team === undefined) S.team = !!S.status.team_default;
  const cm = S.status && S.status.claude.model_setting;
  MODELS.claude.title = cm ? `Claude (${cm})` : "Claude";
  MODELS.claude.desc = "Strongest reasoning. Uses your Claude plan." + (cm ? "" : " Model: Claude Code's default (change it in Settings).");
  renderStatusLine();
  renderModelPicker();
  const note = document.querySelector(".composer-note");  // Only the note: typing in the box is not disturbed.
  if (note) note.innerHTML = composerNoteHtml();
}
// Keep provider sign-in status current without sending model requests.
// Claude's status is the cached one unless you press Refresh.
setInterval(async () => {
  if (document.hidden) return;
  try { S.status = await api("GET", "/api/status"); renderStatusLine(); } catch (e) { /* the next tick tries again */ }
}, 15000);
function coordinatorOn() { return !!(S.status && S.status.coordinator && S.model === "auto" && !localOnlyReason() && !S.status.routing.zero_paid_mode); }
function codexOk() { return !!(S.status && S.status.codex?.available); }
function claudeOk() { return !!(S.status && S.status.claude.available); }

async function loadProjects() { S.projects = (await api("GET", "/api/projects")).projects; }

async function useProject(id) {
  const d = await api("GET", `/api/projects/${id}`);
  S.project = d.project;
  S.facts = d.facts;
  S.tree = null; S.preview = null; S.searchResults = null; S.attached = [];
  lsSet("lastProject", id);
  if (!S.project.is_default) lsSet("lastCodeProject", id);
  await loadChats();
  renderSidebar();
  if (S.drawer) renderDrawer();
}

async function useDefaultProject() {
  const d = await api("POST", "/api/projects/default");
  await useProject(d.project.id);
}

async function switchWorkspace(want) {
  if (!["chat", "code"].includes(want) || S.switchingMode) return;
  if (want === mode() && (want === "chat" || S.project)) {
    if (S.view && S.view !== "chat" && typeof setView === "function") { setView("chat"); renderAll(); }
    return;
  }
  S.switchingMode = true;
  const root = document.documentElement;
  root.dataset.direction = want === "code" ? "to-code" : "to-chat";
  $("mode-switch").setAttribute("aria-busy", "true");
  const change = async () => {
    S.chat = null; S.chatId = null; S.draft = "";
    if (typeof setView === "function") setView("chat");
    if (want === "chat") await useDefaultProject();
    else {
      const folders = codeProjects(), last = Number(lsGet("lastCodeProject"));
      const pick = folders.find(p => p.id === last) || folders[0];
      if (pick) await useProject(pick.id);
      else { S.project = null; S.chats = []; }
    }
    renderAll();
    if (typeof syncFrameTheme === "function") syncFrameTheme();
  };
  try {
    const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (document.startViewTransition && !reduce) {
      const transition = document.startViewTransition(change);
      transition.ready.catch(() => {});
      await transition.updateCallbackDone;
      await transition.finished.catch(() => {});
    } else {
      await change();
      if (!reduce && $("main").animate) $("main").animate([
        {opacity:0,transform:`translateX(${want === "code" ? 18 : -18}px)`},
        {opacity:1,transform:"translateX(0)"}
      ], {duration:320,easing:"cubic-bezier(.2,.8,.2,1)"});
    }
  } finally {
    S.switchingMode = false;
    $("mode-switch").removeAttribute("aria-busy");
  }
}

async function loadChats() {
  if (!S.project) { S.chats = []; return; }
  S.chats = (await api("GET", `/api/tasks?project_id=${S.project.id}&limit=200`)).tasks;
}

let chatLoadVersion = 0;
async function loadChat(id) {
  const version = ++chatLoadVersion;
  S.chatId = id;
  const chat = await api("GET", `/api/tasks/${id}`);
  if (version === chatLoadVersion && S.chatId === id) S.chat = chat;
}

// ---------------------------------------------------------------- sidebar

function renderSidebar() {
  const p = S.project;
  const m = mode();
  document.documentElement.dataset.mode = m;
  EnoyAppearance.apply(m);
  if (typeof syncFrameTheme === "function") syncFrameTheme();
  document.documentElement.dataset.chatExperience = regularChat() ? "chatgpt" : "integrated";
  $("mode-caption").textContent = m === "chat" ? "A little space to think" : "Your development workspace";
  $("mode-indicator").textContent = m === "chat" ? "Chat" : "Code workspace";
  $("workspace-context").innerHTML = m === "code" ? `<span class="workspace-label">WORKSPACE</span><span class="workspace-path" title="${esc(p?.path || "")}">${esc(p?.path || "Choose a project folder to get started")}</span><button class="btn small ghost" data-action="pick-folder">${p ? "Change folder" : "Open folder"}</button>` : "";
  $("workspace-context").classList.toggle("hidden", m !== "code");
  document.querySelectorAll("#mode-switch button").forEach(b => {
    const on = b.dataset.mode === m;
    b.classList.toggle("on", on);
    b.setAttribute("aria-selected", on);
  });
  $("project-switch").innerHTML = m === "chat" ? (p && p.kind === "space" ? `<div class="space-chip">
      <button data-action="project-page" title="Open the project page">${FOLDER_ICON}<span class="pname">${esc(p.name)}</span></button>
      <button class="icon-btn" data-action="switch-default" title="Leave the project (plain chats)" aria-label="Leave project">✕</button></div>` : "") : `<button data-action="project-menu" title="${esc(p ? p.path : "Open a project folder")}">
    ${FOLDER_ICON}<span class="pname">${p ? esc(p.name) : "Open a folder…"}</span>${CHEV}</button>`;
  const groups = { Today: [], Yesterday: [], "Previous 7 days": [], Older: [] };
  const now = new Date();
  const startToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime() / 1000;
  for (const c of S.chats) {
    const t = c.updated;
    const g = t >= startToday ? "Today" : t >= startToday - 86400 ? "Yesterday" : t >= startToday - 7 * 86400 ? "Previous 7 days" : "Older";
    groups[g].push(c);
  }
  const html = Object.entries(groups).filter(([, list]) => list.length).map(([label, list]) =>
    `<div class="group-label">${label}</div>` + list.map(c => {
      const busy = ["running", "queued"].includes(c.status);
      const attn = ["awaiting_approval", "awaiting_claude", "interrupted", "paused"].includes(c.status);
      return `<div class="chat-row ${c.id === S.chatId ? "active" : ""}"><button class="chat-item ${c.id === S.chatId ? "active" : ""}" data-action="open-chat" data-id="${c.id}" title="${esc(c.title)}">
        <span class="t">${esc(c.title)}</span>${busy ? `<span class="spin-dot"></span>` : attn ? `<span class="attn" title="Needs you"></span>` : ""}</button>
        <button class="chat-more" data-action="chat-item-menu" data-id="${c.id}" title="Rename, move or delete" aria-label="Options for ${esc(c.title)}">⋯</button></div>`;
    }).join("")).join("");
  $("chat-list").innerHTML = html || `<div class="group-label">${p ? "No chats yet" : "No folder open"}</div>`;
}

function renderStatusLine() {
  const el = $("status-line");
  el.innerHTML = ["codex", "claude"].map(p => {
    const ready = S.status?.[p]?.available;
    const label = !S.status ? "Unknown" : ready ? "Ready" : "Unavailable";
    return `<button class="provider-button" data-action="provider-status" data-provider="${p}" aria-label="${NAME[p]}: ${label}. View status">
      <span class="provider-mark" aria-hidden="true">${p === "codex" ? CODEX_MARK : "✳"}</span>
      <span class="provider-label">${NAME[p]}<span class="provider-state"><span class="dot ${!S.status ? "" : ready ? "ok" : "bad"}"></span>${label}</span></span><span class="provider-arrow" aria-hidden="true">›</span></button>`;
  }).join("");
}

let providerDialog = 0;
function usageHtml(provider) {
  const data = S.usage?.[provider];
  const now = Date.now() / 1000;
  const windows = data?.windows || [];
  return `<section class="usage-panel" aria-label="Subscription usage"><div class="usage-heading"><h3>Usage remaining</h3><span class="usage-source">${esc(data?.source || (provider === "claude" ? "Claude subscription" : "Subscription"))}</span></div>
    ${windows.length ? windows.map(w => {
      const expired = w.resets_at != null && w.resets_at <= now;
      const stale = data.source !== "Manual snapshot" && now - data.checked_at > 300;
      const hours = Math.max(0, Math.ceil((w.resets_at - now) / 60));
      const until = hours >= 60 ? `${Math.floor(hours / 60)}h ${hours % 60}m` : `${hours}m`;
      return `<div class="usage-window"><div class="usage-heading"><span>${esc(w.label)}</span><strong>${expired ? "Update needed" : `${stale ? "Last reported " : ""}${Number(w.remaining_percent)}% left`}</strong></div>
        ${expired ? "" : `<progress max="100" value="${Number(w.remaining_percent)}" aria-label="${esc(w.label)} remaining"></progress>`}
        <div class="small muted">${w.resets_at == null ? "Reset time not reported" : `${expired ? "Reported reset passed · refresh or update this snapshot" : `Resets in ${until}`}<br>${esc(new Date(w.resets_at * 1000).toLocaleString())}`}</div></div>`;
    }).join("") : `<p class="small muted">${esc(data?.detail || "Usage has not been loaded yet.")}</p>`}
    ${windows.length && data?.detail ? `<p class="small muted">${esc(data.detail)}</p>` : ""}
    ${data?.checked_at ? `<p class="small muted">${data.source === "Manual snapshot" ? "Entered" : "Checked"} ${esc(new Date(data.checked_at * 1000).toLocaleString())} · times shown locally</p>` : ""}
    <div class="provider-actions"><button class="btn" data-action="usage-refresh" data-provider="${provider}">Refresh usage</button>${provider === "codex" ? "" : `<a class="btn" href="https://claude.ai/settings/usage" target="_blank" rel="noopener noreferrer">Open Claude Usage</a><button class="btn" data-action="usage-edit">Update snapshot</button>`}</div></section>`;
}

async function refreshUsage(provider, refresh = false) {
  const version = providerDialog;
  const dialog = $("provider-dialog");
  const feedback = $("provider-feedback");
  if (feedback) feedback.textContent = "Loading usage…";
  try {
    S.usage = await api("GET", `/api/usage${refresh ? "?refresh=1" : ""}`);
    if (version === providerDialog && $("provider-dialog") === dialog && !$("modal").classList.contains("hidden")) showProviderStatus(provider);
  } catch (e) {
    if (version === providerDialog && $("provider-dialog") === dialog) showProviderStatus(provider, e.message, true);
  }
}

function editClaudeUsage() {
  ++providerDialog;
  openModal(`<h2>Claude usage snapshot</h2><p class="small muted">Copy the percentage used and reset times from Claude’s Usage page. Leave both fields empty to omit a window. These values are manual; Enoy won’t infer a new quota after a reset.</p>
    ${[["five_hour", "5 hours"], ["seven_day", "Weekly"]].map(([key, label]) => `<div class="usage-panel"><h3>${label}</h3><label class="field">Used (%)<input id="usage-${key}-used" type="number" min="0" max="100" step="0.1"></label><label class="field">Resets at (your local time)<input id="usage-${key}-reset" type="datetime-local"></label></div>`).join("")}
    <p id="usage-error" class="small danger-text" role="status"></p><div class="provider-actions"><button class="btn primary" data-action="usage-save">Save snapshot</button><button class="btn" data-action="provider-status" data-provider="claude">Cancel</button></div>`);
}

async function saveClaudeUsage() {
  const version = providerDialog;
  const form = $("usage-error");
  const body = {};
  try {
    for (const key of ["five_hour", "seven_day"]) {
      const used = $("usage-" + key + "-used").value;
      const reset = $("usage-" + key + "-reset").value;
      if (!used && !reset) continue;
      const timestamp = new Date(reset).getTime() / 1000;
      if (used === "" || !Number.isFinite(Number(used)) || Number(used) < 0 || Number(used) > 100 || !Number.isFinite(timestamp) || timestamp <= Date.now() / 1000) throw new Error("Enter a percentage from 0 to 100 and a future reset time for each window.");
      body[key] = { used_percent: Number(used), resets_at: timestamp };
    }
    const saved = await api("POST", "/api/usage/claude", body);
    S.usage = { ...S.usage, claude: { ...saved, detail: "Manual snapshot. Update from Claude’s Usage page." } };
    if (version === providerDialog && $("usage-error") === form && !$("modal").classList.contains("hidden")) showProviderStatus("claude", "Usage snapshot saved.");
  } catch (e) { if ($("usage-error") === form && form) form.textContent = e.message; }
}

async function showTokenSavings() {
  ++providerDialog;
  openModal(`<div id="savings-loading"><h2>Estimated token savings</h2><p role="status">Loading Enoy records…</p><button class="btn" data-action="close-modal">Close</button></div>`);
  const version = providerDialog;
  const loading = $("savings-loading");
  try {
    const data = await api("GET", "/api/usage/savings");
    if (version !== providerDialog || $("savings-loading") !== loading || $("modal").classList.contains("hidden")) return;
    openModal(`<h2>Estimated token savings</h2><div class="usage-total">≈ ${Number(data.estimated_tokens_saved).toLocaleString()}</div><p class="muted">History tokens avoided by Enoy</p>
      ${["codex", "claude"].map(p => `<div class="usage-window usage-heading"><span>${NAME[p]}</span><strong>≈ ${Number(data.providers.find(r => r.backend === p)?.estimated_tokens_saved || 0).toLocaleString()}</strong></div>`).join("")}
      <p class="small muted">${esc(data.method)}</p><div id="savings-audit" class="small muted" role="status"></div>
      <div class="row"><button class="btn primary" data-action="audit-savings">Verify saved work</button><button class="btn" data-action="close-modal">Close</button></div>`);
  } catch (e) { if (version === providerDialog && $("savings-loading") === loading && !$("modal").classList.contains("hidden")) openModal(`<h2>Estimated token savings</h2><p role="alert">${esc(e.message)}</p><button class="btn" data-action="token-savings">Retry</button><button class="btn" data-action="close-modal">Close</button>`); }
}

async function auditTokenSavings() {
  const target = $("savings-audit");
  if (!target) return;
  target.textContent = "Checking saved request records…";
  try {
    const data = await api("GET", "/api/usage/savings/audit");
    if (target.isConnected) target.innerHTML = `<strong>${data.ok ? "✓ Accounting verified" : "Check needed"}</strong> · ${esc(data.detail)}<br>${data.completed_requests} completed model request(s), ${data.pending_requests} unfinished request(s), ${data.invalid_records} invalid record(s). ${data.applied_change_sets} applied change set(s); ${data.checks.passed || 0} passed and ${data.checks.failed || 0} failed command check(s). Native edits and provider caching are outside this estimate. No model request was sent.`;
  } catch (e) { if (target.isConnected) target.textContent = `Could not verify: ${e.message}`; }
}

let launchedProvider = null;
window.addEventListener("focus", async () => {
  const provider = launchedProvider;
  if (!provider) return;
  launchedProvider = null;
  try {
    S.status = await api("GET", "/api/status?refresh=1");
    renderStatusLine(); renderModelPicker();
    if ($("provider-dialog")?.dataset.provider === provider && !$("modal").classList.contains("hidden")) showProviderStatus(provider, "Status checked after returning from the terminal.");
  } catch (e) { toast(e.message, true); }
});
function showProviderStatus(provider, message = "", bad = false) {
  if (!["codex", "claude"].includes(provider)) return;
  const st = S.status?.[provider];
  const enabled = S.config?.[provider]?.enabled !== false;
  const cliMissing = !!st && !st.available && /not found|not installed/i.test(st.detail || "");
  openModal(`<div id="provider-dialog" data-provider="${provider}">
    <div class="provider-dialog-head"><span class="provider-mark" aria-hidden="true">${provider === "codex" ? CODEX_MARK : "✳"}</span><div><h2>${NAME[provider]}</h2><span class="provider-state"><span class="dot ${!st ? "" : st.available ? "ok" : "bad"}"></span>${!st ? "Status unknown" : st.available ? "Ready" : "Unavailable"}</span></div></div>
    <p class="provider-detail">${esc(st?.detail || "Check status to connect to Enoy.")}</p>
    ${st?.model ? `<p class="small muted">Model: ${esc(st.model)}</p>` : ""}
    ${usageHtml(provider)}
    <p class="small muted">Enoy uses the signed-in ${provider === "codex" ? "Codex" : "Claude Code"} CLI. Checking status sends no model request.</p>
    <p id="provider-feedback" class="small ${bad ? "danger-text" : "muted"}" role="status">${esc(message)}</p>
    <div class="provider-actions"><button class="btn" data-action="provider-check" data-provider="${provider}">Check status</button>
    ${!enabled ? `<button class="btn primary" data-action="open-settings" data-tab="models">Enable in Settings</button>` : cliMissing ? `<button class="btn primary" data-action="install-providers" data-provider="${provider}">Install missing tools</button>` : `<button class="btn primary" data-action="provider-launch" data-provider="${provider}" data-launch="${st?.available ? "launch" : "login"}">${st?.available ? "Launch CLI" : "Launch & sign in"}</button>`}
    <button class="btn ghost" data-action="close-modal">Close</button></div></div>`);
}
async function providerAction(el, check = false) {
  const provider = el.dataset.provider;
  const version = ++providerDialog;
  const dialog = $("provider-dialog");
  if (!dialog) return;
  dialog.querySelectorAll("button").forEach(b => { b.disabled = true; });
  $("provider-feedback").textContent = check ? "Checking status…" : "Opening terminal…";
  let message, failed = false;
  try {
    if (check) {
      S.status = await api("GET", "/api/status?refresh=1");
      renderStatusLine(); renderModelPicker();
      message = "Status checked.";
    } else {
      message = (await api("POST", `/api/providers/${provider}/launch`, { action: el.dataset.launch })).message;
      launchedProvider = provider;
    }
  } catch (e) { message = e.message; failed = true; }
  if (version === providerDialog && $("provider-dialog") === dialog && !$("modal").classList.contains("hidden")) showProviderStatus(provider, message, failed);
}

// ---------------------------------------------------------------- top bar

const CHEV = `<svg class="chev" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg>`;
const FOLDER_ICON = `<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 6.5A1.5 1.5 0 0 1 5.5 5H10l2 2h6.5A1.5 1.5 0 0 1 20 8.5v9a1.5 1.5 0 0 1-1.5 1.5h-13A1.5 1.5 0 0 1 4 17.5z"/></svg>`;

// The model menu sits in the composer, next to Send, as in Claude.
function renderModelPicker() {
  const el = $("model-picker");
  if (!el) return;
  const m = MODELS[S.model] || MODELS.auto;
  el.innerHTML = `<button class="picker-btn" data-action="model-menu" title="Who answers: ${esc(m.desc)}">
    ${S.model === "auto" ? `<img class="mini-mark" src="/static/logo.svg" alt="">` : avatar(S.model)} ${mode() === "chat" && S.model === "auto" ? "Codex" : m.title}${CHEV}</button>`;
}

function teamOn() { return S.chat ? !!S.chat.task.state.team : !!S.team; }
function renderTeamToggle() {
  const el = $("team-toggle");
  if (!el) return;
  const general = !S.project || mode() === "chat";
  const on = teamOn();
  el.innerHTML = general ? "" : `<button class="team-pill ${on ? "on" : ""}" data-action="team-toggle" aria-pressed="${on}"
    title="${on ? "Team roles are on for this chat: code changes are planned, built, tested and reviewed by a team of roles. Questions are still answered directly. Click to turn off." : "Team roles are off: one model answers directly. Click to let a team of roles plan, build, test and review code changes in this chat."}">
    <span class="dot ${on ? "ok" : ""}"></span>Team ${on ? "on" : "off"}</button>`;
}

// Top bar: the open chat's title opens its menu (rename, move, delete…), as in Claude.
function renderChatMenu() {
  renderTeamToggle();
  const space = S.chat && S.project && S.project.kind === "space";
  $("chat-menu").innerHTML = S.chat ? `${space ? `<button class="crumb-btn" data-action="project-page" title="Open the project page">${esc(S.project.name)}</button><span class="crumb-sep">/</span>` : ""}<button class="title-btn" data-action="chat-menu" title="Chat options">
    <span class="t">${esc(S.chat.task.title)}</span>${CHEV}</button>` : "";
  $("files-btn").classList.toggle("hidden", mode() === "chat" || !S.project);
}

// ---------------------------------------------------------------- thread

function nearBottom() { const s = $("scroll"); return s.scrollHeight - s.scrollTop - s.clientHeight < 140; }
function toBottom() { const s = $("scroll"); s.scrollTop = s.scrollHeight; }

const TIP_ICONS = [
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3l1.8 4.9L19 9.7l-5.2 1.8L12 16.5l-1.8-5L5 9.7l5.2-1.8z"/><path d="M19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8z"/></svg>',
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 7l-5 5 5 5M16 7l5 5-5 5"/></svg>',
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M9.5 9.5a2.5 2.5 0 1 1 3.3 2.4c-.5.2-.8.6-.8 1.1V14M12 17h.01"/></svg>',
];

function renderThread(forceBottom = false) {
  const stick = forceBottom || nearBottom();
  const main = $("main");
  if (!S.chat) {
    main.classList.add("empty");
    if (regularChat()) {
      $("thread").innerHTML = `<div class="hello regular-chat-home">
        <div class="home-eyebrow">CHATGPT / CHAT</div>
        <h1 class="serif">Create, learn, explore.</h1>
        <p>Your everyday ChatGPT, using your ChatGPT account. Continue on ChatGPT's own page in your browser.</p>
        <div class="chatgpt-actions"><button class="btn primary" data-action="open-chatgpt">Open ChatGPT ↗</button></div>
        <p class="small muted chatgpt-note">Enoy sends no prompt to Codex or the API. ChatGPT's own plan and message limits apply. Conversations stay in ChatGPT.</p>
        <button class="btn ghost small" data-action="integrated-chat">Use integrated Codex chat (uses allowance)</button></div>`;
      return;
    }
    const hour = new Date().getHours();
    const greet = hour < 5 ? "Working late" : hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
    const general = S.project && S.project.is_default;
    if (!S.project) {
      $("thread").innerHTML = `<div class="hello code-home">
        <span class="code-spark" aria-hidden="true">✳</span>
        <h1 class="serif">What shall we build?</h1>
        <p>A focused place for your next project. Open a folder to begin.</p>
        <div class="code-open"><button class="btn primary" data-action="pick-folder">${FOLDER_ICON} Open a project folder</button></div>
        <div class="code-home-hint">Explore the code <span>·</span> Make a change <span>·</span> Review the results</div></div>`;
      return;
    }
    const chatting = mode() === "chat";
    const tips = chatting
      ? ["Help me put an idea into words", "Explain something simply", "Plan something with me"]
      : ["Give me an overview of this project", "Find and fix a bug in the main file", "Add tests for the most important function"];
    $("thread").innerHTML = `<div class="hello ${chatting ? "" : "code-home"}">
      ${chatting ? `<div class="home-eyebrow">A MOMENT FOR YOUR IDEAS</div>` : `<span class="code-spark" aria-hidden="true">✳</span><div class="code-project-label">${FOLDER_ICON} ${esc(S.project.name)}</div>`}
      <h1 class="serif">${chatting ? `${greet}.` : "What shall we build?"}</h1>
      <p>${S.project && S.project.kind === "space" ? `In project <strong>${esc(S.project.name)}</strong>.${S.project.instructions ? " Its instructions apply to this chat." : ""}` : general ? "A question, a rough idea, or something on your mind. Start wherever you are." : `Working in <strong>${esc(S.project ? S.project.name : "")}</strong>. Describe a change or explore the code.`}</p>
      ${!codexOk() && S.status && !coordinatorOn() ? `<p style="margin-top:10px;color:var(--bad)">Codex is unavailable. Run codex login, then refresh status. <a href="#" data-action="open-settings" data-tab="status">Details</a></p>` : ""}
      <div class="suggest">${tips.map((t, i) => `<button data-action="suggest" data-text="${esc(t)}"><span class="tip-ic" aria-hidden="true">${chatting ? ["✦", "◌", "↗"][i] : TIP_ICONS[i % TIP_ICONS.length]}</span><span>${esc(t)}</span></button>`).join("")}</div></div>`;
    return;
  }
  main.classList.remove("empty");
  const d = S.chat;
  const t = d.task;
  const items = [];
  d.messages.forEach(m => items.push({ t: m.created, k: "msg", m }));
  d.changesets.forEach(c => items.push({ t: c.created, k: "cs", c }));
  d.runs.forEach(r => items.push({ t: r.created, k: "run", r }));
  let seenOwner = false;
  d.events.forEach(e => {
    if (e.type === "owner") {
      if (seenOwner) items.push({ t: e.created, k: "note", text: handoffText(e) });
      seenOwner = true;
    } else if (e.type === "note") {
      items.push({ t: e.created, k: "note", text: e.text });
    }
  });
  items.sort((a, b) => a.t - b.t);
  const lastAi = [...d.messages].reverse().find(m => m.role === "assistant");
  const latestRun = d.runs[0];
  const busy = ["running", "queued"].includes(t.status);
  const html = items.map(it => {
    if (it.k === "msg") return messageHtml(it.m, it.m === lastAi && !busy);
    if (it.k === "cs") return changesetHtml(it.c);
    if (it.k === "run") return runHtml(it.r, latestRun && it.r.id === latestRun.id && !busy);
    return `<div class="sys-note">${esc(it.text)}</div>`;
  });
  const keyed = items.map((it, n) => ({
    key: it.k === "msg" ? `m${it.m.id}` : it.k === "cs" ? `c${it.c.id}` : it.k === "run" ? `r${it.r.id}` : `n${it.t}-${n}`,
    html: html[n],
    enter: !(it.k === "msg" && it.m.role === "assistant"),
  }));
  keyed.push({ key: `s${t.id}`, html: statusHtml(t) });
  const thread = $("thread");
  patchThread(thread, keyed, thread._chat === t.id);
  thread._chat = t.id;
  if (S.stream[t.id] || S.thinking[t.id]) renderLive(true);
  if (stick) toBottom();
}

// Update the thread in place. An item whose HTML did not change keeps its DOM
// node, so finished answers are not rebuilt (no flicker, text selection and
// images survive) while new items are inserted where they belong.
function patchThread(container, items, animate = false) {
  const old = new Map();
  for (const el of [...container.children]) {
    if (el.dataset && el.dataset.k) old.set(el.dataset.k, el); else el.remove();
  }
  let prev = null;
  for (const it of items) {
    let el = old.get(it.key);
    if (el && el._html === it.html) old.delete(it.key);
    else {
      const fresh = document.createElement("div");
      fresh.className = "ti";
      fresh.dataset.k = it.key;
      fresh.innerHTML = it.html;
      fresh._html = it.html;
      if (el) { el.replaceWith(fresh); old.delete(it.key); }
      else if (animate && it.enter) fresh.classList.add("enter");
      el = fresh;
    }
    const want = prev ? prev.nextSibling : container.firstChild;
    if (want !== el) container.insertBefore(el, want);
    prev = el;
  }
  old.forEach(el => el.remove());
}

function handoffText(e) {
  const who = NAME[e.backend] || e.backend || "Enoy";
  let why = e.text.replace(/^Now handled by [^:]+:\s*/, "").replace(/^Escalated:\s*/, "");
  why = why.replace(/^The local model/, "Qwen");
  return `${who} took over · ${why}`;
}

function modelLabel(info) {
  if (!info || !info.model) return "";
  const names = {"gpt-6.1-sol":"GPT-6.1 Sol", "gpt-6-luna":"GPT-6 Luna", "gpt-6.1-terra":"GPT-6.1 Terra", "gpt-6-astra":"GPT-6 Astra", "claude-opus-5-5":"Claude Opus 5.5", "sonnet":"Claude Sonnet"};
  return `${names[info.model] || info.model}${info.effort ? ` · ${info.effort} effort` : ""}`;
}
function messageModelInfo(m) {
  const rows = (S.chat?.transmissions || []).filter(t => t.backend === m.backend && t.created <= m.created).sort((a,b) => b.created - a.created);
  for (const t of rows) {
    try { const info=JSON.parse(t.usage || "{}"); if (info.model) return info; } catch (_) { /* older usage format */ }
  }
  return {};
}
function progressHtml(notes, id = "") {
  const all = (notes || []).filter(Boolean);
  const latest = all.at(-1) || "Working on your request…";
  const brief = latest.replace(/\s+/g, " ").trim().slice(0, 135);
  return `<details class="thinking" ${id ? `id="${id}-wrap"` : ""}><summary><strong>Progress</strong><span class="thinking-brief">${esc(brief)}</span></summary><div ${id ? `id="${id}"` : ""}>${esc(all.join("\n\n") || "Waiting for a provider progress update.")}</div></details>`;
}
function messageHtml(m, isLast) {
  if (m.role === "user") {
    const pics = (m.images || []).map(id => `<a href="#" data-action="view-image" data-id="${id}"><img src="${imgUrl(id)}" alt="Attached image" loading="lazy"></a>`).join("");
    return `${pics ? `<div class="msg-images">${pics}</div>` : ""}<div class="msg-user">${esc(m.content)}</div>`;
  }
  const b = m.backend || "enoy";
  return `<div class="msg-ai" data-msg-id="${m.id}">
    <div class="ai-head">${avatar(b)}${NAME[b] || b}<span class="where">${esc(modelLabel(messageModelInfo(m)))}</span></div>
    <div class="ai-body">${md(m.content)}</div>
    ${["codex","claude"].includes(b) ? progressHtml(messageModelInfo(m).progress || ["Response completed. No provider progress updates were recorded for this older message."]) : ""}
    <div class="ai-actions">
      <button data-action="copy-msg" data-id="${m.id}">Copy</button>
      <button data-action="save-artifact" data-id="${m.id}" title="Keep this answer in Artifacts">Save as artifact</button>
      ${isLast && b === "qwen" && claudeOk() && !(S.chat && S.chat.task.workflow === "team") ? `<button data-action="ask-claude">Ask Claude instead</button>` : ""}
    </div></div>`;
}

function diffStats(diff) {
  let add = 0, del = 0;
  for (const l of (diff || "").split("\n")) {
    if (l.startsWith("+") && !l.startsWith("+++")) add++;
    else if (l.startsWith("-") && !l.startsWith("---")) del++;
  }
  return `<span style="color:var(--ok)">+${add}</span> <span style="color:var(--bad)">−${del}</span>`;
}

function diffHtml(diff) {
  const lines = (diff || "").split("\n").slice(0, 3000);
  return `<div class="diff">${lines.map(l => {
    const cls = l.startsWith("+++") || l.startsWith("---") ? "meta" : l.startsWith("@@") ? "hunk" : l.startsWith("+") ? "add" : l.startsWith("-") ? "del" : "";
    return `<div class="${cls}">${esc(l) || " "}</div>`;
  }).join("")}</div>`;
}

function changesetHtml(c) {
  const icon = { create: "＋", modify: "✎", delete: "🗑" };
  const files = c.files.map(f => `<div class="file-line">${icon[f.action] || "•"} <code>${esc(f.path)}</code>
    <span class="small">${f.action === "create" ? "new file" : f.action === "delete" ? "deleted" : ""}</span> <span class="small">${diffStats(f.diff)}</span></div>`).join("");
  const diffs = `<details><summary>Show the changes</summary>${c.files.map(f => `<div class="small muted" style="margin-top:8px">${esc(f.path)}</div>${diffHtml(f.diff)}`).join("")}</details>`;
  const warnings = ((c.validation || {}).warnings || []).map(w => `<div class="small muted">⚠ ${esc(w)}</div>`).join("");
  const review = c.review ? `<details><summary>Claude reviewed this change</summary><div class="ai-body" style="font-size:14px;margin-top:6px">${md(c.review)}</div></details>` : "";
  if (c.status === "pending") {
    return `<div class="card accent">
      <div class="card-title">${avatar(c.backend)} ${NAME[c.backend] || c.backend} wants to change ${c.files.length === 1 ? "a file" : `${c.files.length} files`}</div>
      <div class="files">${files}</div>${warnings}${review}${diffs}
      <div class="card-sub">Nothing is changed until you apply. A backup of each file is kept.</div>
      <div class="row"><button class="btn primary" data-action="apply-cs" data-id="${c.id}">Apply changes</button>
        <button class="btn ghost" data-action="discard-cs" data-id="${c.id}">Discard</button></div></div>`;
  }
  if (c.status === "applied") {
    const tests = testCommands();
    return `<div class="card">
      <div class="card-title"><span class="badge-ok">✓</span> Changes applied</div>
      <div class="files">${files}</div>${review}${diffs}
      <div class="row">${tests.length ? `<button class="btn" data-action="run-cmd" data-cmd="${esc(tests[0])}">Run tests</button>` : ""}
        <button class="btn ghost" data-action="undo-cs" data-id="${c.id}">Undo</button></div></div>`;
  }
  const label = { rejected: "Changes discarded", reverted: "Changes undone", superseded: "Replaced by a newer version" }[c.status] || c.status;
  return `<div class="sys-note">${esc(label)} · ${c.files.map(f => esc(f.path)).join(", ")}</div>`;
}

function testCommands() {
  const s = (S.chat && S.chat.task.state.suggested_commands) || [];
  return [...new Set([...s, ...((S.facts && S.facts.test_commands) || [])])];
}

// A check that failed because a tool is not installed, not because the code is wrong.
function missingTool(out) {
  const m = out.match(/No module named '?([\w.-]+)'?/) || out.match(/'([\w.-]+)' is not recognized as an internal or external command/)
    || out.match(/([\w.-]+): (?:command )?not found/);
  return m ? m[1] : "";
}

function runHtml(r, isLatest) {
  const out = [r.stdout, r.stderr].filter(Boolean).join("\n").trim();
  const missing = r.status === "failed" ? missingTool(out) : "";
  const state = r.status === "running" ? `<span class="muted">running…</span>`
    : r.status === "passed" ? `<span class="badge-ok">✓ Passed</span>`
    : `<span class="badge-bad">✗ ${r.status === "timeout" ? "Timed out" : r.status === "cancelled" ? "Stopped" : "Failed"}</span>`;
  return `<div class="card">
    <div class="card-title">▶ <code>${esc(r.command)}</code> ${state}</div>
    ${r.status !== "running" ? `<div class="card-sub">Exit code ${r.exit_code ?? "–"} · ${r.duration_s != null ? r.duration_s.toFixed(1) : "?"} s</div>` : ""}
    ${missing ? `<div class="note-box">The check could not run: <strong>${esc(missing)}</strong> is not installed for this project's Python or PATH. Your code was not tested, so there is nothing to fix yet.
      ${/^[\w-]+$/.test(missing) ? ` Install it with <code>python -m pip install ${esc(missing)}</code> in a terminal, then use Run tests.` : ""}</div>` : ""}
    ${out ? `<details ${r.status !== "passed" && isLatest && !missing ? "open" : ""}><summary>Output</summary><pre class="run-out">${esc(out.slice(-8000))}</pre></details>` : ""}
    <div class="row">
      ${r.status === "running" ? `<button class="btn small" data-action="stop-run" data-id="${r.id}">Stop</button>` : ""}
      ${isLatest && !missing && ["failed", "timeout", "error"].includes(r.status) && !(S.chat && S.chat.task.workflow === "team") ? `<button class="btn primary small" data-action="fix-run" data-id="${r.id}">Fix it</button>` : ""}
    </div></div>`;
}

function statusHtml(t) {
  const state = t.state || {};
  const run = state.team_run || {};
  if ((t.workflow === "team" || run.lead) && ["running", "queued"].includes(t.status)) {
    const who = run.lead ? `${avatar(t.workflow === "chatgpt_leads" ? "codex" : "claude")}${t.workflow === "chatgpt_leads" ? "ChatGPT" : "Claude"}<span class="where">· leading · team ${esc((run.status || "working").replace(/_/g, " "))}</span>`
      : `${avatar("team")}Team<span class="where">· ${esc(run.status || "working")}</span>`;
    return `<div class="msg-ai live"><div class="ai-head">${who}<span class="where" id="live-model">${esc(modelLabel(state.active_agent))}</span></div>
      <div class="ai-body streaming" id="live-body"></div>
      ${progressHtml(state.active_agent?.progress, "live-thinking")}
      <div id="live-wait"><span class="typing"><span></span><span></span><span></span></span></div>
      <div class="small muted">Open <a href="#" data-action="toggle-activity">Activity</a> to see each role, its model and why.</div></div>`;
  }
  if (t.status === "awaiting_claude" && run.claude_request) {
    const r = run.claude_request;
    return `<div class="card accent">
      <div class="card-title">${avatar("claude")} May the team use Claude in this run?</div>
      <div class="card-sub">The smart router picked Claude for these parts:</div>
      <div class="small">${r.tasks.map(x => `• ${esc(x)}`).join("<br>")}</div>
      <div class="small">At most ${r.budget} Claude requests, for this run only. Each one gets only the relevant files, with secrets removed. This uses your Claude plan.</div>
      <div class="row"><button class="btn accent" data-action="claude-yes">Allow for this run</button>
        <button class="btn" data-action="claude-no">Use ChatGPT instead</button></div></div>`;
  }
  if (t.status === "paused") {
    return `<div class="card"><div class="card-title">${avatar("team")} Team run paused</div>
      <div class="card-sub">TEAM ROLES was switched off. Finished work and the plan are kept.</div>
      <div class="row"><button class="btn primary" data-action="team-toggle">Turn TEAM ROLES on and continue</button>
        <button class="btn ghost" data-action="stop">Cancel the run</button></div></div>`;
  }
  if (["running", "queued"].includes(t.status)) {
    const b = state.active_agent?.provider || t.owner || "codex";
    return `<div class="msg-ai live">
      <div class="ai-head">${avatar(b)}${NAME[b]}<span class="where" id="live-model">${esc(modelLabel(state.active_agent) || "Selecting model…")}</span></div>
      <div class="ai-body streaming" id="live-body"></div>
      ${progressHtml(state.active_agent?.progress, "live-thinking")}
      <div id="live-wait"><span class="typing"><span></span><span></span><span></span></span>
        <div class="small muted" id="live-hint">${b === "claude" ? "Claude is working on it" : "Thinking"}<span id="live-elapsed"></span></div></div></div>`;
  }
  if (t.status === "awaiting_claude" && state.pending_claude) {
    const p = state.pending_claude;
    const why = (t.escalation_reason || t.routing_reason || "").replace(/^The local model/, "Qwen");
    return `<div class="card accent">
      <div class="card-title">${avatar("claude")} Ask Claude for help?</div>
      <div class="card-sub">${esc(why)}</div>
      <div class="small">Claude will see ${p.files.length ? `${p.files.length} file${p.files.length > 1 ? "s" : ""}, ` : ""}${(p.images || []).length ? `${p.images.length} image${p.images.length > 1 ? "s" : ""} and ` : ""}about ${p.chars.toLocaleString()} characters from this chat.
        Secrets are removed from text first; images are sent as they are. This uses your Claude plan.</div>
      ${(p.images || []).length ? `<div class="msg-images left">${p.images.map(id => `<img src="${imgUrl(id)}" alt="Image to send">`).join("")}</div>` : ""}
      ${p.files.length ? `<div>${p.files.map(f => `<span class="file-chip"><code>${esc(f.path)}</code></span>`).join("")}</div>` : ""}
      <div class="row"><button class="btn accent" data-action="claude-yes">Send to Claude</button>
        <button class="btn" data-action="claude-no">Use ChatGPT instead</button>
        <button class="btn ghost small" data-action="claude-preview">See exactly what is sent</button></div></div>`;
  }
  if (state.pending_folder && !["running", "queued"].includes(t.status)) {
    const f = state.pending_folder;
    return `<div class="card accent">
      <div class="card-title">${FOLDER_ICON} Work in ${esc(f.is_enoy ? "Enoy's own source code" : f.name)}?</div>
      ${f.reason ? `<div class="card-sub">${esc(f.reason)}</div>` : ""}
      <div class="small"><code>${esc(f.path)}</code></div>
      <div class="small">Enoy moves this chat to that folder and continues with your last message there.${f.is_enoy ? " Changes to Enoy itself take effect after you restart Enoy; Undo restores the files." : " Changes there can be undone."}</div>
      <div class="row"><button class="btn accent" data-action="folder-yes">Move and continue</button>
        <button class="btn" data-action="folder-no">Stay here</button></div></div>`;
  }
  if (t.status === "failed") {
    return `<div class="card bad"><div class="card-title">Something went wrong</div>
      <div class="card-sub">${esc((t.summary || "").replace(/^The local model/, "Qwen"))}</div>
      <div class="row"><button class="btn" data-action="resume">Try again</button>
        ${t.owner !== "claude" && claudeOk() ? `<button class="btn" data-action="ask-claude">Ask Claude</button>` : ""}
        ${!codexOk() ? `<button class="btn ghost" data-action="open-settings" data-tab="status">Check status</button>` : ""}
        ${/PC access|mode allows/.test(t.summary || "") ? `<button class="btn primary" data-action="open-settings" data-tab="pc">Memory settings</button>` : ""}</div></div>`;
  }
  if (t.status === "interrupted") {
    return `<div class="card"><div class="card-title">This chat was interrupted</div>
      <div class="card-sub">The app closed while it was working. Everything so far is saved.</div>
      <div class="row"><button class="btn primary" data-action="resume">Continue</button></div></div>`;
  }
  if (t.status === "cancelled") return `<div class="sys-note">Stopped</div>`;
  return "";
}

// Live tokens: re-render only the streaming bubble, at most once per frame.
// Tokens often arrive in bursts (a whole sentence at once), so the text shown
// catches up with the text received a fraction of the gap per frame: small gaps
// close within a few frames, a large chunk eases in over about half a second,
// and a very large backlog (opening a chat mid-answer) is shown at once. With
// reduced motion everything received is shown immediately. Only the Markdown
// block still being written is redrawn.
let liveFrame = null;
const REDUCE_MOTION = matchMedia("(prefers-reduced-motion: reduce)");
function nextShown(id, full) {
  let n = S.shown[id] || 0;
  if (n > full.length) n = 0;
  const gap = full.length - n;
  if (REDUCE_MOTION.matches || gap > 1500) n = full.length;
  else if (gap > 0) n += Math.min(gap, Math.ceil(gap * 0.16) + 1);
  S.shown[id] = n;
  return n;
}
function renderLive(now = false) {
  const paint = () => {
    liveFrame = null;
    const body = $("live-body");
    if (!body || !S.chat || S.view !== "chat") return;
    const stick = nearBottom();
    const id = S.chat.task.id;
    const full = S.stream[id] || "";
    const text = full.slice(0, nextShown(id, full));
    MD.renderInto(body, text, true);
    if (text.length < full.length) liveFrame = requestAnimationFrame(paint);
    const wait = $("live-wait");
    if (wait) wait.classList.toggle("hidden", !!text);
    const agent = S.chat.task.state.active_agent || {};
    const model = $("live-model");
    if (model) model.textContent = modelLabel(agent);
    const think = (agent.progress || []).join("\n\n") || S.thinking[id] || "Working on your request…";
    const wrap = $("live-thinking-wrap");
    if (wrap) {
      const th = $("live-thinking");
      if (th.textContent !== think) th.textContent = think;
      const brief = wrap.querySelector(".thinking-brief");
      if (brief) brief.textContent = think.split("\n").filter(Boolean).at(-1)?.replace(/\s+/g, " ").slice(0, 135) || "Working on your request…";
    }
    if (stick) toBottom();
  };
  if (now) { if (liveFrame) cancelAnimationFrame(liveFrame); paint(); return; }
  if (!liveFrame) liveFrame = requestAnimationFrame(paint);
}
setInterval(() => {
  const el = $("live-elapsed");
  if (!el || !S.chat) return;
  const s = Math.round(Date.now() / 1000 - (S.chat.task.state.active_agent?.started || S.chat.task.updated));
  el.textContent = s > 3 ? ` · ${s}s` : "";
}, 1000);

// ---------------------------------------------------------------- composer

const AUTO_SIZE = CSS.supports("field-sizing", "content");
function renderComposer() {
  if (regularChat()) {
    $("composer-wrap").innerHTML = S.chat ? `<div class="chatgpt-history-note"><span>This is a saved Enoy conversation. Regular ChatGPT conversations stay on ChatGPT.</span><div class="provider-actions"><button class="btn primary" data-action="open-chatgpt">Open ChatGPT ↗</button><button class="btn" data-action="integrated-chat">Continue here with Codex (uses allowance)</button></div></div>` : "";
    return;
  }
  const busy = S.chat && ["running", "queued"].includes(S.chat.task.status);
  const general = S.project && S.project.is_default;
  const placeholder = S.chat ? "Reply…" : mode() === "chat" ? "What's on your mind?" : S.project ? `Ask about ${S.project.name}, or describe a change…` : "Describe what to build or change. You'll pick the folder next.";
  $("composer-wrap").innerHTML = `
    <div class="composer">
      ${S.images.length ? `<div class="img-chips">${S.images.map(im => `<span class="img-chip"><a href="#" data-action="view-image" data-id="${esc(im.id)}" aria-label="Preview attached image"><img src="${im.src}" alt="Attached image"></a>
        <button data-action="unattach-image" data-id="${im.id}" title="Remove image" aria-label="Remove image">✕</button></span>`).join("")}</div>` : ""}
      ${S.attached.length ? `<div class="chips">${S.attached.map(p => `<span class="chip">📄 ${esc(p)}<button data-action="unattach" data-path="${esc(p)}" title="Remove">✕</button></span>`).join("")}</div>` : ""}
      <textarea id="prompt" rows="1" placeholder="${esc(placeholder)}"></textarea>
      <input type="file" id="img-input" accept="image/png,image/jpeg,image/webp,image/gif,image/bmp" multiple hidden>
      <input type="file" id="file-input" multiple hidden>
      <div class="bar">
        <button class="round-btn plus" data-action="plus-menu" title="Add images or files, run tests…" aria-label="Add"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg></button>
        ${mode() === "code" && !S.chat ? `<button class="comp-chip" data-action="project-menu" title="${esc(S.project ? S.project.path : "Choose the folder Enoy works in")}">${FOLDER_ICON}<span class="t">${S.project ? esc(S.project.name) : "Choose folder"}</span>${CHEV}</button>` : ""}
        <div id="team-toggle"></div><button class="team-pill" data-action="starting-model-menu" title="Starting Codex model for each message">Start: ${esc(S.startingModel ? S.startingModel.model : mode() === "chat" ? (S.config?.codex?.chat_model || "gpt-6-luna") : "Default")}${CHEV}</button>
        <div class="grow"></div>
        <div id="model-picker"></div>
        ${busy ? `<button class="round-btn stop" data-action="stop" title="Stop" aria-label="Stop"><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="7" y="7" width="10" height="10" rx="2" fill="currentColor"/></svg></button>`
               : `<button class="round-btn send" id="send-btn" data-action="send" title="Send (Enter)" aria-label="Send" disabled><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 19V5M5 12l7-7 7 7"/></svg></button>`}
      </div>
    </div>
    <div class="composer-note">${composerNoteHtml()}</div>`;
  const ta = $("prompt");
  const draft = S.draft || "";
  ta.value = draft;
  const resize = () => {
    if (!AUTO_SIZE) { ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight, 260) + "px"; }
    const b = $("send-btn"); if (b) b.disabled = !ta.value.trim() && !S.images.length;
  };
  ta.addEventListener("input", () => { S.draft = ta.value; resize(); showSlashSuggestions(ta); });
  ta.addEventListener("paste", e => {
    const files = [...(e.clipboardData ? e.clipboardData.files : [])].filter(f => f.type.startsWith("image/"));
    if (files.length) { e.preventDefault(); addImages(files); }
  });
  $("img-input").addEventListener("change", e => { addImages([...e.target.files]); e.target.value = ""; });
  $("file-input").addEventListener("change", e => { addDroppedFiles([...e.target.files]); e.target.value = ""; });
  ta.addEventListener("keydown", e => {
    if (e.key === "Tab" && ta.value.trimStart().startsWith("/") && !$("menu").classList.contains("hidden")) {
      const first = $("menu").querySelector('[data-action="insert-command"]');
      if (first) { e.preventDefault(); first.click(); }
      return;
    }
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
  });
  resize();
  renderModelPicker();
  renderTeamToggle();
}

function localOnlyReason() {
  if (S.status && S.status.privacy_never) return "Settings say never to send code to Claude";
  if (S.project && S.project.local_only) return "this project is local-only";
  if (S.chat ? S.chat.task.state.local_only : S.localOnly) return "this chat stays on your PC";
  return "";
}
function composerNoteHtml() {
  return (teamOn() && !(S.project && S.project.is_default) ? "Team roles on: code changes are planned, checked and reviewed. " : "") + composerNote();
}
function composerNote() {
  const blocked = localOnlyReason();
  if (blocked) return `Cloud access disabled: ${esc(blocked)}. Enable cloud access to use ChatGPT or Claude.`;
  if (mode() === "chat" && S.model !== "claude") return "Uses your Codex plan allowance. Choose a starting model above.";
  return S.model === "claude" ? "Claude uses your Claude Code sign-in." : "ChatGPT leads: evaluates your request, delegates to Codex or Claude, and reviews shared results.";
}

async function send(textOverride) {
  if (regularChat()) { toast("Use Open ChatGPT to start a conversation in your browser."); return; }
  const ta = $("prompt");
  let text = (textOverride ?? (ta ? ta.value : "")).trim();
  if (S.uploading) { toast("Wait a moment: an image is still being added."); return; }
  const images = textOverride === undefined ? S.images.map(im => im.id) : [];
  if (!text && images.length) text = images.length > 1 ? "What is in these images?" : "What is in this image?";
  if (!text || S.sending) return;
  if (text.startsWith("/")) { await runSlashCommand(text); return; }
  if (!S.project) {
    // Code mode with no folder: ask for one, then send this message into it.
    S.draft = text; S.sendAfterOpen = true;
    await pickFolder();
    return;
  }
  if (S.chat && ["running", "queued"].includes(S.chat.task.status)) return;
  const backend = S.model === "auto" ? null : S.model;
  if (!S.status) await loadStatus();  // Sent before the first status check finished.
  if (backend === "claude" && !claudeOk()) { toast("Claude is not available right now. Pick Auto or ChatGPT.", true); return; }
  if (backend === "claude" && localOnlyReason()) { toast(`Claude is off here: ${localOnlyReason()}. Change that first.`, true); return; }
  if (coordinatorOn() && !S.status.codex?.available) { toast("ChatGPT coordinator is unavailable. Run codex login, then refresh status in Settings.", true); return; }
  if (!codexOk() && backend !== "claude" && !coordinatorOn()) { toast("Codex is unavailable. Run codex login, then refresh status.", true); return; }
  const files = [...S.attached];
  S.sending = true;
  const ok = await guarded(async () => {
    if (S.chat) {
      await api("POST", `/api/tasks/${S.chat.task.id}/message`, { text, backend, files, images, starting_model: S.startingModel });
      await loadChat(S.chat.task.id);
    } else {
      const d = await api("POST", "/api/tasks", { project_id: S.project.id, objective: text, backend, files, images, local_only: !!S.localOnly, team: !!S.team, starting_model: S.startingModel });
      await loadChat(d.task.id);
    }
    return true;
  });
  S.sending = false;
  if (!ok) return;  // Keep the draft and attachments so nothing typed is lost.
  S.draft = ""; S.attached = [];
  if (images.length) S.images = [];
  await guarded(loadChats);
  renderAll(true);
}

function showSlashSuggestions(ta) {
  const value = ta.value.trimStart();
  if (!value.startsWith("/") || /\s/.test(value)) { closeMenu(); return; }
  const choices = SLASH_COMMANDS.filter(c => c.name.startsWith(value.toLowerCase()));
  if (!choices.length) { closeMenu(); return; }
  openMenu(ta, choices.map(c => ({ title: `<code>${esc(c.name)}</code>`, desc: c.desc,
    action: "insert-command", data: { command: c.name } })));
}

async function runSlashCommand(text) {
  const [rawName, ...args] = text.trim().split(/\s+/);
  const name = rawName.toLowerCase();
  const clear = () => { S.draft = ""; closeMenu(); renderComposer(); };
  if (!SLASH_COMMANDS.some(c => c.name === name)) {
    toast(`Unknown command: ${rawName}. Type /help to see available commands.`, true);
    return;
  }
  if (name === "/help") {
    if (args.length) { toast("Usage: /help", true); return; }
    clear();
    openModal(`<h2>Commands</h2><div class="stack small">${SLASH_COMMANDS.map(c => `<div><code>${esc(c.name)}</code> — ${esc(c.desc)}${c.name === "/compact" ? ` <span class="muted">(optional: add a short focus note)</span>` : ""}</div>`).join("")}</div>
      <div class="row" style="justify-content:flex-end;margin-top:16px"><button class="btn primary" data-action="close-modal">Close</button></div>`);
    return;
  }
  if (name === "/new") {
    if (args.length) { toast("Usage: /new", true); return; }
    clear(); actions["new-chat"](); return;
  }
  if (name === "/model") {
    if (args.length) { toast("Usage: /model", true); return; }
    clear();
    const button = $("model-picker")?.querySelector("button");
    if (button) actions["model-menu"](button); else toast("The model picker is not available here.", true);
    return;
  }
  if (name === "/status") {
    if (args.length) { toast("Usage: /status", true); return; }
    clear();
    await loadStatus(true);
    if (!S.status) { toast("Could not load provider status.", true); return; }
    const line = ["codex", "claude"].map(k => `${NAME[k]}: ${S.status[k]?.available ? "available" : "unavailable"}`).join(" · ");
    toast(line);
    return;
  }
  if (name === "/resume") {
    if (args.length) { toast("Usage: /resume", true); return; }
    if (!S.chat || !["failed", "cancelled", "interrupted"].includes(S.chat.task.status)) {
      toast("There is no stopped or failed chat to resume.", true); return;
    }
    clear(); actions.resume(); return;
  }
  if (name === "/compact") {
    if (!S.chat) { toast("Open a saved chat before using /compact.", true); return; }
    const focus = text.trim().slice(rawName.length).trim();
    const result = await guarded(async () => {
      const d = await api("POST", `/api/tasks/${S.chat.task.id}/compact`, { focus });
      await loadChat(d.task.id);
      return d;
    });
    if (!result) return;
    clear();
    toast("Chat context compacted. Your transcript is still saved.");
  }
}

function renderAll(forceBottom = false) {
  renderSidebar();
  renderChatMenu();
  if (S.view && S.view !== "chat") { renderPage(); return; }
  renderThread(forceBottom);
  renderComposer();
  const ta = $("prompt");
  const modalOpen = !$("modal").classList.contains("hidden");
  if (ta && !modalOpen) ta.focus();
}

// ---------------------------------------------------------------- files drawer

async function renderDrawer() {
  const dr = $("drawer");
  dr.classList.toggle("hidden", !S.drawer);
  if (!S.drawer || !S.project) return;
  if (!S.tree) {
    try { S.tree = await api("GET", `/api/projects/${S.project.id}/tree`); } catch (e) { toast(e.message, true); }
  }
  const node = n => n.type === "dir"
    ? `<details><summary>${esc(n.name)}</summary>${n.children.map(node).join("")}</details>`
    : `<div class="file ${n.sensitive ? "prot" : ""}" data-action="preview-file" data-path="${esc(n.path)}"
         title="${n.sensitive ? "Protected: secret files are never read or sent" : esc(n.path)}">${esc(n.name)}</div>`;
  const body = S.searchResults
    ? `<div class="small muted" style="padding:4px 6px">${S.searchResults.length} result(s) · <a href="#" data-action="clear-search">clear</a></div>` +
      S.searchResults.map(r => `<div class="tree"><div class="file" style="padding-left:6px" data-action="preview-file" data-path="${esc(r.path)}">${esc(r.path)}${r.line ? `:${r.line}` : ""}
        <div class="small muted" style="white-space:normal">${esc(r.text)}</div></div></div>`).join("")
    : S.tree ? `<div class="tree">${S.tree.tree.children.map(node).join("")}</div>` : `<div class="muted small">Loading…</div>`;
  const pv = S.preview;
  dr.innerHTML = `
    <div class="dr-head"><span>${esc(S.project.name)}</span><button class="icon-btn" data-action="toggle-files">✕</button></div>
    <div class="dr-search"><input type="text" id="file-search" placeholder="Search files" value="${esc(S.search)}"></div>
    <div class="dr-body">${body}</div>
    ${pv ? `<div class="preview"><div class="ph"><code style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(pv.path)}</code>
      ${pv.error ? "" : `<button class="btn small" data-action="attach-file" data-path="${esc(pv.path)}">${S.attached.includes(pv.path) ? "Added ✓" : "Add to chat"}</button>`}</div>
      <pre>${esc(pv.error || pv.content || "Loading…")}</pre></div>` : ""}`;
  $("file-search").addEventListener("keydown", e => { if (e.key === "Enter") doSearch(e.target.value); });
}

async function doSearch(q) {
  S.search = q;
  if (!q.trim()) { S.searchResults = null; renderDrawer(); return; }
  await guarded(async () => {
    S.searchResults = (await api("GET", `/api/projects/${S.project.id}/search?q=${encodeURIComponent(q)}`)).results;
    renderDrawer();
  });
}

// ---------------------------------------------------------------- settings

async function openSettings(tab) {
  S.settingsTab = tab || S.settingsTab || "general";
  const meta = await api("GET", "/api/config");
  S.config = meta.config;
  S.cfgMeta = meta;
  renderSettings();
}

function renderSettings() {
  const tabs = [["general", "General"], ["updates", "Updates"], ["appearance", "Colours"], ["models", "Models"], ["tools", "Tools"], ["pc", "PC access"], ["safety", "Safety"], ["status", "Status & help"], ["activity", "Activity"]];
  openModal(`<div class="set-nav"><h2>Settings</h2>
    <button class="set-nav-about ${S.settingsTab === "about" ? "active" : ""}" data-action="settings-tab" data-tab="about">About Enoy</button>
    <div class="set-nav-divider" role="separator" aria-label="Settings sections">—</div>${tabs.map(([k, l]) =>
    `<button class="${S.settingsTab === k ? "active" : ""}" data-action="settings-tab" data-tab="${k}">${l}</button>`).join("")}
    <div class="grow"></div><button data-action="close-modal">Close</button></div>
    <div class="set-body" id="set-body"><div class="muted">Loading…</div></div>`, true);
  ({ about: settingsAbout, general: settingsGeneral, updates: settingsUpdates, appearance: settingsAppearance, models: settingsModels, tools: settingsTools, pc: settingsMemoryPc, safety: settingsSafety, status: settingsStatus, activity: settingsActivity })[S.settingsTab]();
}

function settingsAbout() {
  $("set-body").innerHTML = `<div class="set-section"><h3>About Enoy</h3>
    <p class="small muted">Enoy uses the lowest cost model that fits each step, then checks recorded results.</p>
    <div class="stack-map"><div><strong>1 · Plan</strong><span>Codex reads a bounded project snapshot and chooses tasks.</span></div><div><strong>2 · Build</strong><span>Luna handles routine work; specialist tasks route to Sol, Terra, Astra, or Claude when useful and available.</span></div><div><strong>3 · Check</strong><span>Local syntax and test commands run without model tokens. Larger or risky changes get a model review.</span></div><div><strong>4 · Reuse</strong><span>Later requests receive short shared records instead of the entire conversation.</span></div></div>
    <p class="small muted">The savings number compares omitted conversation characters with a full-history baseline. It is an estimate, not billed tokens. Open Estimated token savings and press <strong>Verify saved work</strong> to audit the stored records without a model call.</p></div>`;
}

function toggleRow(sec, key, label, desc) {
  return `<div class="set-row"><div class="lbl"><span>${esc(label)}</span>${desc ? `<span class="d">${esc(desc)}</span>` : ""}</div>
    <input type="checkbox" data-cfg="${sec}.${key}" ${S.config[sec][key] ? "checked" : ""}></div>`;
}
function inputRow(sec, key, label, desc, type = "text") {
  return `<label class="field">${esc(label)}<input type="${type}" step="any" data-cfg="${sec}.${key}" value="${esc(S.config[sec][key])}">${desc ? `<span class="small">${esc(desc)}</span>` : ""}</label>`;
}

async function settingsTools() {
  const c = S.config.tools;
  let info = { tools: [], keys: {}, search_order: [] };
  try { info = await api("GET", "/api/tools"); } catch (e) { /* shown as empty */ }
  const row = t => `<div class="set-row"><div class="lbl"><span><code>${esc(t.name)}</code></span>
      <span class="d">${esc(t.description)}${t.allowed ? "" : ` <b>Not available now:</b> ${esc(t.why)}`}</span></div>
      <input type="checkbox" data-cfg="tools.enabled_tools.${esc(t.name)}" ${c.enabled_tools[t.name] !== false ? "checked" : ""}
        aria-label="Allow ${esc(t.name)}"></div>`;
  const group = g => info.tools.filter(t => t.group === g).map(row).join("");
  $("set-body").innerHTML = `
    <div class="set-section"><h3>Tools</h3>
      <div class="small muted">Models ask Enoy to run these; Enoy checks the permission, runs the tool and gives the real result back,
        including errors. Every call is listed in Activity. Tools only read: nothing here changes your PC or sends anything on your behalf.</div>
      ${toggleRow("tools", "enabled", "Let models use tools", "Off: models answer only from what is in the chat.")}
      ${toggleRow("tools", "online", "Online tools", "Weather, web search and reading web pages. Off: nothing leaves your PC through tools.")}
      ${toggleRow("tools", "claude_web", "Claude may search the web", "For questions about current information, Claude gets Claude Code's own WebSearch and WebFetch (nothing else). Uses your Claude plan.")}
      ${toggleRow("tools", "claude_enoy_mcp", "Claude calls Enoy's PC and weather tools itself", "Off (default): Enoy reads the PC facts first and puts them in Claude's request. On: Claude Code runs without its safe mode so Enoy's tool server can load; your own Claude Code settings, skills and other MCP servers stay off. Tested: Claude called Enoy's GPU tool and answered from it.")}
      <div class="grid2">${inputRow("tools", "max_rounds", "Tool rounds per answer", "After this, the model must answer with what it has.", "number")}</div></div>
    <div class="set-section"><h3>This PC (read-only)</h3>
      <div class="small muted">Answered by Windows, no administrator rights. Allowed by the "hardware" switch in Memory &amp; PC.</div>
      ${group("pc")}</div>
    <div class="set-section"><h3>Online</h3>${group("web")}
      <label class="field">Web search provider<select data-cfg="tools.search_provider">${["auto", "brave", "searxng", "wikipedia"].map(p =>
        `<option ${c.search_provider === p ? "selected" : ""} value="${p}">${p === "auto" ? "Automatic (first one that is set up)" : p === "brave" ? "Brave Search (your API key)" : p === "searxng" ? "SearXNG (your instance)" : "Wikipedia only (background, not news)"}</option>`).join("")}</select>
        <span class="small">In use now: ${esc(info.search_order.join(" → ") || "none")}. Without Brave or SearXNG, Enoy's web search only finds Wikipedia articles.</span></label>
      <label class="field">Brave Search API key
        <div class="row"><input type="password" id="brave-key" autocomplete="off" placeholder="${info.keys.brave_search ? "Saved in Windows Credential Manager" : "Not set"}">
        <button class="btn" data-action="tools-key" data-name="brave_search">Save</button>
        ${info.keys.brave_search ? `<button class="btn ghost" data-action="tools-key" data-name="brave_search" data-clear="1">Remove</button>` : ""}</div>
        <span class="small">Stored in Windows Credential Manager, never in Enoy's files, logs or prompts. Brave charges per search (prepaid credits with a monthly free credit); check the current terms on brave.com/search/api.
          Searches this month: ${info.brave_usage ? info.brave_usage.count : 0} of ${c.brave_monthly_limit}.</span></label>
      <div class="grid2">${inputRow("tools", "brave_monthly_limit", "Brave searches per month", "Enoy stops using Brave after this many and falls back to the next provider. 0 = never use Brave.", "number")}</div>
      <div class="grid2">${inputRow("tools", "searxng_url", "SearXNG address", "Optional. JSON output must be enabled on the instance.")}
        ${inputRow("tools", "contact", "Contact for weather services", "Optional e-mail or website added to requests; MET Norway asks for one.")}</div>
      <div class="small muted">Weather: SMHI open data (Nordic countries) and MET Norway (elsewhere), both CC BY 4.0; place names via OpenStreetMap Nominatim.</div></div>`;
  bindCfgInputs();
}

function settingsGeneral() {
  const theme = lsGet("theme") || "dark";
  const p = S.project;
  $("set-body").innerHTML = `
    <div class="set-section"><h3>Chat experience</h3>
      <label class="field">Default for Chat<select id="chat-experience">
      <option value="chatgpt" ${(S.config?.chat?.experience || "chatgpt") === "chatgpt" ? "selected" : ""}>Regular ChatGPT (opens in your browser)</option>
      <option value="integrated" ${S.config?.chat?.experience === "integrated" ? "selected" : ""}>Integrated Codex chat (uses Codex allowance)</option>
      </select><span class="small muted">Regular ChatGPT uses its own page and account limits. Enoy sends no model request when opening it.</span></label></div>
    <div class="set-section"><h3>Appearance</h3>
      <div class="set-row"><div class="lbl"><span>Theme</span></div>
        <select id="theme-select">${["system", "light", "dark"].map(t => `<option value="${t}" ${t === theme ? "selected" : ""}>${t[0].toUpperCase() + t.slice(1)}</option>`).join("")}</select></div></div>
    <div class="set-section"><h3>Project</h3>
      <div class="set-row"><div class="lbl"><span>${p && !p.is_default ? esc(p.name) : "No project open"}</span><span class="d">${p && !p.is_default ? esc(p.path) : (mode() === "code" ? "Choose a folder for your development workspace." : "Open a folder in Code to work on a project.")}</span></div>
        <button class="btn" data-action="pick-folder">Open folder…</button></div></div>
    <div class="set-section"><h3>Accounts and setup</h3>
      <p class="small muted">Use your own ChatGPT and Claude accounts. Install missing tools, then open each provider to sign in.</p>
      <div class="provider-actions"><button class="btn" data-action="install-providers">Install missing tools</button>
      <button class="btn" data-action="provider-status" data-provider="codex">Connect Codex</button>
      <button class="btn" data-action="provider-status" data-provider="claude">Connect Claude</button></div></div>
    `;
  $("theme-select").addEventListener("change", e => {
    applyTheme(e.target.value); lsSet("theme", e.target.value);
    api("POST", "/api/appearance", {theme:e.target.value}).catch(error=>toast(error.message,true));
  });
  $("chat-experience").addEventListener("change", e => guarded(async () => {
    await setChatExperience(e.target.value);
  }));
}

function releaseSummaryItems(notes) {
  const source = String(notes || "").trim();
  if (/^Automatic stable Windows release from main commit [a-f\d]+\.?$/i.test(source)) {
    return ["Stable Windows build with the latest Enoy changes."];
  }
  const lines = source.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
  const result = [];
  let inChanges = !lines.some(line => /^#{1,4}\s*what.s changed\s*$/i.test(line.replace(/\*/g, "")));
  for (const line of lines) {
    const heading = line.replace(/^#+\s*/, "").replace(/\*/g, "").trim();
    if (/^(new contributors|contributors|full changelog)/i.test(heading)) break;
    if (/^what.s changed$/i.test(heading)) { inChanges = true; continue; }
    if (/^#{1,4}\s/.test(line) && inChanges && result.length) break;
    if (!inChanges) continue;
    if (/^\*\*/.test(line) && !result.length) continue;
    const item = line.replace(/^[-*+]\s+/, "")
      .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
      .replace(/\s+by\s+@[\w-]+\s*$/i, "")
      .replace(/[`*_]/g, "").trim();
    if (item && !/^full changelog/i.test(item)) result.push(item);
    if (result.length >= 5) break;
  }
  if (!result.length) return ["Release notes are not available for this version yet."];
  return result;
}

async function settingsUpdates() {
  const body = $("set-body");
  body.innerHTML = `<div class="set-section"><h3>Updates</h3><p class="small muted">Check for the latest stable Enoy release. Update installs are verified and keep your chats and settings.</p><div class="update-release" id="update-release"><span class="small muted">Loading latest release…</span></div><p id="update-state" class="small muted" role="status">Updates are checked when you click.</p><div class="provider-actions"><button id="update-check" class="btn" data-action="check-updates">Check for updates</button><a class="btn" href="https://github.com/anglerequip/Enoy/releases" target="_blank" rel="noopener noreferrer">All releases</a></div></div>`;
  renderUpdateStatus(S.updates);
  try {
    const [release, status] = await Promise.all([api("GET", "/api/updates/latest"), api("GET", "/api/updates")]);
    if (S.settingsTab !== "updates" || !$("update-release")) return;
    S.latestRelease = release;
    S.updates = status;
    if (!release) {
      $("update-release").innerHTML = `<p class="small muted">No stable Enoy release is available yet.</p>`;
      renderUpdateStatus(S.updates);
      return;
    }
    const date = release.published_at ? new Date(release.published_at).toLocaleDateString() : "Stable release";
    const notes = releaseSummaryItems(release.notes).map(item => `<li>${esc(item)}</li>`).join("");
    $("update-release").innerHTML = `<div class="update-release-top"><div><span class="small muted">Latest stable</span><h4>${esc(release.name || `Enoy ${release.version}`)}</h4></div><span class="update-version">v${esc(release.version)}</span></div><p class="small muted update-date">Installed v${esc(status.current_version || "unknown")} · ${esc(date)}</p><ul class="update-notes">${notes}</ul><a class="small settings-link" href="${esc(release.release_url)}" target="_blank" rel="noopener noreferrer">View release details</a>`;
    renderUpdateStatus(S.updates);
  } catch (_) {
    if (S.settingsTab === "updates" && $("update-release")) $("update-release").innerHTML = `<p class="small muted">Couldn’t load the latest release notes. Check your connection or open the releases page.</p>`;
  }
}

function settingsAppearance() {
  const editing = S.appearanceMode || mode();
  const pref = EnoyAppearance.preference(editing), colours = EnoyAppearance.colours(editing);
  $("set-body").innerHTML = `<div class="set-section colour-settings"><div class="colour-heading"><div><h3>Make it your space</h3><p class="small muted">Separate colours for Chat and Code. Saved on this PC.</p></div><div class="colour-mode" role="group" aria-label="Workspace colours">${["chat","code"].map(m=>`<button class="btn ${editing===m?"primary":"ghost"}" data-action="colour-mode" data-mode="${m}" aria-pressed="${editing===m}">${m==="chat"?"Chat":"Code"}</button>`).join("")}</div></div>
    <div class="palette-grid" role="group" aria-label="Colour presets">${EnoyAppearance.palettes.map(p=>`<button class="palette-choice ${pref.preset===p.id?"selected":""}" data-action="colour-preset" data-preset="${p.id}" aria-pressed="${pref.preset===p.id}"><span class="palette-swatches" aria-hidden="true"><i style="background:${p.light}"></i><i style="background:${p.dark}"></i><i style="background:${p.night}"></i></span><span>${p.name}</span>${pref.preset===p.id?`<span aria-hidden="true">✓</span>`:""}</button>`).join("")}</div>
    <div class="colour-preview" style="--preview-bg:${colours.background};--preview-panel:${colours.panel};--preview-accent:${colours.accent};--preview-ink:${colours.ink}"><div class="preview-mini-sidebar"></div><div class="preview-mini-body"><span class="small">${editing==="chat"?"Your everyday ideas":"Your next great project"}</span><strong>${editing==="chat"?"Room to think.":"Let's build something."}</strong><div class="preview-mini-composer"><span>Message Enoy…</span><i aria-hidden="true">↑</i></div></div></div>
    <h3>Fine-tune the colours</h3><div class="custom-colours">${[["accent","Accent"],["background","Background"],["sidebar","Sidebar"],["panel","Cards & composer"]].map(([key,label])=>`<label class="custom-colour"><span>${label}</span><span class="colour-value">${colours[key]}</span><input type="color" data-custom-colour="${key}" aria-label="${label} colour" value="${colours[key]}"></label>`).join("")}</div>
    <p id="colour-error" class="small danger-text" role="status"></p><button class="btn ghost" data-action="colour-reset">Reset ${editing=== "chat"?"Chat":"Code"} colours</button></div>`;
  document.querySelectorAll("[data-custom-colour]").forEach(input=>input.addEventListener("change",async()=>{
    const current=EnoyAppearance.preference(editing);
    const error=await saveWorkspaceColours(editing,{...current,custom:{...current.custom,[input.dataset.customColour]:input.value}});
    if (error) { $("colour-error").textContent=error; return; }
    settingsAppearance();
    if (typeof syncFrameTheme === "function") syncFrameTheme();
  }));
}

let colourSaveQueue=Promise.resolve();
async function saveWorkspaceColours(editing,pref) {
  const error=EnoyAppearance.save(editing,pref);
  if (error) return error;
  const save=()=>api("POST","/api/appearance",{mode:editing,...pref});
  const pending=colourSaveQueue.then(save);
  colourSaveQueue=pending.catch(()=>{});
  try { await pending; return ""; }
  catch (_) { return "The preview changed, but saving failed. Check the connection and try again."; }
}

async function setChatExperience(value) {
  const data = await api("POST", "/api/config", {config: {chat: {experience: value}}});
  S.config = data.config;
  renderAll();
}

function renderUpdateStatus(data) {
  const state = $("update-state"), button = $("update-check");
  if (!state || !button || !data) return;
  state.textContent = data.detail + (data.status === "downloading" ? ` ${data.progress || 0}%` : "");
  state.classList.toggle("danger-text", data.status === "error");
  button.disabled = ["checking", "downloading", "installing"].includes(data.status);
}

async function checkUpdates() {
  try {
    S.updates = await api("POST", "/api/updates", {});
    renderUpdateStatus(S.updates);
    while (["checking", "downloading"].includes(S.updates.status)) {
      await new Promise(resolve => setTimeout(resolve, 1000));
      S.updates = await api("GET", "/api/updates");
      renderUpdateStatus(S.updates);
    }
  } catch (e) {
    S.updates = {status: "error", detail: e.message};
    renderUpdateStatus(S.updates);
    toast(e.message, true);
  }
}

async function settingsModels() {
  const c=S.config;
  let blenderStatus = {detail: "Connection has not been checked."};
  try { blenderStatus = await api("GET", "/api/blender"); } catch (e) { blenderStatus.detail = e.message; }
  $("set-body").innerHTML = `<div class="set-section"><h3>ChatGPT and Claude</h3>
    <p>ChatGPT evaluates requests and delegates development tasks to Codex or Claude. Both share project context and recorded results.</p>
    ${toggleRow("codex","enabled","Enable Codex","Uses the official Codex CLI. Run codex login to sign in.")}
    ${inputRow("codex","executable","Codex executable","Empty: find codex on PATH")}
    ${inputRow("codex","model","Planning and review model","Default: gpt-6.1-sol")}
    ${inputRow("codex","effort","Planning reasoning effort","Default: medium")}
    ${toggleRow("codex","adaptive_routing","Use the recommended role-based model stack","Planning/review uses Sol medium; simple edits use Luna low; Luau, Blender and VFX scripting use Sol medium; visual Blender inspection uses Terra medium; unresolved exceptional problems use Astra high. Claude uses Opus for design and difficult review, and Sonnet for VFX art direction.")}
    ${inputRow("codex","execution_model","Execution model","Default: gpt-6-luna")}
    ${inputRow("codex","execution_effort","Execution reasoning effort","Default: low")}
    ${inputRow("codex","chat_model","Chat default model","Default: gpt-6-luna. Uses your Codex plan allowance.")}
    ${inputRow("codex","chat_effort","Chat reasoning effort","Default: low")}
    ${toggleRow("coordinator","claude_reviews","GPT plans, Claude reviews","Claude checks implementation plans before execution and reviews the final results. Unavailable reviews and GPT fallbacks are shown in Activity.")}
    ${toggleRow("coordinator","claude_complex","Claude handles complex work and repairs","Bounded routine implementation stays with the economical GPT execution model. Claude gets broad, risky or complex work and repair tasks when available.")}
    ${toggleRow("agent","native_tools","Native tools for ChatGPT and Claude","Inspect files, edit code, and run commands. Native edits are direct and are not Enoy undoable patches. Private file filters keep requests in filtered context mode.")}
    ${toggleRow("agent","self_check","Check Enoy's usage during work","Local, token-free review when a task completes. Actionable usage issues appear in Activity; no model request is added.")}
    <label class="field">Agent permission mode<select data-cfg="agent.access_mode">
      <option value="workspace" ${c.agent.access_mode === "workspace" ? "selected" : ""}>Workspace Access</option>
      <option value="full" ${c.agent.access_mode === "full" ? "selected" : ""}>Full Access</option>
    </select><span class="small">Workspace: work in the selected project. Full Access: explicitly authorize work across your PC using Enoy's Windows account, including from a general chat. Provider approval checks and Windows permissions still apply. This setting does not grant administrator privileges.</span></label>
    ${toggleRow("claude","enabled","Enable Claude","Uses your Claude Code sign-in.")}
    ${inputRow("claude","model","Claude default model","Default: claude-opus-5-5")}
    ${inputRow("claude","effort","Claude default reasoning effort","Default: medium; difficult debugging and final review route at high")}
    ${inputRow("routing","max_attempts","Attempts per task","","number")}
    ${inputRow("team","max_tasks","Maximum tasks per plan","","number")}
    ${inputRow("team","max_model_calls","Requests per run","","number")}
    ${inputRow("lead","max_followups","Final-review correction rounds","","number")}
    <p class="small muted">Enoy uses cloud providers only. Chats with cloud access disabled cannot send model requests.</p></div>
    <div class="set-section"><h3>Shared Blender workspace</h3>
    ${toggleRow("blender","enabled","Connect ChatGPT and Claude to Blender","Both providers work on the same live scene, including from general chats. Tool calls are serialized and recent actions are shared on handoff. Cloud privacy restrictions still apply. Scene edits happen directly in Blender.")}
    ${inputRow("blender","server_executable","MCP for Blender executable","Empty: detect the local installation automatically.")}
    ${inputRow("blender","host","Blender host","Loopback address of Blender on this PC.")}
    ${inputRow("blender","port","Blender MCP port","Default: 9876","number")}
    <p class="small muted">${esc(blenderStatus.detail)}</p>
    <button id="blender-check">Check Blender connection</button></div>`;
  bindCfgInputs();
  $("blender-check").onclick = () => settingsModels();
}

function settingsSafety() {
  const c = S.config;
  $("set-body").innerHTML = `
    <div class="set-section"><h3>Patch proposals (filtered context mode)</h3>
      ${toggleRow("permissions", "auto_apply_patches", "Apply changes without asking", "Off is safer. Deleting a file always asks.")}
      ${toggleRow("permissions", "test_after_apply", "Run the tests after each applied change", "Only when the test command is on the allowed list below; anything else still asks.")}
      <div class="small muted">Before a change is applied, Enoy checks that the files have not changed since the AI read them, keeps a backup of each, and lets you undo it.</div></div>
    <div class="set-section"><h3>Enoy verification commands</h3>
      <p class="small muted">Native agent commands use the provider's permission policy and the access mode in Models. This list controls Enoy's separate verification runner.</p>
      <label class="field">Commands that run without asking (one per line)
        <textarea class="field mono" rows="7" data-cfg="permissions.command_allowlist" data-list="1">${esc(c.permissions.command_allowlist.join("\n"))}</textarea></label>
      ${inputRow("permissions", "command_timeout_s", "Stop a command after (seconds)", "", "number")}
      <div class="small muted">Commands run in the project folder without a shell. Installing, deleting, network tools and anything not listed above ask first; system and admin tools are blocked.
        This is not a sandbox: an approved command runs with your normal Windows permissions.</div></div>
    <div class="set-section"><h3>Private files</h3>
      <label class="field">Extra paths the AI must never see (one pattern per line, e.g. assets/**)
        <textarea class="field mono" rows="4" data-cfg="permissions.extra_excluded_globs" data-list="1">${esc(c.permissions.extra_excluded_globs.join("\n"))}</textarea></label>
      <div class="small muted">Enoy's context packs exclude .env files, keys, certificates, password and cookie files and redact detected secrets. Native tools read files directly under the provider's policy. Configuring private file filters disables native tools so Enoy can enforce those filters.</div></div>
    <div class="set-section"><h3>What may go to Claude</h3>
      <label class="field">Cloud privacy (ChatGPT and Claude)<select data-cfg="privacy.cloud_policy">
        <option value="auto" ${c.privacy.cloud_policy === "auto" ? "selected" : ""}>Send the relevant, permitted files when Claude is needed</option>
        <option value="never" ${c.privacy.cloud_policy === "never" ? "selected" : ""}>Never send code to cloud models (everything stays on this PC)</option></select></label>
      <label class="field">Files never sent to ChatGPT or Claude (one pattern per line, e.g. src/secret/** or *.private.lua)
        <textarea class="field mono" rows="4" data-cfg="privacy.cloud_excluded_globs" data-list="1">${esc(c.privacy.cloud_excluded_globs.join("\n"))}</textarea></label>
      <div class="small muted">Claude only gets the few files relevant to the task, never the whole project. Before each request Enoy removes private files, any lines quoting them (from earlier answers, diffs or test output), and secrets, and logs a privacy check in Activity.
        You can also keep a whole project or a single chat on this PC from the project menu or the ＋ menu.</div></div>`;
  bindCfgInputs();
}

async function settingsStatus() {
  const d=await api("GET","/api/diagnostics");
  $("set-body").innerHTML=`<div class="set-section"><h3>Provider status</h3>${["codex","claude"].map(p=>`<p><strong>${NAME[p]}</strong>: ${esc(d[p]?.detail || "unavailable")}</p>`).join("")}<button class="btn" data-action="refresh-status">Refresh</button></div><div class="set-section"><h3>Enoy ${esc(d.app_version)}</h3><p>Data: ${esc(d.paths.data)}</p></div>`;
}

async function settingsActivity() {
  let tx = [];
  try { tx = (await api("GET", "/api/transmissions?backend=claude")).transmissions; }
  catch (e) {
    if (S.settingsTab === "activity") $("set-body").innerHTML = `<div class="page-state bad">Could not load activity: ${esc(e.message)}</div>`;
    return;
  }
  if (S.settingsTab !== "activity" || !$("set-body")) return;
  const chats = new Set(tx.map(x => x.task_id)).size;
  const files = tx.reduce((n, x) => n + x.files.length, 0);
  const latest = tx.length ? Math.max(...tx.map(x => x.created)) : null;
  $("set-body").innerHTML = `<div class="set-section"><h3>Claude activity, explained</h3>
    <p>When Claude helps with a chat, Enoy records what it sends here. This history covers Claude requests only.</p>
    ${tx.length ? `<div class="tx-summary"><strong>${tx.length} request(s) across ${chats} chat(s)</strong><p>${files} file attachment(s) in these requests. The same file may have been sent more than once.</p><span class="small muted">Most recent: ${new Date(latest * 1000).toLocaleString()}</span></div>
    <h3>What was shared</h3>
    ${tx.map(x => `<article class="tx-card"><div class="row"><strong>Chat #${esc(x.task_id)}</strong><span class="small muted">${new Date(x.created * 1000).toLocaleString()}</span></div>
      <p>Sent a request to Claude with ${x.files.length ? `${x.files.length} file(s) for context` : "no attached files"}.</p>
      <details><summary>More · files, size & token counts</summary>
        <p><strong>Files:</strong> ${x.files.map(f => esc(f.path)).join(", ") || "None"}</p>
        <p><strong>Request size:</strong> ${x.chars.toLocaleString()} characters</p>
        <p class="small muted">Tokens measure text processed by the model. These are reported usage figures, not a price.</p>
        <p class="small tx-usage">${esc(x.usage || "Token counts were not reported for this request.")}</p>
      </details><button class="btn small" data-action="view-tx" data-id="${esc(x.id)}">View sent request</button></article>`).join("")}`
    : `<p class="muted">Nothing has been sent to Claude yet.</p>`}
    <p class="small muted">Saved requests have secrets removed. This log shows what was sent; it does not confirm whether the work succeeded.</p></div>`;
}

function bindCfgInputs() {
  document.querySelectorAll("#set-body [data-cfg]").forEach(el => {
    const [sec, key, sub] = el.dataset.cfg.split(".");
    const handler = () => {
      let v;
      if (el.type === "checkbox") v = el.checked;
      else if (el.dataset.list) v = el.value.split("\n").map(s => s.trim()).filter(Boolean);
      else if (typeof S.config[sec][key] === "number") v = Number(el.value);
      else v = el.value;
      if (sub) v = { ...S.config[sec][key], [sub]: v };  // One entry of a map setting, e.g. tools.enabled_tools.web_search.
      saveCfg({ [sec]: { [key]: v } });
    };
    el.addEventListener(el.tagName === "TEXTAREA" || el.type === "text" || el.type === "number" ? "change" : "input", handler);
  });
}

async function saveCfg(update) {
  try {
    S.config = (await api("POST", "/api/config", { config: update })).config;
    toast("Saved");
    loadStatus(true);
  } catch (e) { toast(e.message, true); }
}

function applyTheme(t) {
  if (t === "light" || t === "dark") document.documentElement.dataset.theme = t;
  else delete document.documentElement.dataset.theme;
  EnoyAppearance.apply();
  if (typeof syncFrameTheme === "function") syncFrameTheme();  // Window border and background follow the theme.
}

// ---------------------------------------------------------------- commands

async function runCommand(command) {
  if (!command || !S.project) return;
  await guarded(async () => {
    const taskId = S.chat ? S.chat.task.id : null;
    let d = await api("POST", "/api/commands/run", { project_id: S.project.id, command, approved: false, task_id: taskId });
    if (d.needs_approval) {
      const p = d.policy;
      const ok = await confirmBox(p.level === "high_risk" ? "This command needs care" : "Run this command?",
        `<p><code>${esc(command)}</code></p><ul class="small">${p.reasons.map(r => `<li>${esc(r)}</li>`).join("")}</ul>
         <p class="small muted">It runs in your project folder with your normal Windows permissions.</p>`,
        p.level === "high_risk" ? "Run anyway" : "Run", p.level === "high_risk");
      if (!ok) return;
      d = await api("POST", "/api/commands/run", { project_id: S.project.id, command, approved: true, task_id: taskId });
    }
    if (!taskId) toast(`Running ${command}…`);
    if (S.chat) { await loadChat(S.chat.task.id); renderThread(true); }
  });
}

// ---------------------------------------------------------------- actions

async function chatAction(path, body) {
  const id = S.chat.task.id;
  await guarded(async () => {
    await api("POST", `/api/tasks/${id}/${path}`, body);
    await loadChat(id);
    await loadChats();
    renderAll(true);
  });
}

async function pickFolder() {
  closeMenu();
  try {
    const d = await api("POST", "/api/projects/pick");
    if (d.project) { closeModal(); await openProject(d.project.id); }
    else S.sendAfterOpen = false;  // Dialog cancelled: keep the draft, send nothing.
  } catch (e) {
    // No native dialog (e.g. running in a normal browser): ask for the path.
    openModal(`<h2>Open a project folder</h2><label class="field">Folder path<input type="text" id="path-input" placeholder="C:\\Projects\\MyGame"></label>
      <div class="row" style="justify-content:flex-end"><button class="btn ghost" data-action="close-modal">Cancel</button><button class="btn primary" id="path-ok">Open</button></div>`);
    const go = () => guarded(async () => {
      const d = await api("POST", "/api/projects/open", { path: $("path-input").value });
      closeModal();
      await openProject(d.project.id);
    });
    $("path-ok").onclick = go;
    $("path-input").addEventListener("keydown", ev => { if (ev.key === "Enter") go(); });
    $("path-input").focus();
  }
}

async function openProject(id) {
  await useProject(id);
  S.chat = null; S.chatId = null;
  await loadProjects();
  if (typeof setView === "function") setView("chat");
  renderAll();
  if (S.sendAfterOpen) { S.sendAfterOpen = false; await send(); }
}

// On narrow windows the sidebar floats over the chat, so it starts closed and
// closes again once a chat is picked.
const NARROW = 900;
function closeSidebarIfNarrow() { if (window.innerWidth < NARROW) $("app").classList.add("no-sidebar"); }

const actions = {
  "starting-model-menu": el => openMenu(el, [
    { title: "Default", desc: "Use the configured planning model", action: "starting-model-default", checked: !S.startingModel },
    { title: "Lighter model", desc: esc(S.config?.codex?.execution_model || "gpt-6-luna"), action: "starting-model-light" },
    { title: "Custom model…", desc: "Choose a model ID and reasoning effort", action: "starting-model-custom" },
  ]),
  "starting-model-default": () => { S.startingModel = null; closeMenu(); renderComposer(); },
  "starting-model-light": () => {
    S.startingModel = { model: S.config?.codex?.execution_model || "gpt-6-luna", effort: S.config?.codex?.execution_effort || "low" };
    closeMenu(); renderComposer();
  },
  "starting-model-custom": () => {
    closeMenu();
    openModal(`<h2>Starting Codex model</h2><p>Used for the opening Codex request on each message. Later team phases use automatic routing.</p>
      <label>Model ID <input id="start-model-id" maxlength="200" placeholder="Model ID" value="${esc(S.startingModel?.model || S.config?.codex?.model || "")}"></label>
      <label>Reasoning effort <select id="start-model-effort">${["", "low", "medium", "high", "xhigh", "max"].map(e => `<option value="${e}" ${e === (S.startingModel?.effort || "") ? "selected" : ""}>${e || "Model default"}</option>`).join("")}</select></label>
      <div class="row"><button class="btn ghost" data-action="close-modal">Cancel</button><button class="btn primary" data-action="starting-model-save">Use model</button></div>`);
  },
  "starting-model-save": () => {
    const model = $("start-model-id").value.trim();
    if (!model || /\s/.test(model)) { toast("Enter a model ID without spaces.", true); return; }
    S.startingModel = { model, effort: $("start-model-effort").value };
    closeModal(); renderComposer();
  },
  "toggle-sidebar": () => $("app").classList.toggle("no-sidebar"),
  "new-chat": () => { closeSidebarIfNarrow(); S.chat = null; S.chatId = null; S.draft = ""; S.localOnly = false;
    S.team = !!(S.status && S.status.team_default); if (typeof setView === "function") setView("chat"); renderAll(); $("prompt")?.focus(); },
  "team-toggle": () => guarded(async () => {
    if (!S.chat) { S.team = !S.team; renderAll(); return; }
    const on = !S.chat.task.state.team;
    const running = S.chat.task.workflow === "team" && ["running", "awaiting_approval", "awaiting_claude"].includes(S.chat.task.status);
    await api("POST", `/api/tasks/${S.chat.task.id}/team`, { on });
    toast(on ? "TEAM ROLES on for this chat." : running ? "TEAM ROLES off. The team pauses after the current step; finished work is kept." : "TEAM ROLES off for this chat.");
    await loadChat(S.chat.task.id);
    renderAll();
  }),
  "open-chat": el => guarded(async () => { closeSidebarIfNarrow(); await loadChat(Number(el.dataset.id)); renderAll(true); }),
  "suggest": el => { S.draft = el.dataset.text; renderComposer(); $("prompt").focus(); },
  "insert-command": el => {
    S.draft = el.dataset.command + (el.dataset.command === "/compact" ? " " : "");
    closeMenu(); renderComposer(); $("prompt").focus();
  },
  "send": () => send(),
  "stop": () => chatAction("cancel"),
  "project-menu": el => openMenu(el, [
    ...codeProjects().slice(0, 8).map(p => ({ title: `${FOLDER_ICON} ${esc(p.name)}`, desc: p.path, action: "switch-project", data: { id: p.id }, checked: S.project && p.id === S.project.id })),
    ...(codeProjects().length ? ["-"] : []),
    { title: "Open folder…", desc: "Let the AI read and change code in a folder", action: "pick-folder" },
    ...(S.project && !S.project.is_default ? ["-", { title: "🔒 Keep this project on my PC", desc: "Nothing from it is ever sent to Claude", action: "toggle-project-local", checked: !!S.project.local_only }] : []),
  ]),
  "toggle-project-local": () => { closeMenu(); guarded(async () => {
    const d = await api("POST", `/api/projects/${S.project.id}/local-only`, { on: !S.project.local_only });
    S.project.local_only = d.project.local_only;
    toast(S.project.local_only ? "This project now stays on your PC." : "This project may use Claude again for permitted files.");
    renderAll();
  }); },
  "toggle-chat-local": () => { closeMenu(); guarded(async () => {
    if (S.chat) {
      await api("POST", `/api/tasks/${S.chat.task.id}/local-only`, { on: !S.chat.task.state.local_only });
      await loadChat(S.chat.task.id);
    } else S.localOnly = !S.localOnly;
    renderAll();
  }); },
  "switch-project": el => { closeMenu(); guarded(() => openProject(Number(el.dataset.id))); },
  "switch-default": () => { closeMenu(); guarded(async () => { await useDefaultProject(); S.chat = null; S.chatId = null; await loadProjects(); renderAll(); }); },
  "pick-folder": () => pickFolder(),
  "mode": el => guarded(() => switchWorkspace(el.dataset.mode)),
  "colour-mode": el => { S.appearanceMode=el.dataset.mode; settingsAppearance(); },
  "colour-preset": async el => {
    const error=await saveWorkspaceColours(S.appearanceMode||mode(),{preset:el.dataset.preset,custom:{}});
    if (error) { $("colour-error").textContent=error; return; }
    settingsAppearance(); if (typeof syncFrameTheme === "function") syncFrameTheme();
  },
  "colour-reset": async () => {
    const editing=S.appearanceMode||mode();
    const error=await saveWorkspaceColours(editing,{preset:EnoyAppearance.defaultId(editing),custom:{}});
    if (error) { $("colour-error").textContent=error; return; }
    settingsAppearance(); if (typeof syncFrameTheme === "function") syncFrameTheme();
  },
  "open-chatgpt": () => openRegularChatGPT(),
  "integrated-chat": () => guarded(async () => { await setChatExperience("integrated"); toast("Integrated chat uses your Codex plan allowance."); }),
  "add-images": () => { closeMenu(); const i = $("img-input"); if (i) i.click(); },
  "unattach-image": el => { S.images = S.images.filter(im => im.id !== el.dataset.id); renderComposer(); },
  "view-image": (el, e) => { if (e) e.preventDefault();
    openModal(`<img class="img-full" src="${imgUrl(el.dataset.id)}" alt="Attached image">
      <div class="row" style="justify-content:flex-end"><button class="btn primary" data-action="close-modal">Close</button></div>`, true); },
  "model-menu": el => openMenu(el, Object.entries(MODELS).map(([k, m]) => ({
    title: `${k === "auto" ? `<img class="mini-mark" src="/static/logo.svg" alt="">` : avatar(k)} ${m.title}`,
    desc: k === "claude" && !claudeOk() ? "Not available right now. See Settings." : k === "codex" && !codexOk() ? "Unavailable. Run codex login." : m.desc,
    action: "set-model", data: { model: k }, checked: S.model === k, disabled: k === "claude" && !claudeOk(),
  }))),
  "set-model": el => { S.model = el.dataset.model; lsSet("model", S.model); closeMenu(); renderModelPicker(); renderComposer(); },
  "chat-menu": el => {
    const t = S.chat.task;
    openMenu(el, [
      { title: "Activity log", desc: "What happened behind the scenes", action: "activity" },
      { title: "Copy a summary", desc: "To continue this chat in another tool", action: "brief" },
      { title: "Rename", action: "rename-chat", data: { id: t.id } },
      ...(mode() === "chat" ? [{ title: "Move to project…", desc: "Keeps the whole history", action: "move-chat" }] : []),
      { title: "Continue in Claude Code", desc: "Opens Claude Code in the project with this chat's summary", action: "handoff", disabled: !!(S.project && S.project.is_default) },
      ...(["failed", "cancelled", "interrupted"].includes(t.status) ? [{ title: "Try again", action: "resume" }] : []),
      "-",
      { title: `<span class="danger-text">Delete</span>`, action: "delete-chat", data: { id: t.id } },
    ], "left");
  },
  "chat-item-menu": el => openMenu(el, [
    { title: "Rename", action: "rename-chat", data: { id: el.dataset.id } },
    ...(mode() === "chat" ? [{ title: "Move to project…", desc: "Keeps the whole history", action: "move-chat-id", data: { id: el.dataset.id } }] : []),
    "-",
    { title: `<span class="danger-text">Delete</span>`, action: "delete-chat", data: { id: el.dataset.id } },
  ], "right"),
  "move-chat-id": el => guarded(async () => { await loadChat(Number(el.dataset.id)); renderAll(); actions["move-chat"](); }),
  "rename-chat": el => {
    closeMenu();
    const id = Number(el.dataset.id);
    const c = S.chats.find(x => x.id === id) || (S.chat && S.chat.task.id === id ? S.chat.task : null);
    openModal(`<h2>Rename chat</h2><label class="field">Name<input type="text" id="rename-input" maxlength="200" value="${esc(c ? c.title : "")}"></label>
      <div class="row" style="justify-content:flex-end"><button class="btn ghost" data-action="close-modal">Cancel</button><button class="btn primary" id="rename-ok">Save</button></div>`);
    const input = $("rename-input");
    const go = () => {
      if (!input.value.trim()) { toast("Give the chat a name.", true); input.focus(); return; }
      guarded(async () => {
        await api("POST", `/api/tasks/${id}/rename`, { title: input.value });
        closeModal();
        if (S.chatId === id) await loadChat(id);
        await loadChats();
        renderAll();
      });
    };
    $("rename-ok").onclick = go;
    input.addEventListener("keydown", ev => { if (ev.key === "Enter") go(); });
    input.focus(); input.select();
  },
  "delete-chat": el => {
    closeMenu();
    const id = Number(el.dataset.id);
    const c = S.chats.find(x => x.id === id) || (S.chat && S.chat.task.id === id ? S.chat.task : null);
    confirmBox("Delete chat?", `<p><strong>${esc(c ? c.title : "This chat")}</strong> and its messages will be deleted. This cannot be undone.</p>
      <p class="muted small">Files on disk, applied changes and their backups, and saved artifacts stay.</p>`, "Delete", true).then(ok => ok && guarded(async () => {
      await api("POST", `/api/tasks/${id}/delete`, { confirm: true });
      if (S.chatId === id) { S.chat = null; S.chatId = null; S.draft = ""; }
      await loadChats();
      renderAll();
      toast("Chat deleted.");
    }));
  },
  "plus-menu": el => {
    if (!S.project) { openMenu(el, [{ title: "📁 Open a project folder…", action: "pick-folder" }]); return; }
    const general = S.project.is_default;
    const items = [];
    items.push({ title: "Add files from device", desc: "Attach a local file to this chat, or drop it into the composer", action: "add-files" });
    if (!general) {
      items.push({ title: "📄 Add a project file", desc: "Make sure the AI reads it", action: "toggle-files" });
      testCommands().slice(0, 3).forEach(c => items.push({ title: `▶ Run <code>${esc(c)}</code>`, action: "run-cmd", data: { cmd: c } }));
      items.push({ title: "▶ Run a command…", action: "run-custom" });
      items.push({ title: "Summarize this project", desc: "ChatGPT writes an overview it can reuse in later chats", action: "summarize" });
      items.push("-");
    }
    items.unshift({ title: "🖼 Add images", desc: "PNG, JPEG, WebP or GIF. You can also paste or drop them", action: "add-images" });
    const chatLocal = S.chat ? !!S.chat.task.state.local_only : !!S.localOnly;
    items.push({ title: "🔒 Keep this chat on my PC", desc: "Claude is never used in this chat", action: "toggle-chat-local", checked: chatLocal });
    if (general) items.push({ title: "📁 Open a project folder…", desc: "Switches to Code", action: "pick-folder" });
    openMenu(el, items);
  },
  "run-custom": () => {
    closeMenu();
    openModal(`<h2>Run a command</h2><label class="field">Runs in ${esc(S.project.path)}<input type="text" id="cmd-input" placeholder="python -m pytest -q"></label>
      <div class="row" style="justify-content:flex-end"><button class="btn ghost" data-action="close-modal">Cancel</button><button class="btn primary" id="cmd-ok">Run</button></div>`);
    const go = () => { const v = $("cmd-input").value; closeModal(); runCommand(v); };
    $("cmd-ok").onclick = go;
    $("cmd-input").addEventListener("keydown", e => { if (e.key === "Enter") go(); });
    $("cmd-input").focus();
  },
  "summarize": () => { closeMenu(); guarded(async () => { const d = await api("POST", `/api/projects/${S.project.id}/summarize`); await loadChat(d.task.id); await loadChats(); renderAll(true); }); },
  "toggle-files": () => { closeMenu(); S.drawer = !S.drawer; renderDrawer(); },
  "add-files": () => { closeMenu(); $("file-input")?.click(); },
  "preview-file": el => guarded(async () => {
    S.preview = { path: el.dataset.path };
    renderDrawer();
    try { S.preview = await api("GET", `/api/projects/${S.project.id}/file?path=${encodeURIComponent(el.dataset.path)}`); }
    catch (e) { S.preview = { path: el.dataset.path, error: e.message }; }
    renderDrawer();
  }),
  "attach-file": el => { if (!S.attached.includes(el.dataset.path)) S.attached.push(el.dataset.path); renderDrawer(); renderComposer(); },
  "unattach": el => { S.attached = S.attached.filter(p => p !== el.dataset.path); renderComposer(); if (S.drawer) renderDrawer(); },
  "clear-search": (el, e) => { e.preventDefault(); S.searchResults = null; S.search = ""; renderDrawer(); },
  "copy-code": el => { const pre = el.closest(".codeblock").querySelector("pre"); copy(pre.textContent); },
  "copy-msg": el => { const m = S.chat.messages.find(x => x.id === Number(el.dataset.id)); if (m) copy(m.content); },
  "ask-claude": () => chatAction("ask-claude"),
  "folder-no": () => chatAction("folder", { accept: false }),
  "folder-yes": () => guarded(async () => {
    // The chat (or, if it already changed files here, a new chat) now lives in the other folder: follow it there.
    const task = (await api("POST", `/api/tasks/${S.chat.task.id}/folder`, { accept: true })).task;
    await loadProjects();
    await useProject(task.project_id);
    await loadChat(task.id);
    renderAll(true);
  }),
  "claude-yes": () => chatAction("claude/approve"),
  "claude-no": () => chatAction("claude/decline"),
  "claude-preview": () => guarded(async () => {
    const p = (await api("GET", `/api/tasks/${S.chat.task.id}/claude-preview`)).pending;
    openModal(`<h2>Exactly what Claude will see</h2><div class="small muted">Instructions</div><pre class="log">${esc(p.system)}</pre>
      <div class="small muted">Message (${p.prompt.length.toLocaleString()} characters, secrets already removed)</div><pre class="log">${esc(p.prompt)}</pre>
      ${(p.images || []).length ? `<div class="small muted">Images (sent as they are)</div><div class="msg-images left">${p.images.map(id => `<img src="${imgUrl(id)}" alt="Image to send">`).join("")}</div>` : ""}
      <div class="row" style="justify-content:flex-end"><button class="btn primary" data-action="close-modal">Close</button></div>`);
  }),
  "resume": () => { closeMenu(); chatAction("resume"); },
  "apply-cs": el => guarded(async () => {
    const cs = S.chat.changesets.find(c => c.id === Number(el.dataset.id));
    const deletes = cs.files.filter(f => f.action === "delete");
    if (deletes.length && !(await confirmBox("Delete files?", `<p>This deletes ${deletes.map(f => `<code>${esc(f.path)}</code>`).join(", ")}. A backup is kept.</p>`, "Delete and apply", true))) return;
    await api("POST", `/api/changesets/${cs.id}/approve`);
    toast("Applied. A backup of the old version was saved.");
    S.tree = null;
    await loadChat(S.chat.task.id); await loadChats(); renderAll(true);
  }),
  "discard-cs": el => guarded(async () => {
    await api("POST", `/api/changesets/${el.dataset.id}/reject`, { reason: "Discarded in chat" });
    await loadChat(S.chat.task.id); await loadChats(); renderAll();
  }),
  "undo-cs": el => guarded(async () => {
    if (!(await confirmBox("Undo these changes?", "<p>The files go back to how they were before.</p>", "Undo"))) return;
    await api("POST", `/api/changesets/${el.dataset.id}/revert`);
    toast("Undone.");
    S.tree = null;
    await loadChat(S.chat.task.id); renderAll();
  }),
  "run-cmd": el => { closeMenu(); runCommand(el.dataset.cmd); },
  "stop-run": el => guarded(() => api("POST", `/api/commands/${el.dataset.id}/cancel`)),
  "fix-run": el => chatAction("fix", { run_id: Number(el.dataset.id) }),
  "activity": () => {
    closeMenu();
    const d = S.chat;
    openModal(`<h2>Activity</h2><div class="small muted">Routing decisions, which AI worked on what, retries and checks.</div>
      <div>${d.events.map(e => `<div class="ev ${esc(e.type)}"><div class="t">${clock(e.created)}</div><div class="x">${e.backend ? `<strong>${NAME[e.backend] || ""}</strong> ` : ""}${esc(e.text)}</div></div>`).join("")}</div>
      ${d.transmissions.length ? `<h2 style="font-size:15px">Sent to models</h2><table><tr><th>When</th><th>To</th><th>Size</th><th>Files</th><th></th></tr>
        ${d.transmissions.map(x => `<tr><td>${clock(x.created)}</td><td>${NAME[x.backend]}</td><td>${x.chars.toLocaleString()} chars</td><td class="small">${x.files.map(f => esc(f.path)).join(", ") || "–"}</td>
        <td><button class="btn small" data-action="view-tx" data-id="${x.id}">View</button></td></tr>`).join("")}</table>` : ""}
      <div class="row" style="justify-content:flex-end"><button class="btn primary" data-action="close-modal">Close</button></div>`);
  },
  "brief": () => { closeMenu(); guarded(async () => {
    const b = (await api("GET", `/api/tasks/${S.chat.task.id}/brief`)).brief;
    openModal(`<h2>Chat summary</h2><div class="small muted">A self-contained summary for continuing this work elsewhere.</div><pre class="log">${esc(b)}</pre>
      <div class="row" style="justify-content:flex-end"><button class="btn" id="m-copy">Copy</button><button class="btn primary" data-action="close-modal">Close</button></div>`);
    $("m-copy").onclick = () => copy(b);
  }); },
  "handoff": () => { closeMenu(); guarded(async () => {
    const d = (await api("POST", `/api/tasks/${S.chat.task.id}/handoff`)).result;
    openModal(`<h2>${d.opened ? "Opened in Claude Code" : "Claude Code not found"}</h2>
      <p class="small">The chat summary was saved to <code>${esc(d.path)}</code>.</p>
      <p class="small">${d.opened ? "Claude Code opened in a new window with this request:" : "Open a terminal in the project folder, run claude, and paste:"}</p><pre class="log">${esc(d.prompt)}</pre>
      <div class="row" style="justify-content:flex-end"><button class="btn primary" data-action="close-modal">Close</button></div>`);
  }); },
  "view-tx": el => guarded(async () => {
    const x = (await api("GET", `/api/transmissions/${el.dataset.id}`)).transmission;
    openModal(`<h2>Sent to ${esc(NAME[x.backend] || x.backend)}</h2><div class="small muted">${new Date(x.created * 1000).toLocaleString()} · ${x.chars.toLocaleString()} characters · secrets removed</div>
      <pre class="log">${esc(x.content)}</pre><div class="row" style="justify-content:flex-end"><button class="btn primary" data-action="close-modal">Close</button></div>`);
  }),
  "open-settings": (el, e) => { if (e) e.preventDefault(); closeMenu(); guarded(() => openSettings(el && el.dataset.tab)); },
  "provider-status": el => {
    const version = ++providerDialog;
    showProviderStatus(el.dataset.provider);
    guarded(async () => {
      const meta = await api("GET", "/api/config");
      S.config = meta.config;
      if (version === providerDialog && $("provider-dialog")?.dataset.provider === el.dataset.provider && !$("modal").classList.contains("hidden")) {
        showProviderStatus(el.dataset.provider);
        await refreshUsage(el.dataset.provider);
      }
    });
  },
  "provider-check": el => providerAction(el, true),
  "provider-launch": el => providerAction(el),
  "usage-refresh": el => refreshUsage(el.dataset.provider, true),
  "usage-edit": () => editClaudeUsage(),
  "usage-save": () => saveClaudeUsage(),
  "token-savings": () => showTokenSavings(),
  "audit-savings": () => auditTokenSavings(),
  "settings-tab": el => { S.settingsTab = el.dataset.tab; renderSettings(); },
  "check-updates": () => checkUpdates(),
  "install-providers": async el => {
    try { toast((await api("POST", "/api/providers/install", {})).message); launchedProvider = el?.dataset.provider || null; }
    catch (e) { toast(e.message, true); }
  },
  "tools-key": async el => {
    const input = $("brave-key");
    const value = el.dataset.clear ? "" : (input ? input.value.trim() : "");
    if (!el.dataset.clear && !value) { toast("Paste the key first.", true); return; }
    try { await api("POST", "/api/tools/key", { name: el.dataset.name, value }); toast(value ? "Key saved in Windows Credential Manager." : "Key removed."); }
    catch (e) { toast(e.message, true); }
    settingsTools();
  },
  "close-modal": () => closeModal(),
  "refresh-status": () => guarded(async () => { await loadStatus(true); settingsStatus(); }),
  "test-backend": async el => {
    const which = el.dataset.which;
    if (which === "claude" && !(await confirmBox("Test Claude?", "<p>Sends one tiny request (“Reply with READY”) to Claude. It uses a very small amount of your Claude plan.</p>", "Send test"))) return;
    const out = $(`test-${which}`);
    if (out) out.textContent = "Testing…";
    try {
      const d = await api("POST", `/api/${which}/test`);
      if (out) out.innerHTML = d.ok ? `<span style="color:var(--ok)">Works</span> · replied “${esc(d.reply)}” in ${d.seconds} s` : `<span style="color:var(--bad)">Failed:</span> ${esc(d.detail)}`;
    } catch (e) { if (out) out.textContent = e.message; }
    loadStatus(true);
  },
};

function copy(text) {
  navigator.clipboard.writeText(text).then(() => toast("Copied"), () => toast("Copy failed", true));
}

document.addEventListener("click", e => {
  const target = e.target.closest("[data-action]");
  if (window.innerWidth < NARROW && !e.target.closest("#sidebar") && !e.target.closest("#menu") && !e.target.closest("#modal")
      && !(target && target.dataset.action === "toggle-sidebar")) closeSidebarIfNarrow();
  if (!target) {
    if (!e.target.closest("#menu")) closeMenu();
    return;
  }
  if (!target.closest("#menu") && !["starting-model-menu", "model-menu", "project-menu", "chat-menu", "plus-menu", "chat-item-menu"].includes(target.dataset.action)) closeMenu();
  const fn = actions[target.dataset.action];
  if (fn) {
    if (target.tagName === "A") e.preventDefault();
    fn(target, e);
  }
});
document.addEventListener("keydown", e => {
  if (e.key === "Escape") { closeMenu(); closeModal(); }
  if (e.key.toLowerCase() === "n" && e.ctrlKey && e.shiftKey) { e.preventDefault(); actions["new-chat"](); }
});
$("modal").addEventListener("click", e => { if (e.target.id === "modal") closeModal(); });
$("main").addEventListener("dragover", e => { if ([...e.dataTransfer.types].includes("Files")) { e.preventDefault(); $("main").classList.add("dropping"); } });
$("main").addEventListener("dragleave", e => { if (e.target === $("main") || !$("main").contains(e.relatedTarget)) $("main").classList.remove("dropping"); });
$("main").addEventListener("drop", e => {
  $("main").classList.remove("dropping");
  const files = [...e.dataTransfer.files];
  if (!files.length) return;
  e.preventDefault();
  addDroppedFiles(files);
});

async function addDroppedFiles(files) {
  if (!S.project) { toast("Open a project folder first.", true); return; }
  if (regularChat()) { toast("Switch to integrated chat to attach files here.", true); return; }
  const images = files.filter(f => f.type.startsWith("image/"));
  const others = files.filter(f => !f.type.startsWith("image/"));
  if (images.length) await addImages(images);
  if (!others.length) return;
  S.uploading = true;
  try {
    for (const file of others) {
      try {
        const response = await fetch(`/api/projects/${S.project.id}/attachment`, {
          method: "POST", headers: { "X-Enoy-Token": TOKEN, "X-Enoy-Filename": encodeURIComponent(file.name),
            "Content-Type": "application/octet-stream" }, body: file,
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || `Upload failed (${response.status}).`);
        if (!S.attached.includes(result.path)) S.attached.push(result.path);
      } catch (e) { toast(`${file.name}: ${e.message}`, true); }
    }
  } finally { S.uploading = false; renderComposer(); }
  $("prompt")?.focus();
}

// ---------------------------------------------------------------- images

function readDataUrl(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(r.result);
    r.onerror = () => reject(new Error(`Could not read ${file.name || "the image"}.`));
    r.readAsDataURL(file);
  });
}

// Both models read PNG and JPEG. Other formats are converted, and large pictures
// are shrunk to 1568 px on the long side (more detail is not used by the models).
async function prepareImage(file) {
  const src = await readDataUrl(file);
  const img = await new Promise((resolve, reject) => {
    const i = new Image();
    i.onload = () => resolve(i);
    i.onerror = () => reject(new Error(`${file.name || "That file"} is not an image Enoy can read.`));
    i.src = src;
  });
  const scale = Math.min(1, 1568 / Math.max(img.naturalWidth, img.naturalHeight));
  if (scale === 1 && ["image/png", "image/jpeg"].includes(file.type) && file.size <= 3400000) return src;
  const c = document.createElement("canvas");
  c.width = Math.max(1, Math.round(img.naturalWidth * scale));
  c.height = Math.max(1, Math.round(img.naturalHeight * scale));
  const g = c.getContext("2d");
  if (file.type === "image/jpeg") { g.fillStyle = "#fff"; g.fillRect(0, 0, c.width, c.height); }
  g.drawImage(img, 0, 0, c.width, c.height);
  let out = c.toDataURL(file.type === "image/jpeg" ? "image/jpeg" : "image/png", 0.9);
  if (out.length * 0.75 > 3400000) {
    g.globalCompositeOperation = "destination-over"; g.fillStyle = "#fff"; g.fillRect(0, 0, c.width, c.height);
    out = c.toDataURL("image/jpeg", 0.85);
  }
  return out;
}

async function addImages(files) {
  if (!S.project) { toast("Open a folder or switch to Chat first.", true); return; }
  const room = MAX_IMAGES - S.images.length;
  if (room <= 0) { toast(`At most ${MAX_IMAGES} images per message.`, true); return; }
  if (files.length > room) toast(`Only the first ${room} image(s) were added (limit ${MAX_IMAGES} per message).`, true);
  S.uploading = true;
  try {
    for (const f of files.slice(0, room)) {
      await guarded(async () => {
        const data = await prepareImage(f);
        const d = await api("POST", "/api/images", { data });
        if (!S.images.some(im => im.id === d.image.id)) S.images.push({ id: d.image.id, src: data });
      });
    }
  } finally { S.uploading = false; }
  renderComposer();
  const ta = $("prompt"); if (ta) ta.focus();
}

// ---------------------------------------------------------------- live events

let refreshTimer = null;
let refreshRunning = false;
let refreshVersion = 0;
function scheduleRefresh() {
  ++refreshVersion;
  clearTimeout(refreshTimer);
  if (refreshRunning) return;
  refreshTimer = setTimeout(async () => {
    refreshRunning = true;
    const version = refreshVersion;
    const id = S.chatId;
    const loadVersion = chatLoadVersion;
    const done = S.streamDone[id];
    let retry = false;
    try {
      const chat = id ? await api("GET", `/api/tasks/${id}`) : null;
      await loadChats();
      // Events received during either request make this snapshot obsolete.
      // Keep the visible response until a fresh snapshot can replace it.
      if (version !== refreshVersion || id !== S.chatId || loadVersion !== chatLoadVersion) return;
      if (chat) {
        S.chat = chat;
        // message_done is published after saving the answer. Only retire the
        // stream covered by this request, never streams for un-fetched chats.
        if (done && S.streamDone[id] === done) {
          delete S.stream[id]; delete S.thinking[id]; delete S.streamDone[id]; delete S.shown[id];
        }
      }
      renderSidebar();
      renderChatMenu();
      if (S.chat) renderThread();
      const busy = S.chat && ["running", "queued"].includes(S.chat.task.status);
      const hasStop = !!document.querySelector(".round-btn.stop");
      if (busy !== hasStop) renderComposer();
    } catch (e) {
      retry = true;
    } finally {
      refreshRunning = false;
      if (retry) refreshTimer = setTimeout(scheduleRefresh, 1000);
      else if (version !== refreshVersion) scheduleRefresh();
    }
  }, 200);
}

function connectEvents() {
  const es = new EventSource(`/api/events?token=${encodeURIComponent(TOKEN)}`);
  // A reconnect can miss completion events. Fetch saved state without waiting
  // for the user to send another message.
  es.onopen = () => scheduleRefresh();
  es.onmessage = msg => {
    const ev = JSON.parse(msg.data);
    if (ev.type === "agent_status") {
      if (refreshRunning) scheduleRefresh();
      if (ev.task_id === S.chatId && S.chat) {
        if (S.chat.task.state.active_agent?.started !== ev.agent.started) {
          delete S.stream[ev.task_id]; delete S.thinking[ev.task_id]; delete S.streamDone[ev.task_id];
          S.shown[ev.task_id] = 0;
        }
        S.chat.task.state.active_agent = ev.agent;
        renderLive();
      }
      return;
    }
    if (typeof activityEvent === "function") activityEvent(ev);
    if (ev.type === "activity" || ev.type === "dispatch") return;
    if (ev.type === "token" || ev.type === "thinking") {
      if (refreshRunning) scheduleRefresh();
      if (S.streamDone[ev.task_id]) {  // A new answer starts in the same task.
        delete S.streamDone[ev.task_id]; delete S.stream[ev.task_id]; delete S.thinking[ev.task_id];
      }
      const store = ev.type === "token" ? S.stream : S.thinking;
      if (ev.type === "token" && !S.stream[ev.task_id]) S.shown[ev.task_id] = 0;
      store[ev.task_id] = (store[ev.task_id] || "") + ev.text;
      if (ev.task_id === S.chatId) renderLive();
      return;
    }
    if (ev.type === "message_done") S.streamDone[ev.task_id] = {};
    if (ev.type === "command" && !(ev.run && ev.run.task_id === S.chatId)) {
      if (ev.run && ev.run.status !== "running" && !ev.run.task_id) toast(`${ev.run.command}: ${ev.run.status}`, ev.run.status !== "passed");
      return;
    }
    if (ev.type === "event" && ev.task_id !== S.chatId) { return; }
    if (ev.type === "task_deleted" && ev.task_id === S.chatId) { S.chat = null; S.chatId = null; renderAll(); }
    scheduleRefresh();
  };
}

// ---------------------------------------------------------------- start

async function start() {
  if (!MODELS[S.model]) S.model="auto";
  applyTheme(lsGet("theme") || "dark");
  closeSidebarIfNarrow();
  connectEvents();
  renderModelPicker();
  // Provider checks (Codex, Claude Code) can take a moment on a cold start; the
  // chats render without waiting for them, and the status line fills in after.
  const statusReady = loadStatus();
  setInterval(loadStatus, 20000);
  setInterval(() => api("POST", "/api/heartbeat").catch(() => {}), 30000);
  api("POST", "/api/heartbeat").catch(() => {});
  await Promise.allSettled([loadProjects(), api("GET", "/api/config").then(meta => { S.config = meta.config; }),
    api("GET", "/api/appearance").then(data=>{ if(data.theme) { lsSet("theme",data.theme); applyTheme(data.theme); } EnoyAppearance.restore(data.colours); })]);
  const last = Number(lsGet("lastProject"));
  const linked = Number((location.hash.match(/^#chat=(\d+)$/) || [])[1]);
  try {
    if (linked) {
      // Direct link to a chat (#chat=12): open it in its own project.
      await loadChat(linked);
      await useProject(S.chat.task.project_id);
    } else if (last && S.projects.some(p => p.id === last)) await useProject(last);
    else await useDefaultProject();
    await loadProjects();
  } catch (e) { toast(e.message, true); }
  renderAll(true);
  performance.mark("enoy-ready");
  await statusReady;
  if (!S.chat) renderThread();  // Provider status appears once checked.  // First full render done (used to measure startup).
  const settingsLink = (location.hash.match(/^#settings(?:=(\w+))?$/) || []);
  if (settingsLink[0]) guarded(() => openSettings(settingsLink[1]));
}
start();
