"use strict";
// Activity panel: what each model is doing, why, and what happens next.
// Every value comes from /api/activity, which reads stored task state. Opening
// or refreshing this panel never sends a model request.
// Loaded after app.js and uses its helpers (S, api, esc, actions, ...).

const AC = {
  open: false, data: null, focus: null, stage: null, expanded: new Set(),
  agent: lsGet("acAgent") || "all", type: lsGet("acType") || "all", scope: lsGet("acScope") || "task",
  timer: null, loading: false, chunks: {}, openDetails: new Set(),
};
const AGENT = { codex: "Codex", qwen: "Qwen (previous provider)", claude: "Claude", system: "Enoy", user: "You" };
const STATE = {
  offline: ["Offline", "muted"], available: ["Available", "ok"], working: ["Working", "live"],
  waiting: ["Waiting", "warn"], blocked: ["Blocked", "bad"], failed: ["Failed", "bad"], completed: ["Completed", "ok"],
};
const STAGE_ICON = {
  completed: ["✓", "Completed"], active: ["●", "Active"], waiting: ["⏸", "Waiting"], queued: ["○", "Not started"],
  optional: ["○", "Optional"], skipped: ["–", "Skipped"], failed: ["✕", "Failed"], blocked: ["!", "Blocked"],
  cancelled: ["✕", "Cancelled"], unrecorded: ["?", "Not recorded"],
};
const STEP_STATE = {
  working: "Working", waiting: "Waiting", queued: "Queued", blocked: "Blocked", completed: "Completed",
  failed: "Failed", cancelled: "Cancelled", skipped: "Skipped", interrupted: "Interrupted",
};
const TYPES = { all: "All events", routing: "Routing", model: "Model work", approval: "Approvals", tests: "Checks & files", errors: "Errors", status: "Status" };

function ago(ts) {
  if (!ts) return "";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return new Date(ts * 1000).toLocaleDateString();
}
function dur(a, b) {
  if (!a) return "";
  const s = Math.max(0, (b || Date.now() / 1000) - a);
  return s < 60 ? `${Math.round(s)} s` : `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`;
}
function who(a) { return `<span class="who ${esc(a)}">${esc(AGENT[a] || a || "Enoy")}</span>`; }
function stateBadge(state) {
  const [label, cls] = STATE[state] || [state, "muted"];
  return `<span class="state ${cls}">${label}</span>`;
}
function taskBadge(t) {
  const cls = { running: "live", queued: "muted", awaiting_approval: "warn", awaiting_claude: "warn", completed: "ok",
    failed: "bad", cancelled: "muted", interrupted: "bad" }[t.status] || "muted";
  return `<span class="state ${cls}">${esc(t.status_label)}</span>`;
}

// ---------------------------------------------------------------- data

async function loadActivity(refresh = false) {
  if (AC.loading) return;
  AC.loading = true;
  const focus = AC.focus || S.chatId;
  const qs = new URLSearchParams();
  if (focus) qs.set("focus", focus);
  if (AC.scope === "task") qs.set("task", focus || "focus");
  if (refresh) qs.set("refresh", "1");
  try {
    AC.data = await api("GET", `/api/activity?${qs}`);
    AC.error = null;
  } catch (e) {
    AC.error = e.message;
  } finally {
    AC.loading = false;
  }
  renderActivity();
}

let acTimer = null;
function scheduleActivity() {
  if (!AC.open) return;
  clearTimeout(acTimer);
  acTimer = setTimeout(() => loadActivity(), 350);
}

// Called by app.js for every live event.
function activityEvent(ev) {
  if (ev.type === "token" && ev.backend === "codex") {
    AC.chunks[ev.task_id] = (AC.chunks[ev.task_id] || 0) + 1;
    const el = document.getElementById("qwen-chunks");
    if (el && Number(el.dataset.task) === ev.task_id) el.textContent = `${(Number(el.dataset.base) || 0) + AC.chunks[ev.task_id]} pieces of text received`;
    return;
  }
  if (["activity", "task", "changeset", "command", "dispatch", "message_done"].includes(ev.type)) scheduleActivity();
}

// ---------------------------------------------------------------- render

function renderActivity() {
  const panel = $("activity");
  panel.classList.toggle("hidden", !AC.open);
  $("activity-btn").classList.toggle("on", AC.open);
  if (!AC.open) return;
  const d = AC.data;
  if (!d) {
    panel.innerHTML = head(null) + `<div class="ac-body"><div class="muted small ac-pad">${AC.error ? `Could not load activity: ${esc(AC.error)}` : "Loading…"}</div></div>`;
    return;
  }
  AC.chunks = {};
  const scroll = panel.querySelector(".ac-body") ? panel.querySelector(".ac-body").scrollTop : 0;
  // Re-rendering replaces the HTML; keep keyboard focus where it was.
  const a = document.activeElement && panel.contains(document.activeElement) ? document.activeElement : null;
  const focusKey = a ? (a.id ? `#${a.id}` : a.dataset.action ? `[data-action="${a.dataset.action}"]` +
    ["id", "key", "cs"].filter(k => a.dataset[k]).map(k => `[data-${k}="${a.dataset[k]}"]`).join("") : null) : null;
  panel.innerHTML = head(d) + `<div class="ac-body">
    ${AC.error ? `<div class="ac-banner bad">Could not refresh: ${esc(AC.error)}. Showing the last loaded state.</div>` : ""}
    ${activitySummaryHtml(d)}
    <details class="ac-more" data-key="activity-more" ${AC.openDetails.has("activity-more") ? "open" : ""}><summary>More · models, workflow & technical details</summary>
    ${overviewHtml(d)}
    <section class="ac-sec"><h3>Models</h3>${leadLine(d)}<div class="ac-agents">${d.agents.map(agentHtml).join("")}</div></section>
    ${d.focus && d.focus.team ? teamHtml(d.focus.team) : ""}
    ${d.focus ? workflowHtml(d.focus) : ""}
    ${routingHtml(d.routing)}
    ${tasksHtml(d)}
    ${timelineHtml(d)}
    </details>
  </div>`;
  panel.querySelector(".ac-body").scrollTop = scroll;
  if (focusKey) { const el = panel.querySelector(focusKey); if (el) el.focus({ preventScroll: true }); }
  const n = d.counts.needs_you;
  $("activity-btn").dataset.count = n > 0 ? n : "";
}

