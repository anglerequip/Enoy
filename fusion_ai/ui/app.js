"use strict";
// Enoy chat UI. Plain JavaScript, no build step.
// One conversation view; proposed changes, checks and the "ask Claude?"
// question appear as cards inside the conversation.
// All model output is HTML-escaped: this page holds the API token.

const TOKEN = document.querySelector('meta[name="fusion-token"]').content;
const NAME = { qwen: "Qwen", claude: "Claude", team: "Team", enoy: "Enoy" };
const MODELS = {
  auto: { title: "Auto", desc: "The smart router picks per message: Qwen for everyday work, Claude for hard problems. Claude asks you first." },
  qwen: { title: "Qwen 3.5 9B", desc: "Runs on your PC. Free and private. This chat stays local unless you pick Claude." },
  claude: { title: "Claude", desc: "Strongest reasoning. Uses your Claude plan." },
};
const S = {
  status: null, projects: [], project: null, facts: null, chats: [], chatId: null, chat: null,
  model: lsGet("model") || "auto", attached: [], stream: {}, thinking: {}, drawer: false,
  tree: null, search: "", searchResults: null, preview: null, config: null, cfgMeta: null,
  settingsTab: "general", images: [], streamDone: {},
};
const MAX_IMAGES = 5;
function imgUrl(id) { return `/api/images/${id}?token=${encodeURIComponent(TOKEN)}`; }
// Chat mode = plain chats (no folder); Code mode = chats about a project folder.
function mode() { return S.project && (S.project.is_default || S.project.kind === "space") ? "chat" : "code"; }
function codeProjects() { return S.projects.filter(p => !p.is_default && p.kind !== "space"); }

// ---------------------------------------------------------------- helpers

async function api(method, path, body) {
  const opts = { method, headers: { "X-Fusion-Token": TOKEN } };
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
function avatar(b) {
  if (b === "team") return `<span class="avatar team">◆</span>`;
  if (b === "enoy") return `<img class="avatar enoy" src="/static/logo.svg" alt="">`;
  return `<span class="avatar ${b === "claude" ? "claude" : "qwen"}">${b === "claude" ? "✳" : "Q"}</span>`;
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
}
function qwenOk() { return !!(S.status && S.status.local.available); }
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

async function loadChats() {
  if (!S.project) { S.chats = []; return; }
  S.chats = (await api("GET", `/api/tasks?project_id=${S.project.id}&limit=200`)).tasks;
}

async function loadChat(id) {
  S.chatId = id;
  S.chat = await api("GET", `/api/tasks/${id}`);
}

// ---------------------------------------------------------------- sidebar

function renderSidebar() {
  const p = S.project;
  const m = mode();
  document.querySelectorAll("#mode-switch button").forEach(b => {
    const on = b.dataset.mode === m;
    b.classList.toggle("on", on);
    b.setAttribute("aria-selected", on);
  });
  $("project-switch").innerHTML = m === "chat" ? (p && p.kind === "space" ? `<div class="space-chip">
      <button data-action="project-page" title="Open the project page"><span>▣</span><span class="pname">${esc(p.name)}</span></button>
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
  const st = S.status;
  const el = $("status-line");
  if (!st) { el.innerHTML = `<span class="pill"><span class="dot bad"></span>App not responding</span>`; return; }
  const cl = st.claude;
  const claudeText = !cl.available ? "Claude off" : (cl.verified || st.claude_last_verified ? "Claude ready" : "Claude not tested");
  el.innerHTML = `<span class="pill"><span class="dot ${st.local.available ? "ok" : "bad"}"></span>${st.local.available ? "Qwen ready" : "Qwen offline"}
    <span class="dot ${cl.available ? (cl.verified || st.claude_last_verified ? "ok" : "warn") : ""}" style="margin-left:8px"></span>${claudeText}</span>
    <span>⚙ Settings</span>`;
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
    ${S.model === "auto" ? `<img class="mini-mark" src="/static/logo.svg" alt="">` : avatar(S.model)} ${m.title}${CHEV}</button>`;
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
  $("chat-menu").innerHTML = S.chat ? `<button class="title-btn" data-action="chat-menu" title="Chat options">
    <span class="t">${esc(S.chat.task.title)}</span>${CHEV}</button>` : "";
  $("files-btn").classList.toggle("hidden", mode() === "chat" || !S.project);
}

// ---------------------------------------------------------------- thread

function nearBottom() { const s = $("scroll"); return s.scrollHeight - s.scrollTop - s.clientHeight < 140; }
function toBottom() { const s = $("scroll"); s.scrollTop = s.scrollHeight; }

function renderThread(forceBottom = false) {
  const stick = forceBottom || nearBottom();
  const main = $("main");
  if (!S.chat) {
    main.classList.add("empty");
    const hour = new Date().getHours();
    const greet = hour < 5 ? "Working late" : hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
    const general = S.project && S.project.is_default;
    if (!S.project) {
      $("thread").innerHTML = `<div class="hello">
        <h1 class="serif"><img class="hello-mark" src="/static/logo.svg" alt="">Code</h1>
        <p>Open a project folder to let Enoy read and change its code, or just type below and pick the folder when you send. Changes always wait for your OK.</p>
        <div class="suggest"><button data-action="pick-folder">Open folder…</button></div></div>`;
      return;
    }
    const tips = general
      ? ["Explain closures in JavaScript", "Write a Luau function that shuffles a table", "What is a race condition?"]
      : ["Give me an overview of this project", "Find and fix a bug in the main file", "Add tests for the most important function"];
    $("thread").innerHTML = `<div class="hello">
      <h1 class="serif"><img class="hello-mark" src="/static/logo.svg" alt="">${greet}</h1>
      <p>${S.project && S.project.kind === "space" ? `In project <strong>${esc(S.project.name)}</strong>.${S.project.instructions ? " Its instructions apply to this chat." : ""}` : general ? "Ask anything, or attach an image. Switch to Code in the sidebar to work on a project folder." : `Working in <strong>${esc(S.project ? S.project.name : "")}</strong>.`}</p>
      ${!qwenOk() && S.status ? `<p style="margin-top:10px;color:var(--bad)">Qwen is offline. Start Ollama, then try again. <a href="#" data-action="open-settings" data-tab="status">Details</a></p>` : ""}
      <div class="suggest">${tips.map(t => `<button data-action="suggest" data-text="${esc(t)}">${esc(t)}</button>`).join("")}</div></div>`;
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
  }));
  keyed.push({ key: `s${t.id}`, html: statusHtml(t) });
  patchThread($("thread"), keyed);
  if (S.stream[t.id] || S.thinking[t.id]) renderLive(true);
  if (stick) toBottom();
}

