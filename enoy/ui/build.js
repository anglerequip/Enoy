"use strict";
// Build workspace: a planning model writes a reviewed, test-first plan; cheaper models implement it one
// task at a time in separate Git worktrees while Enoy runs the tests and checks itself.
// Uses app.js helpers (S, api, esc, openModal, closeModal, confirmBox, toast, $, lsGet, lsSet, guarded)
// and projects.js page helpers (setView, renderPage, loadingHtml, errorHtml, ago, submitOnce).

// ---------------------------------------------------------------- pure helpers (tests/js/build.test.js)

const BUILD_STATUS = {
  pending: ["Waiting for earlier tasks", "muted"], ready: ["Ready", ""], claimed: ["Starting", "run"],
  testing: ["Writing tests", "run"], implementing: ["Implementing", "run"], verifying: ["Checking", "run"],
  reviewing: ["In review", "run"], merging: ["Merging", "run"], completed: ["Done", "ok"], failed: ["Failed", "bad"],
  blocked: ["Blocked", "bad"], waiting_network: ["Waiting for connection", "warn"], interrupted: ["Interrupted", "warn"],
  cancelled: ["Cancelled", "muted"],
};
const BUILD_TABS = [["project", "Project"], ["plan", "Plan"], ["tasks", "Tasks"], ["agents", "Agents"], ["monitor", "Monitor"]];
const ROLE_LABEL = { planner: "Planner", implementer: "Implementer", reviewer: "Reviewer" };

function buildBadge(status) {
  const [label, cls] = BUILD_STATUS[status] || [String(status || "unknown"), "muted"];
  return `<span class="bstat ${cls}">${esc(label)}</span>`;
}
// Whether a role can be used right now, and why not. online is navigator.onLine.
function roleState(r, online) {
  if (!r || !r.id) return { ok: false, text: "Not chosen" };
  if (!r.local && !online) return { ok: false, text: "Offline: cloud models are unavailable" };
  if (!r.ok) return { ok: false, text: r.reason || "Not ready" };
  return { ok: true, text: r.local ? "Local · ready" : "Cloud · ready" };
}
function canApprove(detail, version) {
  const vs = (detail && detail.versions) || [];
  return !!(detail && detail.plan.approved_version == null && vs.length && vs[vs.length - 1].version === version && !detail.job.busy);
}
function planProgress(tasks) {
  const total = (tasks || []).length, done = (tasks || []).filter(t => t.status === "completed").length;
  return { done, total, label: total ? `${done} of ${total} tasks done` : "No tasks yet" };
}
function busyTasks(tasks) {
  return (tasks || []).some(t => ["claimed", "testing", "implementing", "verifying", "reviewing", "merging"].includes(t.status));
}

// ---------------------------------------------------------------- state and loading

S.build = { tab: lsGet("buildTab") || "project", projectId: Number(lsGet("buildProject")) || null, planId: null,
            version: null, projects: null, roles: {}, plans: [], detail: null, monitor: null, error: null };
let buildPoll = null;

async function showBuild() {
  setView("build");
  S.build.error = null;
  renderPage();
  try {
    const d = await api("GET", "/api/build/projects");
    S.build.projects = d.projects;
    S.build.roles = d.roles;
    if (!d.projects.some(p => p.id === S.build.projectId)) S.build.projectId = d.projects.length ? d.projects[0].id : null;
    await loadBuildPlans();
  } catch (e) { S.build.error = e.message; }
  if (S.view === "build") renderPage();
}
async function loadBuildPlans() {
  S.build.plans = S.build.projectId ? (await api("GET", `/api/build/plans?project_id=${S.build.projectId}`)).plans : [];
  if (S.build.planId && !S.build.plans.some(p => p.id === S.build.planId)) S.build.planId = null;
  if (!S.build.planId && S.build.plans.length) S.build.planId = S.build.plans[0].id;
  await loadBuildDetail();
}
async function loadBuildDetail() {
  S.build.detail = S.build.planId ? await api("GET", `/api/build/plans/${S.build.planId}`) : null;
  const d = S.build.detail;
  if (d && (S.build.version == null || !d.versions.some(v => v.version === S.build.version)))
    S.build.version = d.versions.length ? d.versions[d.versions.length - 1].version : null;
  if (S.build.tab === "monitor") S.build.monitor = await api("GET", "/api/build/monitor");
  scheduleBuildPoll();
}
function scheduleBuildPoll() {
  clearTimeout(buildPoll);
  const d = S.build.detail;
  if (S.view !== "build" || !d || !(d.job.busy || busyTasks(d.tasks))) return;
  buildPoll = setTimeout(async () => {
    try { await loadBuildDetail(); } catch (e) { /* the next action shows errors */ }
    if (S.view === "build") renderBuildBody();
  }, 2000);
}

// ---------------------------------------------------------------- page