function activitySummaryHtml(d) {
  const f = d.focus, t = f && f.task, c = d.counts;
  const status = { running: "Your request is being worked on", queued: "Your request is waiting to start",
    awaiting_approval: "Your approval is needed", awaiting_claude: "Waiting to send work to Claude",
    completed: "Your request is complete", failed: "Something went wrong", cancelled: "This request was cancelled",
    interrupted: "Work stopped before finishing" };
  return `<section class="ac-sec ac-summary">
    <h3>At a glance</h3>
    <p class="ac-summary-status">${t ? esc(status[t.status] || t.status_label) : "No request selected"}</p>
    ${t ? `<div class="ac-summary-title">${esc(t.title)}</div>
      <div class="ac-summary-item"><b>Now</b><p>${esc(f.stage || t.status_label)}${t.owner ? ` · ${esc(AGENT[t.owner] || t.owner)}` : ""}</p></div>
      <div class="ac-summary-item"><b>Latest update</b><p>${f.last_activity ? esc(f.last_activity.text) : "No update recorded yet."}</p>${f.last_activity ? `<span class="small muted">${ago(f.last_activity.created)}</span>` : ""}</div>
      <div class="ac-summary-item"><b>Next</b><p>${esc(t.next || (t.status === "completed" ? "Read the result in your chat." : "Waiting for the next update."))}</p></div>
      ${controlsHtml(t, false)}` : `<p class="muted">Send a message to see its progress here.</p>`}
    <p class="small muted">${Number(c.active)} working · ${Number(c.queued)} waiting · ${Number(c.completed)} completed</p>
    ${c.needs_you ? `<p class="ac-note warn">${Number(c.needs_you)} request(s) need your attention. Open More to find and review them.</p>` : ""}
    </section>`;
}

function head(d) {
  const exec = d ? { working: ["Working", "live"], idle: ["Idle", "muted"], paused: ["New tasks paused", "warn"] }[d.execution] : null;
  return `<div class="ac-head">
    <div class="ac-title">Activity ${exec ? `<span class="state ${exec[1]}">${exec[0]}</span>` : ""}</div>
    ${d ? `<button class="btn small" data-action="ac-pause" title="${d.paused ? "Start queued tasks and let new ones start" : "New tasks wait in a queue. Work already running continues; use Cancel to stop a task."}">
      ${d.paused ? "▶ Resume new tasks" : "⏸ Pause new tasks"}</button>` : ""}
    <button class="icon-btn" data-action="toggle-activity" title="Close (Esc)" aria-label="Close activity panel">✕</button></div>`;
}

function overviewHtml(d) {
  const c = d.counts;
  const counts = `<div class="ac-counts">
    ${[["Active", c.active, "live"], ["Queued", c.queued, ""], ["Needs you", c.needs_you, c.needs_you ? "warn" : ""],
      ["Failed or stopped", c.stopped, c.stopped ? "bad" : ""], ["Completed", c.completed, ""]]
      .map(([l, n, cls]) => `<div class="ac-count ${n ? cls : ""}"><b>${n}</b><span>${l}</span></div>`).join("")}</div>`;
  if (!d.focus) {
    return `<section class="ac-sec">${counts}<div class="ac-empty">No tasks yet. Send a message and its progress shows up here.</div></section>`;
  }
  const f = d.focus, t = f.task;
  const last = f.last_activity;
  return `<section class="ac-sec">
    ${counts}
    <div class="ac-focus">
      <div class="ac-label">${AC.focus && AC.focus !== S.chatId ? "Selected task" : t.id === S.chatId ? "This chat" : "Latest task"}</div>
      <div class="ac-obj"><span class="ac-tt" title="${esc(f.objective)}">${esc(t.title)}</span> ${taskBadge(t)}</div>
      <div class="kv ac-kv">
        <div>Stage</div><div>${esc(f.stage || "–")}</div>
        <div>Handled by</div><div>${t.workflow === "team" ? `${who("system")} Team (see below)` : t.owner ? who(t.owner) : `<span class="muted">Not assigned yet</span>`}${t.initial_backend && t.owner && t.initial_backend !== t.owner ? ` <span class="muted small">(started with ${esc(AGENT[t.initial_backend])})</span>` : ""}</div>
        <div>Why</div><div>${esc(t.routing_reason)}</div>
        ${t.initial_backend && t.owner && t.initial_backend !== t.owner && t.assign_reason ? `<div>Why ${esc(AGENT[t.owner])} now</div><div>${esc(t.assign_reason)}</div>` : ""}
        ${routeRows(f.route)}
        <div>Privacy</div><div>${t.local_only ? `🔒 Local only: ${esc(t.local_only)}` : "Claude may receive the relevant files, with secrets and private files removed"}</div>
        ${f.handoffs.length ? `<div>Handoffs</div><div>${f.handoffs.map(h => `${esc(AGENT[h.from])} → ${esc(AGENT[h.to])}: ${esc(h.reason)} <span class="muted">${clock(h.at)}</span>`).join("<br>")}</div>` : ""}
        ${verificationRow(t.verification)}
        ${t.escalation_reason ? `<div>Escalated</div><div>${esc(t.escalation_reason)}</div>` : ""}
        <div>Next</div><div>${esc(t.next || "Not known yet.")}</div>
        <div>Last activity</div><div>${last ? `${esc(last.text)} <span class="muted">· ${ago(last.created)}</span>` : "–"}</div>
        ${t.test_results.length ? `<div>Checks</div><div>${t.test_results.map(r => `<code>${esc(r.command)}</code> ${r.status === "passed" ? `<span class="badge-ok">passed</span>` : `<span class="badge-bad">${esc(r.status)}</span>`}`).join("<br>")}</div>` : ""}
      </div>
      ${controlsHtml(t, true)}
    </div></section>`;
}