// Update the thread in place. An item whose HTML did not change keeps its DOM
// node, so finished answers are not rebuilt (no flicker, text selection and
// images survive) while new items are inserted where they belong.
function patchThread(container, items) {
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
      el = fresh;
    }
    const want = prev ? prev.nextSibling : container.firstChild;
    if (want !== el) container.insertBefore(el, want);
    prev = el;
  }
  old.forEach(el => el.remove());
}

function handoffText(e) {
  const who = e.backend === "claude" ? "Claude" : "Qwen";
  let why = e.text.replace(/^Now handled by [^:]+:\s*/, "").replace(/^Escalated:\s*/, "");
  why = why.replace(/^The local model/, "Qwen");
  return `${who} took over · ${why}`;
}

function messageHtml(m, isLast) {
  if (m.role === "user") {
    const pics = (m.images || []).map(id => `<a href="#" data-action="view-image" data-id="${id}"><img src="${imgUrl(id)}" alt="Attached image" loading="lazy"></a>`).join("");
    return `${pics ? `<div class="msg-images">${pics}</div>` : ""}<div class="msg-user">${esc(m.content)}</div>`;
  }
  const b = m.backend || "qwen";
  return `<div class="msg-ai" data-msg-id="${m.id}">
    <div class="ai-head">${avatar(b)}${NAME[b] || b}<span class="where">${b === "qwen" ? "· on your PC" : ""}</span></div>
    <div class="ai-body">${md(m.content)}</div>
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

function runHtml(r, isLatest) {
  const out = [r.stdout, r.stderr].filter(Boolean).join("\n").trim();
  const state = r.status === "running" ? `<span class="muted">running…</span>`
    : r.status === "passed" ? `<span class="badge-ok">✓ Passed</span>`
    : `<span class="badge-bad">✗ ${r.status === "timeout" ? "Timed out" : r.status === "cancelled" ? "Stopped" : "Failed"}</span>`;
  return `<div class="card">
    <div class="card-title">▶ <code>${esc(r.command)}</code> ${state}</div>
    ${r.status !== "running" ? `<div class="card-sub">Exit code ${r.exit_code ?? "–"} · ${r.duration_s != null ? r.duration_s.toFixed(1) : "?"} s</div>` : ""}
    ${out ? `<details ${r.status !== "passed" && isLatest ? "open" : ""}><summary>Output</summary><pre class="run-out">${esc(out.slice(-8000))}</pre></details>` : ""}
    <div class="row">
      ${r.status === "running" ? `<button class="btn small" data-action="stop-run" data-id="${r.id}">Stop</button>` : ""}
      ${isLatest && ["failed", "timeout", "error"].includes(r.status) && !(S.chat && S.chat.task.workflow === "team") ? `<button class="btn primary small" data-action="fix-run" data-id="${r.id}">Fix it</button>` : ""}
    </div></div>`;
}

function statusHtml(t) {
  const state = t.state || {};
  const run = state.team_run || {};
  if (t.workflow === "team" && ["running", "queued"].includes(t.status)) {
    return `<div class="msg-ai live"><div class="ai-head">${avatar("team")}Team<span class="where">· ${esc(run.status || "working")}</span></div>
      <div class="ai-body" id="live-body"></div>
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
        <button class="btn" data-action="claude-no">Keep the run on Qwen</button></div></div>`;
  }
  if (t.status === "paused") {
    return `<div class="card"><div class="card-title">${avatar("team")} Team run paused</div>
      <div class="card-sub">TEAM ROLES was switched off. Finished work and the plan are kept.</div>
      <div class="row"><button class="btn primary" data-action="team-toggle">Turn TEAM ROLES on and continue</button>
        <button class="btn ghost" data-action="stop">Cancel the run</button></div></div>`;
  }
  if (["running", "queued"].includes(t.status)) {
    const b = t.owner || "qwen";
    return `<div class="msg-ai live">
      <div class="ai-head">${avatar(b)}${NAME[b]}<span class="where">${b === "qwen" ? "· on your PC" : ""}</span></div>
      <details class="thinking hidden" id="live-thinking-wrap"><summary>Thinking</summary><div id="live-thinking"></div></details>
      <div class="ai-body" id="live-body"></div>
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
        <button class="btn" data-action="claude-no">Keep it with Qwen</button>
        <button class="btn ghost small" data-action="claude-preview">See exactly what is sent</button></div></div>`;
  }
  if (t.status === "failed") {
    return `<div class="card bad"><div class="card-title">Something went wrong</div>
      <div class="card-sub">${esc((t.summary || "").replace(/^The local model/, "Qwen"))}</div>
      <div class="row"><button class="btn" data-action="resume">Try again</button>
        ${t.owner !== "claude" && claudeOk() ? `<button class="btn" data-action="ask-claude">Ask Claude</button>` : ""}
        ${!qwenOk() ? `<button class="btn ghost" data-action="open-settings" data-tab="status">Check status</button>` : ""}
        ${/Memory & PC|mode allows/.test(t.summary || "") ? `<button class="btn primary" data-action="open-settings" data-tab="pc">Memory settings</button>` : ""}</div></div>`;
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
// Tokens arrive as separate events; they are applied at most once per frame
// (no fixed timer, so text appears as fast as it arrives). Only the Markdown
// block still being written is redrawn.
let liveFrame = null;
function renderLive(now = false) {
  const paint = () => {
    liveFrame = null;
    const body = $("live-body");
    if (!body || !S.chat || S.view !== "chat") return;
    const stick = nearBottom();
    const id = S.chat.task.id;
    const text = S.stream[id] || "";
    MD.renderInto(body, text, true);
    const wait = $("live-wait");
    if (wait) wait.classList.toggle("hidden", !!text);
    const think = S.thinking[id] || "";
    const wrap = $("live-thinking-wrap");
    if (wrap) {
      wrap.classList.toggle("hidden", !think);
      const th = $("live-thinking");
      if (th.textContent.length !== think.length) th.textContent = think;
    }
    if (stick) toBottom();
  };
  if (now) { if (liveFrame) cancelAnimationFrame(liveFrame); paint(); return; }
  if (!liveFrame) liveFrame = requestAnimationFrame(paint);
}
setInterval(() => {
  const el = $("live-elapsed");
  if (!el || !S.chat) return;
  const s = Math.round(Date.now() / 1000 - S.chat.task.updated);
  el.textContent = s > 3 ? ` · ${s}s` + (s > 25 && S.chat.task.owner !== "claude" && !S.stream[S.chat.task.id] ? " (the first answer after a restart loads the model)" : "") : "";
}, 1000);

// ---------------------------------------------------------------- composer

function renderComposer() {
  const busy = S.chat && ["running", "queued"].includes(S.chat.task.status);
  const general = S.project && S.project.is_default;
  const placeholder = S.chat ? "Reply…" : general ? "Message Enoy…" : S.project ? `Ask about ${S.project.name}, or describe a change…` : "Describe what to build or change. You'll pick the folder next.";
  $("composer-wrap").innerHTML = `
    <div class="composer">
      ${S.images.length ? `<div class="img-chips">${S.images.map(im => `<span class="img-chip"><img src="${im.src}" alt="Attached image">
        <button data-action="unattach-image" data-id="${im.id}" title="Remove image" aria-label="Remove image">✕</button></span>`).join("")}</div>` : ""}
      ${S.attached.length ? `<div class="chips">${S.attached.map(p => `<span class="chip">📄 ${esc(p)}<button data-action="unattach" data-path="${esc(p)}" title="Remove">✕</button></span>`).join("")}</div>` : ""}
      <textarea id="prompt" rows="1" placeholder="${esc(placeholder)}"></textarea>
      <input type="file" id="img-input" accept="image/png,image/jpeg,image/webp,image/gif,image/bmp" multiple hidden>
      <div class="bar">
        <button class="round-btn plus" data-action="plus-menu" title="Add images or files, run tests…" aria-label="Add"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg></button>
        ${mode() === "code" && !S.chat ? `<button class="comp-chip" data-action="project-menu" title="${esc(S.project ? S.project.path : "Choose the folder Enoy works in")}">${FOLDER_ICON}<span class="t">${S.project ? esc(S.project.name) : "Choose folder"}</span>${CHEV}</button>` : ""}
        <div id="team-toggle"></div>
        <div class="grow"></div>
        <div id="model-picker"></div>
        ${busy ? `<button class="round-btn stop" data-action="stop" title="Stop" aria-label="Stop"><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="7" y="7" width="10" height="10" rx="2" fill="currentColor"/></svg></button>`
               : `<button class="round-btn send" id="send-btn" data-action="send" title="Send (Enter)" aria-label="Send" disabled><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 19V5M5 12l7-7 7 7"/></svg></button>`}
      </div>
    </div>
    <div class="composer-note">${teamOn() && !(S.project && S.project.is_default) ? "Team roles on: code changes are planned, checked and reviewed. " : ""}${composerNote()}</div>`;
  const ta = $("prompt");
  const draft = S.draft || "";
  ta.value = draft;
  const resize = () => { ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight, 260) + "px"; const b = $("send-btn"); if (b) b.disabled = !ta.value.trim() && !S.images.length; };
  ta.addEventListener("input", () => { S.draft = ta.value; resize(); });
  ta.addEventListener("paste", e => {
    const files = [...(e.clipboardData ? e.clipboardData.files : [])].filter(f => f.type.startsWith("image/"));
    if (files.length) { e.preventDefault(); addImages(files); }
  });
  $("img-input").addEventListener("change", e => { addImages([...e.target.files]); e.target.value = ""; });
  ta.addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); } });
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
function composerNote() {
  const lo = localOnlyReason();
  if (lo) return `🔒 Local only: ${esc(lo)}. Nothing here is sent to Claude.`;
  if (S.model === "claude") return "Claude uses your Claude plan.";
  return "Qwen runs on your PC. Claude only gets the relevant files (secrets removed) when needed, and asks first.";
}

async function send(textOverride) {
  const ta = $("prompt");
  let text = (textOverride ?? (ta ? ta.value : "")).trim();
  if (S.uploading) { toast("Wait a moment: an image is still being added."); return; }
  const images = textOverride === undefined ? S.images.map(im => im.id) : [];
  if (!text && images.length) text = images.length > 1 ? "What is in these images?" : "What is in this image?";
  if (!text || S.sending) return;
  if (!S.project) {
    // Code mode with no folder: ask for one, then send this message into it.
    S.draft = text; S.sendAfterOpen = true;
    await pickFolder();
    return;
  }
  if (S.chat && ["running", "queued"].includes(S.chat.task.status)) return;
  const backend = S.model === "auto" ? null : S.model;
  if (!S.status) await loadStatus();  // Sent before the first status check finished.
  if (backend === "claude" && !claudeOk()) { toast("Claude is not available right now. Pick Auto or Qwen.", true); return; }
  if (backend === "claude" && localOnlyReason()) { toast(`Claude is off here: ${localOnlyReason()}. Change that first.`, true); return; }
  if (!qwenOk() && backend !== "claude") { toast("Qwen is offline. Start Ollama (it is in the Start menu), then try again.", true); return; }
  const files = [...S.attached];
  S.sending = true;
  const ok = await guarded(async () => {
    if (S.chat) {
      await api("POST", `/api/tasks/${S.chat.task.id}/message`, { text, backend, files, images });
      await loadChat(S.chat.task.id);
    } else {
      const d = await api("POST", "/api/tasks", { project_id: S.project.id, objective: text, backend, files, images, local_only: !!S.localOnly, team: !!S.team });
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
  const tabs = [["general", "General"], ["models", "Models"], ["pc", "Memory & PC"], ["safety", "Safety"], ["status", "Status & help"], ["activity", "Activity"]];
  openModal(`<div class="set-nav"><h2>Settings</h2>${tabs.map(([k, l]) =>
    `<button class="${S.settingsTab === k ? "active" : ""}" data-action="settings-tab" data-tab="${k}">${l}</button>`).join("")}
    <div class="grow"></div><button data-action="close-modal">Close</button></div>
    <div class="set-body" id="set-body"><div class="muted">Loading…</div></div>`, true);
  ({ general: settingsGeneral, models: settingsModels, pc: settingsMemoryPc, safety: settingsSafety, status: settingsStatus, activity: settingsActivity })[S.settingsTab]();
}

function toggleRow(sec, key, label, desc) {
  return `<div class="set-row"><div class="lbl"><span>${esc(label)}</span>${desc ? `<span class="d">${esc(desc)}</span>` : ""}</div>
    <input type="checkbox" data-cfg="${sec}.${key}" ${S.config[sec][key] ? "checked" : ""}></div>`;
}
function inputRow(sec, key, label, desc, type = "text") {
  return `<label class="field">${esc(label)}<input type="${type}" step="any" data-cfg="${sec}.${key}" value="${esc(S.config[sec][key])}">${desc ? `<span class="small">${esc(desc)}</span>` : ""}</label>`;
}

function settingsGeneral() {
  const theme = lsGet("theme") || "system";
  const p = S.project;
  $("set-body").innerHTML = `
    <div class="set-section"><h3>Appearance</h3>
      <div class="set-row"><div class="lbl"><span>Theme</span></div>
        <select id="theme-select">${["system", "light", "dark"].map(t => `<option value="${t}" ${t === theme ? "selected" : ""}>${t[0].toUpperCase() + t.slice(1)}</option>`).join("")}</select></div></div>
    <div class="set-section"><h3>Project</h3>
      <div class="set-row"><div class="lbl"><span>${p && !p.is_default ? esc(p.name) : "No project open"}</span><span class="d">${p && !p.is_default ? esc(p.path) : "Plain chat. Open a folder to let the AI read and change code."}</span></div>
        <button class="btn" data-action="pick-folder">Open folder…</button></div></div>
    <div class="set-section"><h3>About</h3>
      <div class="small muted">Enoy ${esc(S.status ? S.status.version : "")}. Qwen runs locally through Ollama; Claude is reached through your Claude Code sign-in.
      Every change to your files waits for your approval.</div></div>`;
  $("theme-select").addEventListener("change", e => { applyTheme(e.target.value); lsSet("theme", e.target.value); });
}

const PRESETS = {
  recommended: { mode: "auto", workflow: "escalate", title: "Smart routing", desc: "Recommended. Enoy picks per task from what you ask: Qwen for everyday work, Claude straight away for hard problems, and Claude when Qwen's attempt fails. Claude still asks you first." },
  qwen_first: { mode: "local_first", workflow: "escalate", title: "Qwen first, Claude only after failures", desc: "Qwen always tries first, even on hard tasks. Claude only steps in when Qwen's attempt fails." },
  local: { mode: "local_first", workflow: "local_only", title: "Only Qwen", desc: "Everything stays on your PC. You can still pick Claude yourself in the model menu." },
  review: { mode: "local_first", workflow: "local_then_review", title: "Qwen writes, Claude double-checks", desc: "Claude reviews every code change Qwen proposes. Uses more of your Claude plan." },
  claude: { mode: "claude_first", workflow: "escalate", title: "Claude first", desc: "Claude answers by default; Qwen is used when Claude is unavailable." },
};

async function settingsModels() {
  const c = S.config;
  const preset = Object.entries(PRESETS).find(([, p]) => p.mode === c.routing.mode && p.workflow === c.routing.workflow);
  let models = [];
  try { models = (await api("GET", "/api/ollama/models")).models.map(m => m.name); } catch (e) { /* offline */ }
  $("set-body").innerHTML = `
    <div class="set-section"><h3>Who answers</h3>
      ${Object.entries(PRESETS).map(([k, p]) => `<label class="radio-card ${preset && preset[0] === k ? "sel" : ""}">
        <input type="radio" name="preset" value="${k}" ${preset && preset[0] === k ? "checked" : ""}>
        <span><div>${esc(p.title)}</div><div class="small muted">${esc(p.desc)}</div></span></label>`).join("")}
      ${preset ? "" : `<div class="small muted">Custom setup (see Advanced below).</div>`}
    </div>
    ${c.routing.mode === "auto" ? smartRoutingHtml(c) : ""}
    <div class="set-section"><h3>${avatar("team")} TEAM ROLES</h3>
      <div class="small muted">A chat with TEAM ROLES on hands code changes to a small team: an orchestrator (plain code) plans and tracks the work, an architect is added for larger features, developers write the code, QA runs your project's real checks, a reviewer checks the diff, and a debugger steps in after repeated failures. Each part is routed to Qwen or Claude by the smart router. Questions are always answered directly. Switch it per chat with the TEAM ROLES button at the top.</div>
      ${toggleRow("team", "default_new_chats", "Turn TEAM ROLES on for new chats", "Each chat keeps its own setting; this only sets the starting point.")}
      <div class="grid2">
        ${inputRow("team", "max_tasks", "Max developer tasks per plan", "", "number")}
        ${inputRow("team", "max_parallel", "Team tasks at the same time", "Only tasks on different models with different files run together.", "number")}
        ${inputRow("team", "max_repair_rounds", "Repair rounds before stopping", "", "number")}
        ${inputRow("team", "max_model_calls", "Model requests per run", "The run stops and reports when this is used up.", "number")}
        ${inputRow("team", "max_claude_calls", "Claude requests per run", "", "number")}
        ${inputRow("team", "review_min_lines", "Model review from … added lines", "Smaller changes get the automatic checks only, unless they are high risk.", "number")}
      </div></div>
    <div class="set-section"><h3>${avatar("qwen")} Qwen (on your PC)</h3>
      <label class="field">Model<select data-cfg="ollama.model">${[...new Set([c.ollama.model, ...models])].map(m => `<option ${m === c.ollama.model ? "selected" : ""}>${esc(m)}</option>`).join("")}</select>
        <span class="small">${models.length ? "Models installed in Ollama. Enoy never downloads models." : "Ollama is not reachable right now."}</span></label>
      ${toggleRow("ollama", "think", "Think before answering", "Better on hard problems, much slower.")}
      <div class="row"><button class="btn" data-action="test-backend" data-which="ollama">Test Qwen</button><span id="test-ollama" class="small"></span></div></div>
    <div class="set-section"><h3>${avatar("claude")} Claude</h3>
      ${toggleRow("claude", "enabled", "Use Claude", "Off = everything stays on your PC.")}
      ${toggleRow("claude", "confirm_before_send", "Ask me before sending anything to Claude", "Shows which files and how much text will be sent.")}
      ${toggleRow("claude", "safe_mode", "Lean requests", "Skips your personal Claude Code setup (CLAUDE.md, plugins, MCP) so each request uses far fewer tokens.")}
      <div class="row"><button class="btn" data-action="test-backend" data-which="claude">Test Claude</button><span id="test-claude" class="small"></span></div></div>
    <details class="set-section"><summary class="muted" style="cursor:pointer">Advanced</summary>
      <div class="grid2" style="margin-top:10px">
        <label class="field">Routing mode<select data-cfg="routing.mode">${S.cfgMeta.routing_modes.map(m => `<option ${m === c.routing.mode ? "selected" : ""}>${m}</option>`).join("")}</select></label>
        <label class="field">Workflow<select data-cfg="routing.workflow">${S.cfgMeta.workflows.map(m => `<option ${m === c.routing.workflow ? "selected" : ""}>${m}</option>`).join("")}</select></label>
        ${inputRow("ollama", "num_ctx", "Qwen context size (tokens)", "Smaller fits better in GPU memory.", "number")}
        ${inputRow("ollama", "num_predict", "Max answer length (tokens)", "", "number")}
        ${inputRow("ollama", "temperature", "Temperature", "", "number")}
        ${inputRow("ollama", "keep_alive", "Keep Qwen loaded for", "e.g. 15m or 1h")}
        ${inputRow("ollama", "endpoint", "Ollama address", "")}
        ${inputRow("ollama", "request_timeout_s", "Qwen timeout (s)", "", "number")}
        ${inputRow("claude", "model", "Claude model", "Default claude-opus-5-5 (Opus 5.5). Empty = Claude Code's default")}
        <label class="field">Claude effort<select data-cfg="claude.effort">${["", "low", "medium", "high", "xhigh", "max"].map(v => `<option value="${v}" ${v === c.claude.effort ? "selected" : ""}>${v || "Claude Code default"}</option>`).join("")}</select>
          <span class="small">Higher effort thinks longer and uses more of your Claude plan.</span></label>
        ${inputRow("claude", "timeout_s", "Claude timeout (s)", "", "number")}
        ${inputRow("claude", "executable", "Claude Code path", "Empty = find automatically")}
        ${inputRow("routing", "max_local_attempts", "Qwen tries before asking Claude", "", "number")}
        ${inputRow("routing", "max_test_fix_attempts", "Qwen test-fix tries before asking Claude", "", "number")}
        ${inputRow("routing", "max_escalations", "Max Claude hand-offs per chat", "", "number")}
        ${inputRow("routing", "max_review_rounds", "Max Claude review rounds", "", "number")}
        ${inputRow("context", "max_files", "Files sent per request", "", "number")}
        ${inputRow("context", "max_chars", "Characters of file content per request", "", "number")}
      </div>
      <div style="margin-top:10px">${toggleRow("routing", "escalate_high_complexity", "Send hard-looking tasks straight to Claude", "Still asks first.")}</div>
    </details>`;
  if (c.routing.mode === "auto") loadRoutingStats();
  document.querySelectorAll('input[name="preset"]').forEach(r => r.addEventListener("change", async e => {
    const p = PRESETS[e.target.value];
    await saveCfg({ routing: { mode: p.mode, workflow: p.workflow } });
    settingsModels();
  }));
  bindCfgInputs();
}

const AGGRESSIVENESS = { prefer_qwen: "Prefer Qwen", balanced: "Balanced", prefer_claude: "Prefer Claude" };

function smartRoutingHtml(c) {
  const r = c.routing;
  const sel = (key, opts) => `<select data-cfg="routing.${key}">${Object.entries(opts).map(([k, v]) => `<option value="${k}" ${k === r[key] ? "selected" : ""}>${esc(v)}</option>`).join("")}</select>`;
  return `<div class="set-section"><h3>Smart routing</h3>
    <div class="small muted">Every new chat is classified by what it asks (an explanation, a small function, a cross-module bug…), how far the change reaches, and how costly a mistake would be. Clear routine work goes to Qwen; work that clearly benefits from stronger reasoning goes straight to Claude. No model is asked to make this choice.</div>
    <div class="grid2">
      <label class="field">How readily to use Claude${sel("aggressiveness", AGGRESSIVENESS)}<span class="small">Balanced: borderline tasks stay with Qwen unless a mistake would be costly or hard to spot.</span></label>
      ${inputRow("routing", "target_local_share", "Target share for Qwen (%)", "A goal to compare against, never a quota. Enoy never calls Claude, or avoids it, to hit this number.", "number")}
      <label class="field">If Claude is needed but unavailable${sel("claude_fallback", { local: "Continue with Qwen and say so", stop: "Stop and tell me" })}</label>
      ${inputRow("routing", "max_handoffs", "Max model switches per chat", "Stops Qwen and Claude handing a task back and forth.", "number")}
      ${inputRow("routing", "stats_window", "Statistics cover the last … finished tasks", "", "number")}
    </div>
    ${toggleRow("routing", "failure_escalation", "Hand failed work to Claude", "When Qwen's attempts fail Enoy's checks, the same error repeats, or tests keep failing.")}
    ${toggleRow("routing", "manual_override", "Let me pick the model per chat", "The model menu at the top. Your pick always wins, within privacy rules.")}
    ${toggleRow("routing", "log_decisions", "Write routing decisions to the app log", "They always appear in Activity.")}
    ${toggleRow("routing", "zero_paid_mode", "Zero-paid mode", "Claude is never used automatically, only when you pick it yourself. Enoy never uses the paid Anthropic API; Claude runs through your Claude Code plan, which has usage limits.")}
    <div id="routing-stats" class="small muted">Loading model share…</div></div>`;
}

async function loadRoutingStats() {
  let s;
  try { s = await api("GET", "/api/routing/stats"); } catch (e) { return; }
  const el = $("routing-stats");
  if (!el) return;
  el.innerHTML = s.counted ? `<b>Last ${s.counted} finished task(s):</b> Qwen ${s.qwen} (${s.qwen_pct}%), Claude ${s.claude} (${100 - s.qwen_pct}%). Target: Qwen ${s.target_qwen_pct}%.<br>${esc(s.note)}<br><span class="muted">${esc(s.rule)}</span>`
    : `No finished tasks yet. ${esc(s.rule)}`;
}

function settingsSafety() {
  const c = S.config;
  $("set-body").innerHTML = `
    <div class="set-section"><h3>Changes to your files</h3>
      ${toggleRow("permissions", "auto_apply_patches", "Apply changes without asking", "Off is safer. Deleting a file always asks.")}
      <div class="small muted">Before a change is applied, Enoy checks that the files have not changed since the AI read them, keeps a backup of each, and lets you undo it.</div></div>
    <div class="set-section"><h3>Commands</h3>
      <label class="field">Commands that run without asking (one per line)
        <textarea class="field mono" rows="7" data-cfg="permissions.command_allowlist" data-list="1">${esc(c.permissions.command_allowlist.join("\n"))}</textarea></label>
      ${inputRow("permissions", "command_timeout_s", "Stop a command after (seconds)", "", "number")}
      <div class="small muted">Commands run in the project folder without a shell. Installing, deleting, network tools and anything not listed above ask first; system and admin tools are blocked.
        This is not a sandbox: an approved command runs with your normal Windows permissions.</div></div>
    <div class="set-section"><h3>Private files</h3>
      <label class="field">Extra paths the AI must never see (one pattern per line, e.g. assets/**)
        <textarea class="field mono" rows="4" data-cfg="permissions.extra_excluded_globs" data-list="1">${esc(c.permissions.extra_excluded_globs.join("\n"))}</textarea></label>
      <div class="small muted">Always hidden: .env files, keys, certificates, password and cookie files. Secrets in other files are replaced with [REDACTED] before any model sees them.</div></div>
    <div class="set-section"><h3>What may go to Claude</h3>
      <label class="field">Cloud privacy<select data-cfg="privacy.cloud_policy">
        <option value="auto" ${c.privacy.cloud_policy === "auto" ? "selected" : ""}>Send the relevant, permitted files when Claude is needed</option>
        <option value="never" ${c.privacy.cloud_policy === "never" ? "selected" : ""}>Never send code to Claude (everything stays on this PC)</option></select></label>
      <label class="field">Files Qwen may read but Claude never receives (one pattern per line, e.g. src/secret/** or *.private.lua)
        <textarea class="field mono" rows="4" data-cfg="privacy.cloud_excluded_globs" data-list="1">${esc(c.privacy.cloud_excluded_globs.join("\n"))}</textarea></label>
      <div class="small muted">Claude only gets the few files relevant to the task, never the whole project. Before each request Enoy removes private files, any lines quoting them (from Qwen's answers, diffs or test output), and secrets, and logs a privacy check in Activity.
        You can also keep a whole project or a single chat on this PC from the project menu or the ＋ menu.</div></div>`;
  bindCfgInputs();
}

async function settingsStatus() {
  $("set-body").innerHTML = `<div class="muted">Checking…</div>`;
  let d;
  try { d = await api("GET", "/api/diagnostics"); } catch (e) { $("set-body").innerHTML = esc(e.message); return; }
  const loaded = d.local.extra && d.local.extra.loaded;
  const gpu = d.gpus.map(g => `${esc(g.name)} · ${g.vram_used_mb} / ${g.vram_total_mb} MB in use`).join("<br>") || "No NVIDIA GPU found";
  $("set-body").innerHTML = `
    ${d.advice.length ? `<div class="card accent">${d.advice.map(a => `<div class="small">${esc(a)}</div>`).join("")}</div>` : ""}
    ${d.config_warnings.length ? `<div class="card bad">${d.config_warnings.map(a => `<div class="small">${esc(a)}</div>`).join("")}</div>` : ""}
    <div class="set-section"><div class="row" style="justify-content:space-between"><h3>AI status</h3><button class="btn small" data-action="refresh-status">Refresh</button></div>
      <div class="kv"><div>Qwen</div><div><span class="dot ${d.local.available ? "ok" : "bad"}"></span> ${esc(d.local.detail)}</div>
      ${loaded ? `<div>Speed</div><div>${loaded.gpu_percent}% of Qwen is on the graphics card (${loaded.vram_gb} of ${loaded.size_gb} GB). ${loaded.gpu_percent < 100 ? "The rest runs on the CPU, which is slower." : ""}</div>` : ""}
      <div>Claude</div><div><span class="dot ${d.claude && d.claude.available ? "ok" : "bad"}"></span> ${esc(d.claude ? d.claude.detail : "")}</div></div></div>
    <div class="set-section"><h3>This PC</h3><div class="kv">
      <div>Windows</div><div>${esc(d.os)}</div><div>Memory</div><div>${d.memory.total_gb ?? "?"} GB (${d.memory.free_gb ?? "?"} GB free)</div>
      <div>Graphics</div><div>${gpu}</div><div>Python</div><div>${esc(d.python)}</div></div></div>
    <div class="set-section"><h3>Where your data is</h3><div class="kv">${Object.entries(d.paths).map(([k, v]) => `<div>${esc(k.replace(/_/g, " "))}</div><div class="mono small" style="word-break:break-all">${esc(v)}</div>`).join("")}</div></div>
    <details class="set-section"><summary class="muted" style="cursor:pointer">Recent log</summary><pre class="log">${esc(d.log_tail || "(empty)")}</pre></details>`;
}

async function settingsActivity() {
  let tx = [];
  try { tx = (await api("GET", "/api/transmissions?backend=claude")).transmissions; } catch (e) { /* ignore */ }
  $("set-body").innerHTML = `<div class="set-section"><h3>What was sent to Claude</h3>
    <div class="small muted">Every Claude request, stored with secrets removed. Token counts are shown only when Claude Code reports them.</div>
    ${tx.length ? `<table><tr><th>When</th><th>Chat</th><th>Size</th><th>Files</th><th>Reported tokens</th><th></th></tr>
    ${tx.map(x => `<tr><td>${new Date(x.created * 1000).toLocaleString()}</td><td>#${x.task_id}</td><td>${x.chars.toLocaleString()} chars</td>
      <td>${x.files.map(f => esc(f.path)).join(", ") || "–"}</td><td class="small">${esc(x.usage || "not reported")}</td>
      <td><button class="btn small" data-action="view-tx" data-id="${x.id}">View</button></td></tr>`).join("")}</table>`
    : `<div class="muted">Nothing has been sent to Claude yet.</div>`}</div>`;
}