function buildPageHtml() {
  const b = S.build;
  const head = `<div class="page-head"><h1>Build</h1>${b.projects && b.projects.length ? `<div class="row page-tools">
      <select id="build-project" aria-label="Project">${b.projects.map(p => `<option value="${p.id}" ${p.id === b.projectId ? "selected" : ""}>${esc(p.name)}</option>`).join("")}</select></div>` : ""}</div>
    <p class="page-sub">A planning model writes a test-first plan for you to approve. Cheaper models then implement it one task
      at a time, each in its own Git worktree; Enoy runs the tests and checks itself. Your branch changes only when you merge.</p>`;
  if (b.error) return head + errorHtml(b.error, "nav-build");
  if (!b.projects) return head + loadingHtml();
  if (!b.projects.length) return head + `<div class="empty-state"><div class="es-icon">⌘</div><h2>No code folder yet</h2>
      <p>Open a project folder on this PC. The Build workspace plans and implements changes in it.</p>
      <button class="btn primary" data-action="pick-folder">Open folder…</button></div>`;
  const offline = navigator.onLine ? "" : `<div class="bnote warn" role="status">You are offline. Plans, tasks and results stay
    available; cloud models cannot be used until the connection is back. Local models still work.</div>`;
  return head + offline + `<div class="seg build-tabs" role="tablist" aria-label="Build sections">${BUILD_TABS.map(([k, l]) =>
    `<button role="tab" class="${b.tab === k ? "on" : ""}" aria-selected="${b.tab === k}" data-action="build-tab" data-tab="${k}">${l}</button>`).join("")}</div>
    <div id="build-body">${buildBodyHtml()}</div>`;
}
function renderBuildBody() {
  const el = $("build-body");
  if (el) el.innerHTML = buildBodyHtml(); else renderPage();
  bindBuild();
}
function buildBodyHtml() {
  try {
    return ({ project: buildProjectTab, plan: buildPlanTab, tasks: buildTasksTab, agents: buildAgentsTab, monitor: buildMonitorTab })[S.build.tab]();
  } catch (e) { return `<div class="page-state bad">${esc(e.message)}</div>`; }
}
function bindBuild() {
  const sel = $("build-project");
  if (sel) sel.onchange = () => guarded(async () => {
    S.build.projectId = Number(sel.value); S.build.planId = null; S.build.version = null;
    lsSet("buildProject", sel.value);
    await loadBuildPlans(); renderBuildBody();
  });
}
function project() { return (S.build.projects || []).find(p => p.id === S.build.projectId); }

function rolesHtml(roles) {
  return `<div class="plist">${["planner", "implementer", "reviewer"].map(k => {
    const r = roles[k] || {}, st = roleState(r, navigator.onLine);
    return `<div class="plist-row static"><span class="pl-title"><b>${ROLE_LABEL[k]}</b> · ${esc(r.id || "none")}${r.model ? ` <span class="muted">(${esc(r.model)})</span>` : ""}</span>
      <span class="bstat ${st.ok ? "ok" : "warn"}">${esc(st.text)}</span></div>`;
  }).join("")}</div><button class="link-btn" data-action="open-settings" data-tab="providers">Change models and keys in Settings → Providers</button>`;
}

function buildProjectTab() {
  const p = project(), b = S.build;
  if (!p) return "";
  const git = p.git ? `${esc(p.branch || "detached HEAD")} · ${p.uncommitted ? `${p.uncommitted} uncommitted file${p.uncommitted === 1 ? "" : "s"}` : "clean"}`
    : `Not a Git repository <button class="btn small" data-action="build-git-init">Set up Git…</button>`;
  return `<div class="psec"><h2>${esc(p.name)}</h2><div class="small muted">${esc(p.path)}${p.local_only ? " · stays on this PC (local-only)" : ""}</div>
      <div class="kv"><span>Git</span><span>${git}</span>
        <span>Instructions</span><span>${p.instructions.length ? p.instructions.map(n => `<code>${esc(n)}</code>`).join(", ") : "none found"}
          <button class="btn small ghost" data-action="build-claude-md">Draft CLAUDE.md…</button></span>
        <span>Test commands</span><span>${p.test_commands.length ? p.test_commands.map(c => `<code>${esc(c)}</code>`).join(", ") : "none found"}</span>
        <span>planning.md</span><span>${p.planning_md ? "present" : "not yet"}</span></div></div>
    <div class="psec"><h2>Models</h2>${rolesHtml(b.roles)}</div>
    <div class="psec"><div class="psec-head"><h2>Plans</h2><div class="grow"></div><button class="btn primary small" data-action="build-new-plan">New plan</button></div>
      ${b.plans.length ? `<div class="plist">${b.plans.map(x => `<button class="plist-row" data-action="build-open-plan" data-id="${x.id}">
        <span class="pl-title">${esc(x.title)}</span><span class="pl-tag">${esc(x.status)}</span>
        <span class="pl-meta">${x.approved_version ? `${x.done} of ${x.tasks} done` : `${x.versions} version${x.versions === 1 ? "" : "s"}`} · ${esc(ago(x.updated))}</span></button>`).join("")}</div>`
        : `<div class="small muted">No plans for this project yet.</div>`}</div>`;
}