// Who leads, and the memory mode, from Enoy's own state (no model is asked to draw this).
function leadLine(d) {
  const bits = [];
  if (d.lead && d.lead.coordinator) bits.push(`${who("codex")} <b>leads</b>: evaluates requests, delegates to Codex or Claude, and reviews shared results.`);
  else if (d.lead && d.lead.enabled) bits.push(`${who("claude")} <b>leads</b>: reads each new message first, then answers, asks, or plans work for ChatGPT and the team.`);
  else bits.push(`<span class="muted">Claude does not lead (Settings → Claude leads). Messages go to the model the router picks.</span>`);
  const m = d.memory;
  if (m) bits.push(`<span class="muted">Memory:</span> ${esc(m.mode)} mode, limit ${m.ceiling_mib.toLocaleString()} MiB`
    + (m.used_mib ? ` · ChatGPT uses ${Math.round(m.used_mib).toLocaleString()} MiB` : " · ChatGPT not loaded")
    + (m.state && !["idle", "within"].includes(m.state) ? ` · <b>${esc(m.state.replace(/_/g, " "))}</b>` : ""));
  return `<div class="small ac-lead">${bits.map(b => `<div>${b}</div>`).join("")}</div>`;
}

const ROLE = { lead: "Claude (lead)", orchestrator: "Orchestrator", architect: "Architect", developer: "Developer", qa: "QA", reviewer: "Reviewer", debugger: "Debugger", user: "You" };
const TEAM_STATE = { planned: "Planned", ready: "Ready", running: "Running", waiting: "Waiting", awaiting_review: "Awaiting review",
  paused: "Paused", completed: "Completed", failed: "Failed", blocked: "Blocked", cancelled: "Cancelled" };