function bindCfgInputs() {
  document.querySelectorAll("#set-body [data-cfg]").forEach(el => {
    const [sec, key] = el.dataset.cfg.split(".");
    const handler = () => {
      let v;
      if (el.type === "checkbox") v = el.checked;
      else if (el.dataset.list) v = el.value.split("\n").map(s => s.trim()).filter(Boolean);
      else if (typeof S.config[sec][key] === "number") v = Number(el.value);
      else v = el.value;
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
  renderAll();
  if (S.sendAfterOpen) { S.sendAfterOpen = false; await send(); }
}

// On narrow windows the sidebar floats over the chat, so it starts closed and
// closes again once a chat is picked.
const NARROW = 900;
function closeSidebarIfNarrow() { if (window.innerWidth < NARROW) $("app").classList.add("no-sidebar"); }

const actions = {
  "toggle-sidebar": () => $("app").classList.toggle("no-sidebar"),
  "new-chat": () => { closeSidebarIfNarrow(); S.chat = null; S.chatId = null; S.draft = ""; S.localOnly = false;
    S.team = !!(S.status && S.status.team_default); renderAll(); $("prompt").focus(); },
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
  "send": () => send(),
  "stop": () => chatAction("cancel"),
  "project-menu": el => openMenu(el, [
    ...codeProjects().slice(0, 8).map(p => ({ title: `📁 ${esc(p.name)}`, desc: p.path, action: "switch-project", data: { id: p.id }, checked: S.project && p.id === S.project.id })),
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
  "mode": el => guarded(async () => {
    const want = el.dataset.mode;
    if (want === mode() && (want === "chat" || S.project)) return;
    S.chat = null; S.chatId = null; S.draft = "";
    if (want === "chat") await useDefaultProject();
    else {
      const folders = codeProjects();
      const last = Number(lsGet("lastCodeProject"));
      const pick = folders.find(p => p.id === last) || folders[0];
      if (pick) await useProject(pick.id);
      else { S.project = null; S.chats = []; renderSidebar(); }
    }
    renderAll();
  }),
  "add-images": () => { closeMenu(); const i = $("img-input"); if (i) i.click(); },
  "unattach-image": el => { S.images = S.images.filter(im => im.id !== el.dataset.id); renderComposer(); },
  "view-image": (el, e) => { if (e) e.preventDefault();
    openModal(`<img class="img-full" src="${imgUrl(el.dataset.id)}" alt="Attached image">
      <div class="row" style="justify-content:flex-end"><button class="btn primary" data-action="close-modal">Close</button></div>`, true); },
  "model-menu": el => openMenu(el, Object.entries(MODELS).map(([k, m]) => ({
    title: `${k === "auto" ? `<img class="mini-mark" src="/static/logo.svg" alt="">` : avatar(k)} ${m.title}`,
    desc: k === "claude" && !claudeOk() ? "Not available right now. See Settings." : k === "qwen" && !qwenOk() ? "Offline. Start Ollama." : m.desc,
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
    if (!general) {
      items.push({ title: "📄 Add a project file", desc: "Make sure the AI reads it", action: "toggle-files" });
      testCommands().slice(0, 3).forEach(c => items.push({ title: `▶ Run <code>${esc(c)}</code>`, action: "run-cmd", data: { cmd: c } }));
      items.push({ title: "▶ Run a command…", action: "run-custom" });
      items.push({ title: "Summarize this project", desc: "Qwen writes an overview it reuses in later chats", action: "summarize" });
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
  "settings-tab": el => { S.settingsTab = el.dataset.tab; renderSettings(); },
  "close-modal": () => closeModal(),
  "refresh-status": () => guarded(async () => { await loadStatus(true); settingsStatus(); }),
  "test-backend": async el => {
    const which = el.dataset.which;
    if (which === "claude" && !(await confirmBox("Test Claude?", "<p>Sends one tiny request (“Reply with READY”) to Claude. It uses a very small amount of your Claude plan.</p>", "Send test"))) return;
    const out = $(`test-${which}`);
    if (out) out.textContent = which === "ollama" ? "Testing… (up to a minute if Qwen has to load)" : "Testing…";
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
  if (!target.closest("#menu") && !["model-menu", "project-menu", "chat-menu", "plus-menu", "chat-item-menu"].includes(target.dataset.action)) closeMenu();
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
  const files = [...e.dataTransfer.files].filter(f => f.type.startsWith("image/"));
  if (!files.length) return;
  e.preventDefault();
  addImages(files);
});

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
function scheduleRefresh() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(async () => {
    try {
      if (S.chatId) await loadChat(S.chatId);
      await loadChats();
    } catch (e) { return; }
    for (const id of Object.keys(S.streamDone)) {
      delete S.stream[id]; delete S.thinking[id]; delete S.streamDone[id];
    }
    renderSidebar();
    renderChatMenu();
    if (S.chat) { renderThread(); }
    const busy = S.chat && ["running", "queued"].includes(S.chat.task.status);
    const hasStop = !!document.querySelector(".round-btn.stop");
    if (busy !== hasStop) renderComposer();
  }, 200);
}

function connectEvents() {
  const es = new EventSource(`/api/events?token=${encodeURIComponent(TOKEN)}`);
  es.onmessage = msg => {
    const ev = JSON.parse(msg.data);
    if (typeof activityEvent === "function") activityEvent(ev);
    if (ev.type === "activity" || ev.type === "dispatch") return;
    if (ev.type === "token" || ev.type === "thinking") {
      if (S.streamDone[ev.task_id]) {  // A new answer starts in the same task.
        delete S.streamDone[ev.task_id]; delete S.stream[ev.task_id]; delete S.thinking[ev.task_id];
      }
      const store = ev.type === "token" ? S.stream : S.thinking;
      store[ev.task_id] = (store[ev.task_id] || "") + ev.text;
      if (ev.task_id === S.chatId) renderLive();
      return;
    }
    if (ev.type === "message_done") S.streamDone[ev.task_id] = true;
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
  applyTheme(lsGet("theme") || "system");
  closeSidebarIfNarrow();
  connectEvents();
  renderModelPicker();
  // Provider checks (Ollama, Claude Code) can take a moment on a cold start; the
  // chats render without waiting for them, and the status line fills in after.
  const statusReady = loadStatus();
  setInterval(loadStatus, 20000);
  setInterval(() => api("POST", "/api/heartbeat").catch(() => {}), 30000);
  api("POST", "/api/heartbeat").catch(() => {});
  try { await loadProjects(); } catch (e) { /* server problem shown in status */ }
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
  if (!S.chat) renderThread();  // The welcome screen mentions Qwen being offline.  // First full render done (used to measure startup).
  const settingsLink = (location.hash.match(/^#settings(?:=(\w+))?$/) || []);
  if (settingsLink[0]) guarded(() => openSettings(settingsLink[1]));
}
start();