function versionHtml(v) {
  const body = v.body;
  const list = items => items && items.length ? `<ul>${items.map(i => `<li>${esc(i)}</li>`).join("")}</ul>` : `<p class="small muted">None listed.</p>`;
  return `<div class="psec"><h2>Summary</h2><p>${esc(body.summary)}</p><h2>Architecture</h2><p>${esc(body.architecture)}</p>
      <h2>Risks</h2>${list(body.risks)}<h2>Open questions</h2>${list(body.questions)}</div>
    <div class="psec"><h2>Tasks (${body.tasks.length})</h2><div class="plist">${body.tasks.map(t => `<button class="plist-row" data-action="build-spec" data-key="${esc(t.id)}">
      <span class="pl-title"><b>${esc(t.id)}</b> ${esc(t.title)}</span>
      <span class="pl-meta">${t.depends_on.length ? `after ${esc(t.depends_on.join(", "))} · ` : ""}${t.allowed_files.length + t.test_files.length} files · ${t.tests.length} test${t.tests.length === 1 ? "" : "s"}</span></button>`).join("")}</div></div>`;
}

function buildPlanTab() {
  const d = S.build.detail, planner = roleState(S.build.roles.planner, navigator.onLine);
  if (!S.build.planId || S.build.newPlan) {
    return `<div class="psec"><h2>New plan</h2>
      <label class="field">What should be built?<textarea id="build-goal" rows="5" maxlength="8000" placeholder="e.g. Add CSV export to the reports page, with tests."></textarea></label>
      <div class="small muted">Planner: ${esc(S.build.roles.planner?.id || "none")} · ${esc(planner.text)}. The planner reads the project but cannot change it.</div>
      <div class="row"><button class="btn primary" data-action="build-create-plan" ${planner.ok ? "" : `disabled title="${esc(planner.text)}"`}>Write plan</button>
        ${S.build.plans.length ? `<button class="btn ghost" data-action="build-cancel-new">Cancel</button>` : ""}</div></div>`;
  }
  if (!d) return loadingHtml();
  const p = d.plan, job = d.job;
  const v = d.versions.find(x => x.version === S.build.version);
  const jobLine = job.busy ? `<div class="bnote" role="status"><span class="typing"><span></span><span></span><span></span></span> ${esc(job.kind)}…</div>`
    : job.error ? `<div class="bnote bad" role="alert">${esc(job.kind)} failed: ${esc(job.error)}</div>` : "";
  const versions = d.versions.length > 1 ? `<div class="seg" role="tablist" aria-label="Versions">${d.versions.map(x =>
    `<button role="tab" class="${x.version === S.build.version ? "on" : ""}" data-action="build-version" data-v="${x.version}">v${x.version}${x.approved ? " ✓" : ""}</button>`).join("")}</div>` : "";
  const approved = p.approved_version != null;
  const actions = approved ? `<div class="psec"><h2>Approved</h2>
      <div class="kv"><span>Version</span><span>${p.approved_version}</span><span>Branch</span><span><code>${esc(p.integration_branch || "created on the first run")}</code></span>
        <span>Changed files</span><span>${d.changed_files.length ? d.changed_files.map(f => `<code>${esc(f)}</code>`).join(", ") : "none yet"}</span></div>
      <div class="row"><button class="btn" data-action="build-planning-md">Update planning.md</button>
        <button class="btn" data-action="build-merge" ${d.changed_files.length ? "" : "disabled"}>Merge into my branch…</button></div></div>`
    : v && !job.busy ? `<div class="psec"><h2>Review</h2>
      <label class="field">Request changes<textarea id="build-comment" rows="3" maxlength="4000" placeholder="What should the planner change?"></textarea></label>
      <div class="row"><button class="btn" data-action="build-revise">Request changes</button>
        ${canApprove(d, v.version) ? `<button class="btn primary" data-action="build-approve" data-v="${v.version}">Approve version ${v.version}</button>` : `<span class="small muted">Only the latest version can be approved.</span>`}</div></div>` : "";
  return `<div class="page-head"><div><h2>${esc(p.title)}</h2><div class="small muted">${esc(p.goal)}</div></div>
      <div class="row">${buildBadge(approved ? (p.status === "completed" ? "completed" : "ready") : "pending").replace(/Ready|Waiting for earlier tasks/, approved ? "Approved" : "Draft")}</div></div>
    ${jobLine}${versions}
    ${d.warnings.length ? `<div class="bnote warn">${d.warnings.map(esc).join("<br>")}</div>` : ""}
    ${v ? (v.comment ? `<div class="small muted">Version ${v.version} was asked for: “${esc(v.comment)}”</div>` : "") + versionHtml(v) : job.busy ? "" : `<div class="small muted">No version yet.</div>`}
    ${actions}`;
}