function teamHtml(t) {
  const prov = p => p === "qwen" ? who("qwen") : p === "claude" ? who("claude") : p === "codex" ? who("codex") : who("system");
  const board = t.tasks.map(x => {
    const open = AC.expanded.has("tt" + x.id);
    return `<div class="ac-tt-row ${esc(x.status)}">
      <button class="ac-ev-row ac-tt-main" data-action="ac-tt" data-id="${x.id}" aria-expanded="${open}">
        <span class="t">${esc(x.key)}</span><span class="who">${esc(ROLE[x.role] || x.role)}${x.specialty ? " · " + esc(x.specialty) : ""}</span>
        <span class="x">${esc(x.title)}</span><span class="state ${x.status === "completed" ? "ok" : x.status === "running" ? "live" : ["failed", "blocked"].includes(x.status) ? "bad" : x.status === "awaiting_review" || x.status === "paused" ? "warn" : "muted"}">${esc(TEAM_STATE[x.status] || x.status)}</span></button>
      ${open ? `<div class="kv ac-ev-more small">
        <div>Model</div><div>${prov(x.provider)}</div>
        <div>Why this model</div><div>${esc(x.route_reason)}</div>
        ${x.activation ? `<div>Why this role</div><div>${esc(x.activation)}</div>` : ""}
        ${x.after.length ? `<div>Waits for</div><div>${x.after.map(esc).join(", ")}</div>` : ""}
        ${x.spec.why ? `<div>Why this task</div><div>${esc(x.spec.why)}</div>` : ""}
        ${x.spec.expected_output ? `<div>Expected</div><div>${esc(x.spec.expected_output)}</div>` : ""}
        ${x.verification ? `<div>Verified</div><div>${esc(x.verification)}</div>` : ""}
        ${x.files.length ? `<div>Owns files</div><div>${x.files.map(esc).join(", ")}</div>` : ""}
        ${x.changed.length ? `<div>Changed</div><div>${x.changed.map(esc).join(", ")}</div>` : ""}
        ${x.commands.length ? `<div>Checks</div><div>${x.commands.map(c => `<code>${esc(c.command)}</code> ${c.status === "passed" ? `<span class="badge-ok">passed</span>` : `<span class="badge-bad">${esc(c.status)}</span>`}`).join("<br>")}</div>` : ""}
        ${x.findings.length ? `<div>Findings</div><div>${x.findings.map(f => `${esc(f.severity)} ${esc(f.file || "")}${f.line ? ":" + f.line : ""} ${esc(f.problem)}`).join("<br>")}</div>` : ""}
        ${x.error ? `<div>Problem</div><div class="bad-text">${esc(x.error)}</div>` : ""}</div>
        ${ttControls(x)}` : ""}</div>`;
  }).join("");
  const msgs = t.messages.slice(0, 12).map(m => `<div class="small ac-msg"><span class="muted">${clock(m.at)}</span>
    <b>${esc(ROLE[m.from] || m.from)}</b> → ${esc(ROLE[m.to] || m.to)} <span class="muted">(${esc(m.type.replace(/_/g, " "))})</span>: ${esc(m.body)}</div>`).join("");
  const verif = { passed: "checks passed", failed: "checks FAILED", not_run: "no test command available" }[t.verification] || "not verified yet";
  const hold = t.status && !["completed", "failed", "cancelled"].includes(t.status)
    ? `<button class="btn small" data-action="ac-hold" data-id="${AC.data.focus.task.id}" data-hold="${t.hold ? "" : "1"}"
        title="${t.hold ? "Hand out the remaining tasks again" : "Finish the current step, then start nothing new. Finished work is kept."}">${t.hold ? "▶ Resume delegation" : "⏸ Pause delegation"}</button>` : "";
  return `<section class="ac-sec"><h3>Team ${hold}</h3>
    ${t.lead ? `<div class="small">${who(t.lead_provider || "claude")} ${esc(t.lead_state)}</div>` : ""}
    ${t.git && t.git.user_changes && t.git.user_changes.length ? `<div class="small muted">Git: ${t.git.user_changes.length} file(s) had uncommitted changes of yours; changes to them wait for your review.</div>` : ""}
    <div class="small">Run: <b>${esc((t.status || "").replace(/_/g, " "))}</b> · ${esc(verif)} · ${t.model_calls} model request(s), ${t.claude_calls} to Claude${t.repairs ? ` · ${t.repairs} repair round(s)` : ""}</div>
    <div class="small muted">Roles used: ${t.roles_used.map(r => esc(ROLE[r] || r)).join(", ") || "none yet"}${t.running_now.length > 1 ? ` · running at the same time: ${t.running_now.map(esc).join(", ")}` : ""}
      · plan by ${esc(t.planned_by || "…")}${t.auto_apply ? " · changes apply automatically (Settings)" : " · changes wait for your approval"}</div>
    ${t.plan_note ? `<div class="small muted">${esc(t.plan_note)}</div>` : ""}
    ${t.acceptance.length ? `<details class="small"><summary class="muted">Acceptance criteria (${t.acceptance.length})</summary>${t.acceptance.map(a => `<div>• ${esc(a)}</div>`).join("")}</details>` : ""}
    <div class="ac-board">${board}</div>
    ${msgs ? `<details class="small" data-key="team-msgs" ${AC.openDetails.has("team-msgs") ? "open" : ""}><summary class="muted">Messages between roles</summary>${msgs}</details>` : ""}</section>`;
}

function ttControls(x) {
  const b = [];
  if (x.can.retry) b.push(`<button class="btn small" data-action="ac-tt-do" data-do="retry" data-id="${x.id}">Retry</button>`);
  if (x.can.cancel) b.push(`<button class="btn small" data-action="ac-tt-do" data-do="cancel" data-id="${x.id}">Cancel task</button>`);
  if (x.can.model) {
    const cooperative = AC.data?.focus?.task?.workflow === "chatgpt_leads";
    const other = x.provider === "claude" ? "codex" : "claude";
    b.push(`<button class="btn small ghost" data-action="ac-tt-do" data-do="model" data-provider="${other}" data-id="${x.id}">Use ${AGENT[other]} instead</button>`);
  }
  return b.length ? `<div class="row ac-tt-ctl">${b.join("")}</div>` : "";
}

function routeRows(r) {
  return `<div>Sharing</div><div>${esc((r && r.note) || "ChatGPT and Claude share recorded task results.")}</div>`;
}

function verificationRow(v) {
  if (!v) return "";
  if (v.status === "not_run") return `<div>Verified</div><div class="muted">Changes applied; no check run since.</div>`;
  const ok = v.status === "passed";
  return `<div>Verified</div><div>${ok ? `<span class="badge-ok">✓ passed</span>` : `<span class="badge-bad">✕ ${esc(v.status)}</span>`} <code>${esc(v.command)}</code>${!ok && v.detail ? `<div class="small muted">${esc(v.detail)}</div>` : ""}</div>`;
}
function routingHtml(r) {
  if (!r) return "";
  return `<section class="ac-sec"><h3>Provider usage</h3>${Object.entries(r.providers || {}).map(([p,n])=>`<div>${esc(AGENT[p] || p)}: ${Number(n).toLocaleString()} output tokens</div>`).join("")}<p class="small muted">${esc(r.note || "")}</p></section>`;
}

function controlsHtml(t, full) {
  const b = [];
  if (t.id !== S.chatId) b.push(`<button class="btn small" data-action="ac-open-chat" data-id="${t.id}" data-project="${t.project_id}">Open chat</button>`);
  if (t.can.approve_claude) {
    b.push(`<button class="btn small accent" data-action="ac-claude-approve" data-id="${t.id}">Send to Claude…</button>`);
    b.push(`<button class="btn small" data-action="ac-claude-decline" data-id="${t.id}">Keep with ChatGPT</button>`);
  }
  if (t.can.review) b.push(`<button class="btn small accent" data-action="ac-review" data-id="${t.id}">Review changes</button>`);
  if (t.can.retry) b.push(`<button class="btn small" data-action="ac-retry" data-id="${t.id}">${t.status === "interrupted" ? "Resume" : "Retry"}</button>`);
  b.push(`<button class="btn small ${t.can.cancel ? "danger" : ""}" data-action="ac-cancel" data-id="${t.id}" ${t.can.cancel ? "" : "disabled title=\"Only queued, running or waiting tasks can be cancelled\""}>Cancel</button>`);
  if (full) {
    b.push(`<button class="btn small ghost" data-action="ac-log" data-id="${t.id}">View log</button>`);
    if (t.relevant_files.length) b.push(`<button class="btn small ghost" data-action="ac-files" data-id="${t.id}">Files (${t.relevant_files.length})</button>`);
    const qwen = AC.data && AC.data.agents.find(a => a.name === "codex");
    const qwenFree = qwen && qwen.state !== "offline" && qwen.state !== "blocked" && qwen.state !== "working";
    b.push(`<button class="btn small ghost" data-action="ac-explain" data-id="${t.id}" ${qwenFree ? "" : "disabled"}
      title="${qwenFree ? "Sends one short request to ChatGPT via Codex. Claude is not used." : "ChatGPT is busy or not available"}">Explain workflow</button>`);
  }
  return `<div class="row ac-ctl">${b.join("")}</div>`;
}

function agentHtml(a) {
  const conn = a.connection || {};
  const cur = a.current;
  const lines = [];
  if (cur) {
    const chunks = a.name === "codex" && cur.chunks !== undefined && cur.status === "working"
      ? `<div class="small muted" id="qwen-chunks" data-task="${cur.task_id}" data-base="${cur.chunks}">${cur.chunks ? `${cur.chunks} pieces of text received` : "Request sent; waiting for the first words (the provider is responding)"}</div>` : "";
    lines.push(`<div class="ac-cur">
      <div><b>${esc(cur.title)}</b> <span class="muted small">· ${esc(STEP_STATE[cur.status] || cur.status)}${cur.since ? ` since ${clock(cur.since)}` : ""}</span></div>
      ${cur.task_id ? `<div class="small">Task: <a href="#" data-action="ac-focus" data-id="${cur.task_id}">${esc(cur.task_title || `#${cur.task_id}`)}</a>${cur.stage_label ? ` · ${esc(cur.stage_label)}` : ""}</div>` : ""}
      <div class="small"><span class="muted">Why:</span> ${esc(cur.reason)}</div>${chunks}</div>`);
  } else {
    lines.push(`<div class="small muted">No current task.</div>`);
  }
  if (a.waiting_for) lines.push(`<div class="small"><span class="muted">Waiting for:</span> ${esc(a.waiting_for)}</div>`);
  if (a.name === "claude" && a.pending && a.pending.length) {
    lines.push(a.pending.map(p => `<div class="small ac-note warn">Prepared, not sent: ${esc(p.purpose)} request for
      <a href="#" data-action="ac-focus" data-id="${p.task_id}">${esc(p.task_title)}</a>, ${p.chars.toLocaleString()} characters, ${p.files.length} file(s).</div>`).join(""));
  }
  if (a.last_sent) {
    const x = a.last_sent;
    lines.push(`<div class="small"><span class="muted">Last sent:</span> ${esc(x.purpose)} request, ${x.chars.toLocaleString()} characters${x.files.length ? `, ${x.files.length} file(s): ${x.files.slice(0, 4).map(esc).join(", ")}${x.files.length > 4 ? "…" : ""}` : ""}
      · ${ago(x.at)}${""}${x.usage ? `<div class="muted">${esc(x.usage)}</div>` : ""}</div>`);
  }
  if (a.error) {
    lines.push(`<div class="small ac-note bad"><b>${esc(a.error.title)}</b> failed ${ago(a.error.at)}: ${esc(a.error.error)}
      ${a.error.handed_on ? `<div class="muted">The task continued with another step.</div>` : ""}</div>`);
  }
  if (a.recent.length) {
    lines.push(`<details class="ac-recent" data-key="recent-${a.name}" ${AC.openDetails.has(`recent-${a.name}`) ? "open" : ""}><summary>Recent completed work (${a.recent.length})</summary>${a.recent.map(r =>
      `<div class="small">✓ ${esc(r.title)} · <a href="#" data-action="ac-focus" data-id="${r.task_id}">${esc(r.task_title)}</a> <span class="muted">${ago(r.at)}</span>${r.result ? `<div class="muted">${esc(r.result)}</div>` : ""}</div>`).join("")}</details>`);
  }
  const model = a.name === "codex" ? (conn.model || "") : (conn.model || "");
  return `<div class="ac-agent ${esc(a.state)}">
    <div class="ac-agent-head">${avatar(a.name)}<div class="grow"><div><b>${esc(a.label)}</b> <span class="muted small">${esc(model)}</span></div>
      <div class="small muted ac-conn" title="${esc(conn.detail || "")}">${esc(conn.detail || "")}</div></div>${stateBadge(a.state)}</div>
    ${lines.join("")}</div>`;
}