function buildTasksTab() {
  const d = S.build.detail;
  if (!d) return `<div class="small muted">Create a plan first.</div>`;
  if (d.plan.approved_version == null) return `<div class="small muted">Approve the plan to create its tasks.</div>`;
  const impl = roleState(S.build.roles.implementer, navigator.onLine), prog = planProgress(d.tasks);
  const disabled = impl.ok && !d.job.busy ? "" : `disabled title="${esc(d.job.busy ? "Already running" : impl.text)}"`;
  return `<div class="row build-bar"><button class="btn primary" data-action="build-run" data-mode="next" ${disabled}>Run next task</button>
      <button class="btn" data-action="build-run" data-mode="all" ${disabled}>Run all</button>
      ${d.parallel ? `<button class="btn" data-action="build-run" data-mode="parallel" ${disabled}>Run in parallel</button>` : ""}
      ${d.job.busy || busyTasks(d.tasks) ? `<button class="btn danger" data-action="build-stop">Stop</button>` : ""}
      <span class="small muted">${esc(prog.label)} · implementer: ${esc(S.build.roles.implementer?.id || "none")} (${esc(impl.text)})</span></div>
    ${d.job.error ? `<div class="bnote bad">${esc(d.job.kind)}: ${esc(d.job.error)}</div>` : d.job.result && d.job.result.reason ? `<div class="bnote warn">${esc(d.job.result.reason)}</div>` : ""}
    <div class="plist">${d.tasks.map(t => `<button class="plist-row" data-action="build-task" data-id="${t.id}">
      ${t.status === "completed" ? `<span class="tick" aria-hidden="true">✓</span>` : `<span class="tick open" aria-hidden="true"></span>`}
      <span class="pl-title"><b>${esc(t.task_key)}</b> ${esc(t.title)}${t.blocker && t.status !== "completed" ? `<span class="pl-sub">${esc(t.blocker.slice(0, 160))}</span>` : ""}</span>
      <span class="pl-meta">${[t.depends_on.length ? `after ${esc(t.depends_on.join(", "))}` : "", t.attempts ? `attempt ${t.attempts}` : ""].filter(Boolean).join(" · ")}</span>${buildBadge(t.status)}</button>`).join("")}</div>`;
}

function specHtml(t) {
  const li = a => a && a.length ? `<ul>${a.map(x => `<li>${esc(x)}</li>`).join("")}</ul>` : `<span class="muted">none</span>`;
  const code = a => a && a.length ? a.map(x => `<code>${esc(x)}</code>`).join(", ") : `<span class="muted">none</span>`;
  return `<div class="kv spec"><span>Objective</span><span>${esc(t.objective)}</span>
    <span>Allowed files</span><span>${code(t.allowed_files)}</span><span>Test files</span><span>${code(t.test_files)}</span>
    <span>Specification</span><span class="pre">${esc(t.spec)}</span><span>Inputs and outputs</span><span>${esc(t.inputs_outputs)}</span>
    <span>Edge cases</span><span>${li(t.edge_cases)}</span>
    <span>Tests first</span><span>${li((t.tests || []).map(x => `${x.name} in ${x.file}: fails before with ${x.expect_failure}`))}</span>
    <span>Verify</span><span>${code(t.verify_commands)}</span><span>Regression</span><span>${code(t.regression_commands)}</span>
    <span>Acceptance</span><span>${li(t.acceptance)}</span><span>Done when</span><span>${esc(t.completion)}</span>
    <span>Stop and report if</span><span>${li(t.blockers)}</span></div>`;
}
function openTask(id) {
  const t = S.build.detail.tasks.find(x => x.id === id);
  if (!t) return;
  const ev = Object.entries(t.evidence || {});
  openModal(`<h2>${esc(t.task_key)} ${esc(t.title)} ${buildBadge(t.status)}</h2>
    ${t.blocker ? `<div class="bnote ${t.status === "completed" ? "" : "bad"}">${esc(t.blocker)}</div>` : ""}
    ${t.commit_id ? `<div class="small">Commit <code>${esc(t.commit_id.slice(0, 12))}</code>${t.summary ? ` · ${esc(t.summary)}` : ""}</div>` : ""}
    ${ev.length ? `<h3>Evidence</h3><div class="kv">${ev.map(([k, v]) => `<span>${esc(k)}</span><span>${esc(v)}</span>`).join("")}</div>` : ""}
    ${Object.keys(t.frozen || {}).length ? `<h3>Frozen tests</h3><div class="small">${Object.entries(t.frozen).map(([p, h]) => `<code>${esc(p)}</code> · SHA-256 ${esc(h.slice(0, 12))}…`).join("<br>")}</div>` : ""}
    <h3>Specification</h3>${specHtml(t.spec)}
    ${t.runs.length ? `<h3>Runs</h3><div class="plist">${t.runs.slice().reverse().map(r => `<div class="plist-row static">
      <span class="pl-title">Attempt ${r.attempt} · ${esc(r.phase)}${r.error ? `<span class="pl-sub">${esc(r.error.slice(0, 200))}</span>` : ""}</span>
      <span class="pl-meta">${esc(r.model || r.provider || "")} · ${esc(ago(r.started))}</span>${buildBadge(r.status === "running" ? "implementing" : r.status)}</div>`).join("")}</div>` : ""}
    <div class="row" style="justify-content:flex-end">
      ${t.status === "ready" ? `<button class="btn primary" data-action="build-run-task" data-id="${t.id}">Run this task</button>` : ""}
      ${["failed", "blocked", "waiting_network", "interrupted"].includes(t.status) ? `<button class="btn" data-action="build-retry" data-id="${t.id}">Try again</button>` : ""}
      <button class="btn ghost" data-action="close-modal">Close</button></div>`); $("modal-box").classList.add("build-modal");
}