function workflowHtml(f) {
  const stages = f.stages;
  const chips = stages.map((st, i) => {
    const [icon, label] = STAGE_ICON[st.status] || ["○", st.status];
    return `${i ? `<span class="ac-arrow" aria-hidden="true">→</span>` : ""}<button class="ac-stage ${esc(st.status)} ${AC.stage === st.key ? "sel" : ""}"
      aria-label="${esc(st.label)}: ${esc(label)}"
      data-action="ac-stage" data-key="${esc(st.key)}" title="${esc(label)}${st.note ? ": " + esc(st.note) : ""}" aria-pressed="${AC.stage === st.key}">
      <span class="i">${icon}</span>${esc(st.label)}</button>`;
  }).join("");
  const sel = stages.find(s => s.key === AC.stage);
  let detail = "";
  if (sel) {
    const steps = f.steps.filter(s => sel.steps.includes(s.id));
    const [, label] = STAGE_ICON[sel.status] || ["", sel.status];
    detail = `<div class="ac-stage-detail"><div><b>${esc(sel.label)}</b> · ${esc(label)}${sel.note && !steps.length ? ` · <span class="muted">${esc(sel.note)}</span>` : ""}</div>
      ${steps.map(s => `<div class="ac-step ${esc(s.status)}">
        <div>${who(s.agent)} <b>${esc(s.title)}</b> <span class="muted small">· ${esc(STEP_STATE[s.status] || s.status)}${s.started ? ` · ${clock(s.started)}` : ""}${s.finished && s.started ? ` · took ${dur(s.started, s.finished)}` : ""}</span></div>
        <div class="small"><span class="muted">Why:</span> ${esc(s.reason)}</div>
        ${s.result ? `<div class="small"><span class="muted">Result:</span> ${esc(s.result)}</div>` : ""}
        ${s.error ? `<div class="small bad-text"><span class="muted">Problem:</span> ${esc(s.error)}</div>` : ""}
        ${s.after.length ? `<div class="small muted">After: ${s.after.map(esc).join(", ")}</div>` : ""}
        ${s.needs_approval ? `<div class="small muted">Needed your approval.</div>` : ""}
        ${s.files.length ? `<div class="small muted">Files: ${s.files.slice(0, 6).map(esc).join(", ")}${s.files.length > 6 ? "…" : ""}</div>` : ""}
      </div>`).join("") || ""}</div>`;
  }
  return `<section class="ac-sec"><h3 class="ac-h-row">Workflow <span class="ac-h-task" title="${esc(f.task.title)}">${esc(f.task.title)}</span></h3>
    <div class="ac-flow" role="list">${chips}</div>${detail || `<div class="small muted">Select a stage to see who did what and why.</div>`}</section>`;
}

function tasksHtml(d) {
  const rows = d.tasks.filter(t => !d.focus || t.id !== d.focus.task.id);
  if (!rows.length) return "";
  return `<section class="ac-sec"><h3>Other tasks</h3>${rows.map(t => `<div class="ac-task">
      <button class="ac-task-main" data-action="ac-focus" data-id="${t.id}" title="Show this task's workflow">
        <span class="ac-tt">${esc(t.title)}</span> ${taskBadge(t)}
        <div class="small muted">${t.owner ? esc(AGENT[t.owner]) + " · " : ""}${esc(t.stage)}${t.next ? " · " + esc(t.next) : ""}</div></button>
      ${t.can.cancel || t.can.retry || t.can.review || t.can.approve_claude ? controlsHtml(t, false) : ""}</div>`).join("")}</section>`;
}

function timelineHtml(d) {
  const evs = d.events.filter(e => (AC.agent === "all" || e.agent === AC.agent) && (AC.type === "all" || e.category === AC.type));
  const opt = (obj, cur) => Object.entries(obj).map(([k, v]) => `<option value="${k}" ${k === cur ? "selected" : ""}>${esc(v)}</option>`).join("");
  return `<section class="ac-sec"><h3>Timeline</h3>
    <div class="row ac-filters">
      <select id="ac-scope" aria-label="Which tasks">${opt({ task: "This task", all: "All tasks" }, AC.scope)}</select>
      <select id="ac-agent" aria-label="Model or component">${opt({ all: "Everyone", codex: "Codex", claude: "Claude", system: "Enoy", user: "You" }, AC.agent)}</select>
      <select id="ac-type" aria-label="Event type">${opt(TYPES, AC.type)}</select>
    </div>
    <div class="ac-timeline">${evs.length ? evs.map(e => {
      const open = AC.expanded.has(e.id);
      const bad = e.category === "errors";
      return `<div class="ac-ev ${bad ? "bad" : ""}">
        <button class="ac-ev-row" data-action="ac-ev" data-id="${e.id}" aria-expanded="${open}">
          <span class="t">${clock(e.created)}</span>${who(e.agent)}<span class="x">${esc(e.text)}</span></button>
        ${open ? `<div class="kv ac-ev-more small">
          <div>Task</div><div>${e.task_id ? `${esc(e.task_title || "")} (#${e.task_id})` : "Whole app"}</div>
          ${e.stage ? `<div>Stage</div><div>${esc(e.stage)}</div>` : ""}
          <div>Why</div><div>${esc(e.reason || "Reason not recorded.")}</div>
          ${e.outcome ? `<div>Outcome</div><div>${esc(e.outcome)}</div>` : ""}
          <div>Event</div><div><code>${esc(e.kind)}</code> · ${new Date(e.created * 1000).toLocaleString()}</div></div>` : ""}</div>`;
    }).join("") : `<div class="small muted">No events match these filters.</div>`}</div></section>`;
}

// ---------------------------------------------------------------- actions

function setOpen(open) {
  AC.open = open;
  if (open) {
    if (S.drawer) { S.drawer = false; renderDrawer(); }
    loadActivity(true);
    clearInterval(AC.timer);
    // Backend status and "x min ago" labels; the server caches status checks.
    AC.timer = setInterval(() => loadActivity(), 30000);
  } else {
    clearInterval(AC.timer);
  }
  lsSet("activityOpen", open ? "1" : "");
  renderActivity();
}

async function taskPost(id, action, okMsg) {
  await guarded(async () => {
    await api("POST", `/api/tasks/${id}/${action}`);
    if (okMsg) toast(okMsg);
    await loadActivity();
    scheduleRefresh();
  });
}

const baseToggleFiles = actions["toggle-files"];
const baseOpenChat = actions["open-chat"];
const baseNewChat = actions["new-chat"];
Object.assign(actions, {
  // The panel follows the chat you open, unless you picked another task in it.
  "open-chat": el => { AC.focus = null; AC.stage = null; const r = baseOpenChat(el); Promise.resolve(r).then(scheduleActivity); return r; },
  "new-chat": el => { AC.focus = null; AC.stage = null; baseNewChat(el); scheduleActivity(); },
  "toggle-activity": () => { closeMenu(); setOpen(!AC.open); },
  "toggle-files": el => { if (!S.drawer && AC.open) setOpen(false); baseToggleFiles(el); },
  "ac-pause": () => guarded(async () => {
    const r = await api("POST", "/api/dispatch", { paused: !AC.data.paused });
    toast(r.paused ? "New tasks will wait. Running work continues." : `Resumed. Starting ${r.queued} queued task(s).`);
    await loadActivity();
  }),
  "ac-focus": el => { AC.focus = Number(el.dataset.id); AC.stage = null; loadActivity(); },
  "ac-hold": el => guarded(async () => {
    const hold = !!el.dataset.hold;
    await api("POST", `/api/tasks/${el.dataset.id}/team/hold`, { hold });
    toast(hold ? "Delegation paused. The current step finishes; nothing new starts." : "Delegation resumed.");
    await loadActivity();
  }),
  "ac-tt-do": el => guarded(async () => {
    const what = el.dataset.do;
    if (what === "model" && el.dataset.provider === "claude" && !(await confirmBox("Move this task to Claude?",
      `<p>The task will be sent to Claude through Claude Code when it runs. This uses your Claude plan.</p>`, "Use Claude"))) return;
    await api("POST", `/api/team-tasks/${el.dataset.id}/${what}`, { provider: el.dataset.provider });
    toast({ retry: "The task will run again.", cancel: "Task cancelled.", model: "Model changed for this task." }[what]);
    await loadActivity();
  }),
  "ac-tt": el => { const k = "tt" + el.dataset.id; AC.expanded.has(k) ? AC.expanded.delete(k) : AC.expanded.add(k); renderActivity(); },
  "ac-stage": el => { AC.stage = AC.stage === el.dataset.key ? null : el.dataset.key; renderActivity(); },
  "ac-ev": el => { const id = Number(el.dataset.id); AC.expanded.has(id) ? AC.expanded.delete(id) : AC.expanded.add(id); renderActivity(); },
  "ac-open-chat": el => guarded(async () => {
    const pid = Number(el.dataset.project);
    if (!S.project || S.project.id !== pid) { await useProject(pid); await loadProjects(); }
    await loadChat(Number(el.dataset.id));
    AC.focus = null;
    if (window.innerWidth < NARROW) setOpen(false);
    renderAll(true);
    loadActivity();
  }),
  "ac-cancel": async el => {
    const ok = await confirmBox("Cancel this task?", `<p>Running work stops at the next safe point. For ChatGPT, Enoy stops the Codex request.
      For Claude, it stops the local Claude Code process; a request Anthropic already received may still count toward your usage.</p>
      <p class="small muted">The panel shows “Cancelled” only after the task has actually stopped.</p>`, "Cancel task", true);
    if (ok) taskPost(el.dataset.id, "cancel", "Cancel requested.");
  },
  "ac-retry": el => taskPost(el.dataset.id, "resume", "Retrying from the saved task state."),
  "ac-claude-decline": el => taskPost(el.dataset.id, "claude/decline", "Kept with ChatGPT. Nothing was sent to Claude."),
  "ac-claude-approve": el => guarded(async () => {
    const p = (await api("GET", `/api/tasks/${el.dataset.id}/claude-preview`)).pending;
    if (!p) { toast("Nothing is waiting to be sent."); return; }
    const ok = await confirmBox("Send this to Claude?", `<p>${p.chars.toLocaleString()} characters and ${p.files.length} file(s) will be sent to Claude through Claude Code.
      This uses your Claude plan.</p>${p.files.length ? `<p class="small muted">${p.files.map(f => esc(f.path)).join(", ")}</p>` : ""}
      <p class="small muted">Open the chat to read the full request first.</p>`, "Send to Claude");
    if (ok) taskPost(el.dataset.id, "claude/approve", "Sent to Claude.");
  }),
  "ac-review": el => guarded(async () => {
    const t = await api("GET", `/api/tasks/${el.dataset.id}`);
    const pending = t.changesets.filter(c => c.status === "pending");
    if (!pending.length) { toast("No changes are waiting."); loadActivity(); return; }
    openModal(`<h2>Review changes · ${esc(t.task.title)}</h2>${pending.map(c => `<div class="card">
      <div class="card-title">${avatar(c.backend)} Change set #${c.id} <span class="muted small">by ${esc(NAME[c.backend] || c.backend)}</span></div>
      ${c.note ? `<div class="small">${esc(c.note)}</div>` : ""}
      ${c.files.map(f => `<div class="small muted">${esc(f.action)} ${esc(f.path)}</div>${diffHtml(f.diff)}`).join("")}
      <div class="row"><button class="btn accent" data-action="ac-apply" data-cs="${c.id}">Apply changes</button>
        <button class="btn" data-action="ac-discard" data-cs="${c.id}">Discard</button></div></div>`).join("")}
      <div class="row" style="justify-content:flex-end"><button class="btn ghost" data-action="close-modal">Close</button></div>`);
  }),
  "ac-apply": el => guarded(async () => {
    await api("POST", `/api/changesets/${el.dataset.cs}/approve`);
    closeModal(); toast("Changes applied. Backups were saved."); loadActivity(); scheduleRefresh();
  }),
  "ac-discard": el => guarded(async () => {
    await api("POST", `/api/changesets/${el.dataset.cs}/reject`, { reason: "Discarded from the Activity panel." });
    closeModal(); toast("Changes discarded. No files were changed."); loadActivity(); scheduleRefresh();
  }),
  "ac-log": el => guarded(async () => {
    const l = await api("GET", `/api/activity/log?task_id=${el.dataset.id}`);
    openModal(`<h2>Log · ${esc(l.task)}</h2>
      <div class="ac-log">${l.activity.map(e => `<div class="ev ${e.kind.includes("fail") || e.outcome === "failed" ? "error" : ""}"><span class="t">${clock(e.created)}</span>
        <span class="x">${who(e.agent)} ${esc(e.text)}${e.reason ? `<span class="muted"> — ${esc(e.reason)}</span>` : ""}</span></div>`).join("")}</div>
      ${l.runs.length ? `<h3 class="small">Checks</h3>${l.runs.map(r => `<div class="small"><code>${esc(r.command)}</code> · ${esc(r.status)}${r.exit_code !== null ? ` (exit ${r.exit_code})` : ""}</div><pre class="log">${esc(r.tail || "(no output)")}</pre>`).join("")}` : ""}
      <details><summary class="small muted">App log (last 40 lines, secrets removed)</summary><pre class="log">${esc(l.app_log || "(empty)")}</pre></details>
      <div class="row" style="justify-content:flex-end"><button class="btn ghost" data-action="close-modal">Close</button></div>`);
  }),
  "ac-files": el => {
    const t = AC.data.tasks.find(x => x.id === Number(el.dataset.id)) || AC.data.focus.task;
    openModal(`<h2>Files used by this task</h2><p class="small muted">Files Enoy selected as relevant. Select one to read it.</p>
      <div class="stack">${t.relevant_files.map(p => `<button class="btn" style="text-align:left" data-action="ac-file" data-project="${t.project_id}" data-path="${esc(p)}"><code>${esc(p)}</code></button>`).join("")}</div>
      <div class="row" style="justify-content:flex-end"><button class="btn ghost" data-action="close-modal">Close</button></div>`);
  },
  "ac-file": el => guarded(async () => {
    const f = await api("GET", `/api/projects/${el.dataset.project}/file?path=${encodeURIComponent(el.dataset.path)}`);
    openModal(`<h2><code>${esc(f.path)}</code></h2><pre class="log" style="max-height:60vh">${esc(f.content)}</pre>
      <div class="row" style="justify-content:flex-end"><button class="btn ghost" data-action="close-modal">Close</button></div>`);
  }),
  "ac-explain": el => guarded(async () => {
    openModal(`<h2>Explain workflow</h2><p>${avatar("qwen")} Asking ChatGPT via Codex… <span class="typing"><span></span><span></span><span></span></span></p>
      <p class="small muted">This is one local request. ChatGPT reads only the step log (titles, models, reasons, results), not your chat or files. Claude is not used.</p>`);
    try {
      const r = await api("POST", "/api/activity/explain", { task_id: Number(el.dataset.id) });
      openModal(`<h2>Explain workflow</h2><div class="ai-body">${md(r.text)}</div>
        <p class="small muted">Written by ChatGPT via Codex in ${r.seconds} s from the step log below. Check it against the timeline; the log is the source of truth.</p>
        <details><summary class="small muted">Step log ChatGPT read</summary><pre class="log">${esc(r.log)}</pre></details>
        <div class="row" style="justify-content:flex-end"><button class="btn ghost" data-action="close-modal">Close</button></div>`);
    } catch (e) { closeModal(); throw e; }
    loadActivity();
  }),
});

document.addEventListener("toggle", e => {
  const key = e.target.dataset && e.target.dataset.key;
  if (key && e.target.closest("#activity")) e.target.open ? AC.openDetails.add(key) : AC.openDetails.delete(key);
}, true);
document.addEventListener("change", e => {
  const map = { "ac-scope": "scope", "ac-agent": "agent", "ac-type": "type" };
  const key = map[e.target.id];
  if (!key) return;
  AC[key] = e.target.value;
  lsSet("ac" + key[0].toUpperCase() + key.slice(1), e.target.value);
  if (key === "scope") loadActivity(); else renderActivity();
});
document.addEventListener("keydown", e => {
  if (e.key.toLowerCase() === "a" && e.ctrlKey && e.shiftKey) { e.preventDefault(); setOpen(!AC.open); }
  if (e.key === "Escape" && AC.open && $("modal").classList.contains("hidden") && $("menu").classList.contains("hidden")
      && document.activeElement && document.activeElement.closest("#activity")) { setOpen(false); $("activity-btn").focus(); }
});

if (lsGet("activityOpen") === "1" && window.innerWidth >= NARROW) setOpen(true);