function buildAgentsTab() {
  const d = S.build.detail;
  if (!d) return `<div class="small muted">Create a plan first.</div>`;
  const runs = d.tasks.flatMap(t => t.runs.map(r => ({ ...r, key: t.task_key, title: t.title }))).sort((a, b) => b.started - a.started);
  return `<div class="psec"><h2>Now</h2>${d.job.busy ? `<div class="bnote" role="status"><span class="typing"><span></span><span></span><span></span></span> ${esc(d.job.kind)} since ${esc(ago(d.job.started))}</div>`
      : `<div class="small muted">Nothing is running.${d.job.kind ? ` Last: ${esc(d.job.kind)}${d.job.error ? " failed" : " finished"}.` : ""}</div>`}</div>
    <div class="psec"><h2>Implementer runs</h2>${runs.length ? `<div class="plist">${runs.map(r => `<button class="plist-row" data-action="build-task" data-id="${r.task_id}">
      <span class="pl-title"><b>${esc(r.key)}</b> attempt ${r.attempt} · ${esc(r.phase)}${r.error ? `<span class="pl-sub">${esc(r.error.slice(0, 160))}</span>` : ""}</span>
      <span class="pl-meta">${esc(r.model || "")} · <code>${esc(r.branch)}</code> · ${esc(ago(r.started))}</span>${buildBadge(r.status === "running" ? "implementing" : r.status)}</button>`).join("")}</div>`
      : `<div class="small muted">No runs yet.</div>`}
    <div class="small muted">Every run works in its own worktree under Enoy's data folder. Each task starts with a fresh model context.</div></div>`;
}

function buildMonitorTab() {
  const m = S.build.monitor, d = S.build.detail;
  if (!m) return loadingHtml();
  const tests = d ? d.tasks.filter(t => t.evidence && t.evidence.tests).map(t => `<div class="plist-row static"><span class="pl-title"><b>${esc(t.task_key)}</b> ${esc(t.evidence.tests)}</span>${buildBadge(t.status)}</div>`).join("") : "";
  return `<div class="psec"><h2>Usage</h2>
      <div class="small muted">${m.totals.calls} request${m.totals.calls === 1 ? "" : "s"} · ${m.totals.input.toLocaleString()} input tokens (${m.totals.cached.toLocaleString()} cached) · ${m.totals.output.toLocaleString()} output tokens.
        Build's own models run on this PC and cost nothing. Requests through the signed-in Claude and Codex apps use your plans and are not listed here.</div></div>
    ${m.attention.length ? `<div class="psec"><h2>Needs attention</h2><div class="plist">${m.attention.map(t => `<div class="plist-row static"><span class="pl-title"><b>${esc(t.task_key)}</b> ${esc(t.title)}<span class="pl-sub">${esc((t.blocker || "").slice(0, 200))}</span></span>${buildBadge(t.status)}</div>`).join("")}</div></div>` : ""}
    ${tests ? `<div class="psec"><h2>Test results</h2><div class="plist">${tests}</div></div>` : ""}
    <div class="psec"><h2>Model requests</h2>${m.calls.length ? `<div class="plist">${m.calls.map(c => `<div class="plist-row static">
      <span class="pl-title">${esc(c.provider)} · ${esc(c.model)} · ${esc(c.role || c.purpose)}${c.error ? `<span class="pl-sub">${esc(c.error.slice(0, 160))}</span>` : ""}</span>
      <span class="pl-meta">${c.input_tokens ?? "?"} in${c.cached_tokens ? ` (${c.cached_tokens} cached)` : ""} · ${c.output_tokens ?? "?"} out · ${esc(ago(c.created))}</span>
      <span class="pl-tag ${c.status === "done" ? "" : "muted"}">${esc(c.status)}</span></div>`).join("")}</div>` : `<div class="small muted">No requests yet.</div>`}</div>
    <div class="psec"><h2>Recent events</h2>${m.activity.length ? `<div class="plist">${m.activity.map(a => `<div class="plist-row static"><span class="pl-title">${esc(a.text)}</span><span class="pl-meta">${esc(ago(a.created))}</span></div>`).join("")}</div>` : `<div class="small muted">Nothing yet.</div>`}</div>`;
}

// ---------------------------------------------------------------- Settings → Providers

async function settingsProviders() {
  const info = await api("GET", "/api/build/providers");
  const b = S.config.build;
  const models = b.models;
  const choose = role => `<label class="field">${ROLE_LABEL[role]}<select data-build-role="${role}"><option value="">None</option>${models.filter(m => (m.roles || []).includes(role)).map(m =>
    `<option value="${esc(m.id)}" ${b.roles[role] === m.id ? "selected" : ""}>${esc(m.id)}${m.model ? ` (${esc(m.model)})` : ""}</option>`).join("")}</select></label>`;
  const stateText = { disabled: "Off", no_connection: "No connection", key_refused: "Key refused", error: "Error", ready: "Ready (not yet verified)", verified: "Verified with a real request" };
  const card = st => {
    const p = b.providers[st.id];
    return `<div class="set-section prov-card" data-pid="${esc(st.id)}"><div class="psec-head"><h3>${esc(st.id)}</h3>
        <span class="pl-tag">On this PC · free</span><div class="grow"></div>
        <span class="bstat ${st.state === "verified" || st.state === "ready" ? "ok" : st.state === "disabled" ? "muted" : "warn"}">${esc(stateText[st.state] || st.state)}</span>
        <input type="checkbox" data-bp="enabled" ${p.enabled ? "checked" : ""} aria-label="Use ${esc(st.id)}"></div>
      <div class="grid2"><label class="field">Address<input type="text" data-bp="base_url" value="${esc(p.base_url)}"></label>
        <label class="field">Requests at once<input type="number" min="1" max="8" data-bp="max_concurrency" value="${esc(p.max_concurrency)}"></label></div>
      <div class="small muted">The address must be on this PC (localhost or 127.0.0.1). Build never sends work to paid online services.</div>
      ${p.kind === "ollama" ? localModelsHtml(st) : ""}
      ${p.kind !== "openai" ? "" : `<label class="field">Access key (optional)<div class="row"><input type="password" autocomplete="off" id="bkey-${esc(st.id)}" placeholder="${st.key_set ? "Saved in Windows Credential Manager" : "Not set"}">
        <button class="btn" data-action="build-key" data-pid="${esc(st.id)}">Save</button>
        ${st.key_set ? `<button class="btn ghost" data-action="build-key" data-pid="${esc(st.id)}" data-clear="1">Remove</button>` : ""}</div>
        <span class="small">Only if your local server asks for one. Kept in Windows Credential Manager; never shown again, logged or sent to a model.</span></label>`}
      <div class="row"><button class="btn small" data-action="build-test" data-pid="${esc(st.id)}" ${p.enabled ? "" : "disabled"}>Test connection</button>
        <span class="small muted">${st.models.length ? `${st.models.length} models listed${st.verified_at ? ` · verified ${esc(ago(st.verified_at))}` : ""}` : ""}</span></div>
</div>`;
  };
  $("set-body").innerHTML = `
    <div class="set-section"><h3>Roles</h3>
      <div class="small muted">The planner writes plans and never edits code. Implementers write one approved task at a time through Enoy's
        limited tools, so they must be free models that run on this PC. The reviewer must differ from the implementer.</div>
      <div class="grid2">${choose("planner")}${choose("implementer")}${choose("reviewer")}</div>
      <div class="set-row"><div class="lbl"><span>Run independent tasks in parallel</span><span class="d">Off by default. Only tasks that share no files run together; merges still happen one at a time.</span></div>
        <input type="checkbox" id="build-parallel" ${b.parallel ? "checked" : ""}></div></div>
    <div class="set-section"><h3>Models</h3>
      <div class="plist">${models.map(m => `<div class="plist-row static"><span class="pl-title"><b>${esc(m.id)}</b> · ${esc(m.provider)}${m.model ? ` · ${esc(m.model)}` : ""}
        <span class="pl-sub">${esc((m.roles || []).join(", "))}</span></span>
        ${["claude-cli", "codex-cli"].includes(m.id) ? "" : `<button class="btn small ghost" data-action="build-model-remove" data-id="${esc(m.id)}">Remove</button>`}</div>`).join("")}</div>
      <button class="btn small" data-action="build-model-add">Add a model…</button></div>
    ${info.providers.map(card).join("")}`;
  document.querySelectorAll("#set-body [data-build-role]").forEach(el => el.onchange = () => guarded(async () => {
    const d = await api("POST", "/api/build/roles", { roles: { [el.dataset.buildRole]: el.value } });
    S.config.build.roles = d.roles; toast("Saved");
  }));
  $("build-parallel").onchange = e => saveCfg({ build: { parallel: e.target.checked } });
  const mem = $("build-local-memory");
  if (mem) mem.onchange = () => saveCfg({ build: { local_memory: mem.value } }).then(() => settingsProviders());
  document.querySelectorAll("#set-body .prov-card [data-bp]").forEach(el => el.addEventListener("change", () => {
    const pid = el.closest(".prov-card").dataset.pid, key = el.dataset.bp;
    const providers = JSON.parse(JSON.stringify(S.config.build.providers));
    providers[pid][key] = el.type === "checkbox" ? el.checked : el.type === "number" ? Number(el.value) : el.value.trim();
    saveCfg({ build: { providers } }).then(() => settingsProviders());
  }));
}

const MEMORY_MODES = [["chill", "Chill", 1200, 4096], ["medium", "Middle", 1600, 8192], ["full", "Full", 1900, 16384]];
function localModelsHtml(st) {
  const mode = S.config.build.local_memory || "medium";
  const limit = (MEMORY_MODES.find(m => m[0] === mode) || MEMORY_MODES[1])[2];
  return `<label class="field">Memory mode<select id="build-local-memory">${MEMORY_MODES.map(([k, l, mib, ctx]) =>
      `<option value="${k}" ${k === mode ? "selected" : ""}>${l}: at most ${mib.toLocaleString()} MiB, ${(ctx / 1024)}k context</option>`).join("")}</select>
      <span class="small">Enoy caps the model's memory to this and stops a request (and unloads the model) if it gets close.</span></label>
    <div class="small">${st.installed.length ? `Installed in Ollama: ${st.installed.map(m => `<code>${esc(m.name)}</code> ${m.size_mib.toLocaleString()} MiB on disk${m.size_mib > limit ? " (larger than the memory limit; it will run slowly or be stopped)" : ""}`).join(" · ")}`
      : "Test the connection to list the models installed in Ollama."} Enoy never downloads models.</div>`;
}

function addModelDialog() {
  const pids = Object.keys(S.config.build.providers);
  openModal(`<h2>Add a model</h2>
    <label class="field">Name in Enoy<input type="text" id="bm-id" maxlength="60" placeholder="e.g. qwen-coder"></label>
    <label class="field">Provider<select id="bm-provider">${pids.map(p => `<option>${esc(p)}</option>`).join("")}</select></label>
    <label class="field">Model ID at the provider<input type="text" id="bm-model" list="bm-known" placeholder="exactly as the provider lists it"><datalist id="bm-known"></datalist></label>
    <div class="row">${["planner", "implementer", "reviewer"].map(r => `<label class="pl-check"><input type="checkbox" data-role="${r}" ${r === "implementer" ? "checked" : ""}> ${ROLE_LABEL[r]}</label>`).join("")}</div>
    <div class="row" style="justify-content:flex-end"><button class="btn ghost" data-action="close-modal">Cancel</button><button class="btn primary" id="bm-ok">Add</button></div>`);
  const fillKnown = () => guarded(async () => {
    const st = (await api("GET", "/api/build/providers")).providers.find(x => x.id === $("bm-provider").value);
    $("bm-known").innerHTML = (st ? st.models : []).map(m => `<option value="${esc(m)}">`).join("");
  });
  $("bm-provider").onchange = fillKnown;
  fillKnown();
  const ok = $("bm-ok");
  ok.onclick = submitOnce(ok, async () => {
    const m = { id: $("bm-id").value.trim(), provider: $("bm-provider").value, model: $("bm-model").value.trim(),
                roles: [...document.querySelectorAll("#modal [data-role]:checked")].map(x => x.dataset.role) };
    if (!m.id || !m.model) { toast("Give the model a name and its provider model ID.", true); return; }
    S.config = (await api("POST", "/api/config", { config: { build: { models: [...S.config.build.models, m] } } })).config;
    toast("Model added");
    S.settingsTab = "providers"; renderSettings();
  });
}

// ---------------------------------------------------------------- actions

async function buildRefresh() { await loadBuildPlans(); renderBuildBody(); }

Object.assign(actions, {
  "nav-build": () => { closeSidebarIfNarrow(); showBuild(); },
  "build-tab": el => guarded(async () => {
    S.build.tab = el.dataset.tab; lsSet("buildTab", S.build.tab);
    if (S.build.tab === "monitor") S.build.monitor = await api("GET", "/api/build/monitor");
    renderPage();
  }),
  "build-new-plan": () => { S.build.newPlan = true; S.build.tab = "plan"; renderPage(); },
  "build-cancel-new": () => { S.build.newPlan = false; renderPage(); },
  "build-open-plan": el => guarded(async () => {
    S.build.planId = Number(el.dataset.id); S.build.version = null; S.build.newPlan = false; S.build.tab = "plan";
    await loadBuildDetail(); renderPage();
  }),
  "build-create-plan": el => guarded(async () => {
    const goal = $("build-goal").value.trim();
    if (!goal) { toast("Describe what should be built.", true); return; }
    el.disabled = true;
    const d = await api("POST", "/api/build/plans", { project_id: S.build.projectId, goal });
    S.build.newPlan = false; S.build.planId = d.plan.id; S.build.version = null;
    await buildRefresh();
  }),
  "build-version": el => { S.build.version = Number(el.dataset.v); renderBuildBody(); },
  "build-spec": el => {
    const v = S.build.detail.versions.find(x => x.version === S.build.version);
    const t = v && v.body.tasks.find(x => x.id === el.dataset.key);
    if (t) openModal(`<h2>${esc(t.id)} ${esc(t.title)}</h2>${specHtml(t)}<div class="row" style="justify-content:flex-end"><button class="btn primary" data-action="close-modal">Close</button></div>`); $("modal-box").classList.add("build-modal");
  },
  "build-revise": () => guarded(async () => {
    const comment = $("build-comment").value.trim();
    if (!comment) { toast("Say what should change.", true); return; }
    await api("POST", `/api/build/plans/${S.build.planId}/revise`, { comment });
    S.build.version = null; await buildRefresh();
  }),
  "build-approve": el => guarded(async () => {
    const v = Number(el.dataset.v);
    if (!await confirmBox(`Approve version ${v}?`, `<p>The plan is locked and its tasks are created. Enoy archives the approved
      version and writes <code>planning.md</code> into the project if that is safe. Implementers can then run the tasks.</p>`, "Approve")) return;
    const d = await api("POST", `/api/build/plans/${S.build.planId}/approve`, { version: v });
    toast(d.planning_md && d.planning_md.written === false ? "Approved. planning.md was not changed: it has edits Enoy did not make." : "Approved.");
    S.build.tab = "tasks"; await buildRefresh(); renderPage();
  }),
  "build-run": el => guarded(async () => {
    await api("POST", `/api/build/plans/${S.build.planId}/run`, { mode: el.dataset.mode });
    await buildRefresh();
  }),
  "build-run-task": el => guarded(async () => { closeModal(); await api("POST", `/api/build/tasks/${el.dataset.id}/run`, {}); await buildRefresh(); }),
  "build-retry": el => guarded(async () => { closeModal(); await api("POST", `/api/build/tasks/${el.dataset.id}/retry`, {}); await buildRefresh(); }),
  "build-stop": () => guarded(async () => { await api("POST", `/api/build/plans/${S.build.planId}/stop`, {}); toast("Stopping…"); await buildRefresh(); }),
  "build-task": el => openTask(Number(el.dataset.id)),
  "build-planning-md": () => guarded(async () => {
    const r = await api("POST", `/api/build/plans/${S.build.planId}/planning-md`, {});
    if (r.written) { toast("planning.md updated."); return; }
    openModal(`<h2>planning.md has changes Enoy did not make</h2>
      <p class="small">${r.reason === "not_enoy" ? "This planning.md was not written by Enoy." : "Someone edited planning.md after Enoy wrote it."} Replacing it keeps a backup in Enoy's data folder.</p>
      <pre class="diff">${esc(r.diff || "")}</pre>
      <div class="row" style="justify-content:flex-end"><button class="btn ghost" data-action="close-modal">Keep the file</button>
      <button class="btn danger" data-action="build-planning-md-force">Replace (backup kept)</button></div>`); $("modal-box").classList.add("build-modal");
  }),
  "build-planning-md-force": () => guarded(async () => {
    await api("POST", `/api/build/plans/${S.build.planId}/planning-md`, { confirm: true });
    closeModal(); toast("planning.md replaced; your version is backed up.");
  }),
  "build-merge": () => guarded(async () => {
    const d = S.build.detail;
    if (!await confirmBox("Merge into your branch?", `<p>Enoy merges <code>${esc(d.plan.integration_branch)}</code> into the branch you have checked out
      in <code>${esc(d.project.path)}</code>. Changed files: ${d.changed_files.map(f => `<code>${esc(f)}</code>`).join(", ")}.</p>
      <p class="small">It refuses if you have uncommitted changes in those files. You can undo it later with <code>git revert -m 1</code>.</p>`, "Merge")) return;
    const r = await api("POST", `/api/build/plans/${S.build.planId}/merge`, { confirm: true });
    toast(r.ok ? `Merged (${r.commit.slice(0, 12)}).` : r.reason, !r.ok);
    await showBuild();
  }),
  "build-git-init": () => guarded(async () => {
    if (!await confirmBox("Set up Git for this folder?", `<p>Enoy runs <code>git init</code> and makes a first commit of the folder.
      Secrets such as <code>.env</code> and generated folders are left out. Build needs Git to keep each task separate.</p>`, "Set up Git")) return;
    await api("POST", `/api/build/projects/${S.build.projectId}/git-init`, { confirm: true });
    toast("Git is set up."); await showBuild();
  }),
  "build-claude-md": el => guarded(async () => {
    el.disabled = true; toast("Asking the planner for a draft…");
    try {
      const d = await api("POST", `/api/build/projects/${S.build.projectId}/instructions/draft`, {});
      openModal(`<h2>CLAUDE.md draft</h2><p class="small muted">Written by the planner from the project's files. Edit it before saving.${d.exists ? " Saving replaces the current CLAUDE.md; Enoy keeps a backup." : ""}</p>
        <textarea id="claude-md-text" rows="18" class="mono">${esc(d.text)}</textarea>
        <div class="row" style="justify-content:flex-end"><button class="btn ghost" data-action="close-modal">Cancel</button>
        <button class="btn primary" id="claude-md-save" data-exists="${d.exists ? 1 : 0}">${d.exists ? "Replace CLAUDE.md" : "Save CLAUDE.md"}</button></div>`); $("modal-box").classList.add("build-modal");
      const save = $("claude-md-save");
      save.onclick = submitOnce(save, async () => {
        const r = await api("POST", `/api/build/projects/${S.build.projectId}/instructions`, { text: $("claude-md-text").value, confirm: save.dataset.exists === "1" });
        closeModal(); toast(r.backup ? "CLAUDE.md replaced; the old one is backed up." : "CLAUDE.md saved."); await showBuild();
      });
    } finally { el.disabled = false; }
  }),
  "build-key": el => guarded(async () => {
    const pid = el.dataset.pid, input = $(`bkey-${pid}`);
    const value = el.dataset.clear ? "" : input.value.trim();
    if (!el.dataset.clear && !value) { toast("Paste the access key first.", true); return; }
    await api("POST", `/api/build/providers/${pid}/key`, { value });
    toast(value ? "Key saved in Windows Credential Manager." : "Key removed."); settingsProviders();
  }),
  "build-test": el => guarded(async () => {
    el.disabled = true;
    const r = await api("POST", `/api/build/providers/${el.dataset.pid}/test`, {});
    toast(r.ok ? `Connected: ${r.models.length} models listed.` : r.error, !r.ok); settingsProviders();
  }),
  "build-model-add": () => addModelDialog(),
  "build-model-remove": el => guarded(async () => {
    const models = S.config.build.models.filter(m => m.id !== el.dataset.id);
    const roles = Object.fromEntries(Object.entries(S.config.build.roles).map(([k, v]) => [k, v === el.dataset.id ? "" : v]));
    S.config = (await api("POST", "/api/config", { config: { build: { models, roles } } })).config;
    toast("Model removed"); settingsProviders();
  }),
});

// The Build page renders through projects.js's page shell.
const baseRenderPage = renderPage;
renderPage = function () {
  if (S.view !== "build") { clearTimeout(buildPoll); return baseRenderPage(); }
  $("main").classList.remove("empty");
  $("thread").innerHTML = `<div class="page build-page">${buildPageHtml()}</div>`;
  $("composer-wrap").innerHTML = "";
  bindBuild();
  scheduleBuildPoll();
};
window.addEventListener("online", () => { if (S.view === "build") renderPage(); });
window.addEventListener("offline", () => { if (S.view === "build") renderPage(); });
