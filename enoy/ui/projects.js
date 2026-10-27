"use strict";
// Projects and Artifacts: two pages in the main area (Projects, Artifacts), a
// project page, and an artifact panel beside the chat. Opening these pages only
// reads saved data; nothing here ever asks a model for anything.
// Uses the helpers from app.js (S, api, esc, md, openModal, confirmBox, toast, $).

const ART_TYPES = { markdown: "Document", text: "Text", code: "Code", html: "HTML", svg: "SVG", json: "JSON", csv: "CSV" };
const ART_ICON = { markdown: "¶", text: "≡", code: "</>", html: "◇", svg: "◈", json: "{}", csv: "▦" };
S.view = "chat";          // chat | projects | project | artifacts
S.page = {};              // Data for the current page.
S.art = null;             // Open artifact: { data, draft, tab, saving }

function ago(ts) {
  if (!ts) return "";
  const s = Date.now() / 1000 - ts;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  if (s < 7 * 86400) return `${Math.floor(s / 86400)} d ago`;
  return new Date(ts * 1000).toLocaleDateString();
}
function kb(n) { return n >= 1e6 ? `${(n / 1e6).toFixed(1)} MB` : n >= 1000 ? `${Math.round(n / 1000)} KB` : `${n} B`; }
function isSpace(p) { return !!(p && p.kind === "space"); }
function folderProject(p) { return !!(p && !p.is_default && p.kind !== "space"); }

// ---------------------------------------------------------------- page shell

function setView(view) {
  S.view = view;
  document.querySelectorAll("[data-nav]").forEach(b => b.classList.toggle("active", b.dataset.nav === view || (view === "project" && b.dataset.nav === "projects")));
  $("main").classList.toggle("page-view", view !== "chat");
}

function renderPage() {
  $("main").classList.remove("empty");
  const v = S.view;
  const html = v === "projects" ? projectsPageHtml() : v === "project" ? projectPageHtml() : artifactsPageHtml();
  $("thread").innerHTML = `<div class="page">${html}</div>`;
  $("composer-wrap").innerHTML = "";
  if (v === "project") bindProjectPage();
  if (v === "artifacts") bindArtifactsPage();
}

function loadingHtml() { return `<div class="page-state"><span class="typing"><span></span><span></span><span></span></span> Loading…</div>`; }
function errorHtml(msg, retry) {
  return `<div class="page-state bad">Could not load this page: ${esc(msg)} <button class="btn small" data-action="${retry}">Try again</button></div>`;
}

// ---------------------------------------------------------------- Projects list

async function showProjects() {
  setView("projects");
  S.page = { loading: true };
  renderPage();
  try {
    const rows = (await api("GET", "/api/projects?all=1")).projects.filter(p => !p.is_default);
    S.page = { projects: rows };
  } catch (e) { S.page = { error: e.message }; }
  if (S.view === "projects") renderPage();
}

function projectsPageHtml() {
  const p = S.page;
  const head = `<div class="page-head"><h1>Projects</h1>
    <div class="row"><button class="btn" data-action="pick-folder" title="A project for a code folder on this PC">Open folder…</button>
    <button class="btn primary" data-action="project-create">New project</button></div></div>
    <p class="page-sub">A project keeps related chats, instructions, documents and artifacts together. Instructions and documents are used only in that project's chats.</p>`;
  if (p.loading) return head + loadingHtml();
  if (p.error) return head + errorHtml(p.error, "nav-projects");
  if (!p.projects.length) {
    return head + `<div class="empty-state"><div class="es-icon">▣</div><h2>No projects yet</h2>
      <p>Create a project for a topic you come back to, for example "Thesis" or "Roblox game". Add instructions once and every chat in it follows them.</p>
      <button class="btn primary" data-action="project-create">Create your first project</button></div>`;
  }
  return head + `<div class="card-grid">${p.projects.map(x => `
    <button class="proj-card" data-action="open-project-page" data-id="${x.id}">
      <div class="pc-top"><span class="pc-kind">${isSpace(x) ? "Chat project" : "Code folder"}</span><span class="pc-time">${esc(ago(x.activity))}</span></div>
      <div class="pc-name">${esc(x.name)}</div>
      <div class="pc-desc">${esc(x.description || (isSpace(x) ? "No description" : x.path))}</div>
      <div class="pc-meta">${x.chats} chat${x.chats === 1 ? "" : "s"} · ${x.files} document${x.files === 1 ? "" : "s"} · ${x.artifacts} artifact${x.artifacts === 1 ? "" : "s"}${x.instructions ? " · has instructions" : ""}</div>
    </button>`).join("")}</div>`;
}

function projectForm(p = {}) {
  return `<label class="field">Name<input type="text" id="pf-name" maxlength="120" value="${esc(p.name || "")}" placeholder="e.g. Thesis, Roblox game, Job applications"></label>
    <label class="field">Description <span class="muted small">(optional)</span><input type="text" id="pf-desc" maxlength="2000" value="${esc(p.description || "")}" placeholder="What is this project about?"></label>`;
}

function submitOnce(btn, fn) {
  // Prevents double submissions: the button stays disabled until the request ends.
  return async () => {
    if (btn.disabled) return;
    btn.disabled = true;
    try { await fn(); } catch (e) { toast(e.message, true); } finally { btn.disabled = false; }
  };
}

function openCreateProject() {
  openModal(`<h2>New project</h2>${projectForm()}
    <label class="field">Instructions <span class="muted small">(optional, used in every chat of this project)</span>
      <textarea id="pf-instr" rows="4" maxlength="8000" placeholder="e.g. Answer in Swedish. I use Python 3.12 and FastAPI. Keep answers short."></textarea></label>
    <div class="row" style="justify-content:flex-end"><button class="btn ghost" data-action="close-modal">Cancel</button>
    <button class="btn primary" id="pf-ok">Create project</button></div>`);
  const ok = $("pf-ok");
  ok.onclick = submitOnce(ok, async () => {
    const name = $("pf-name").value.trim();
    if (!name) { toast("Give the project a name.", true); $("pf-name").focus(); return; }
    const d = await api("POST", "/api/projects", { name, description: $("pf-desc").value, instructions: $("pf-instr").value });
    closeModal();
    toast("Project created.");
    await loadProjects();
    await showProject(d.project.id);
  });
  $("pf-name").focus();
}

// ---------------------------------------------------------------- one project

async function showProject(id) {
  setView("project");
  S.page = { loading: true, id };
  renderPage();
  try {
    const [p, chats, files, arts] = await Promise.all([
      api("GET", `/api/projects/${id}`),
      api("GET", `/api/tasks?project_id=${id}&limit=200`),
      api("GET", `/api/projects/${id}/files`),
      api("GET", `/api/artifacts?project_id=${id}`),
    ]);
    S.page = { id, project: p.project, chats: chats.tasks, files: files.files, exts: files.extensions,
               limit: files.limit_bytes, artifacts: arts.artifacts, instr: p.project.instructions || "" };
  } catch (e) { S.page = { id, error: e.message }; }
  if (S.view === "project" && S.page.id === id) renderPage();
}

function projectPageHtml() {
  const pg = S.page;
  const back = `<button class="link-btn" data-action="nav-projects">← Projects</button>`;
  if (pg.loading) return back + loadingHtml();
  if (pg.error) return back + errorHtml(pg.error, "nav-projects");
  const p = pg.project;
  const space = isSpace(p);
  const dirty = pg.instr !== (p.instructions || "");
  return `${back}
    <div class="page-head"><div><h1>${esc(p.name)}</h1>
      <p class="page-sub">${esc(p.description || "No description.")} ${space ? "" : `<span class="muted">· <code>${esc(p.path)}</code></span>`}</p></div>
      <div class="row"><button class="btn" data-action="project-edit">Edit details</button>
      <button class="btn primary" data-action="project-new-chat">New chat</button></div></div>

    <section class="psec"><div class="psec-head"><h2>Instructions</h2><span class="muted small" id="pi-state">${dirty ? "Unsaved changes" : p.instructions ? "Saved" : ""}</span></div>
      <p class="small muted">Added to every chat in this project, for Qwen and for Claude. Your message in a chat wins if it asks for something different.</p>
      <textarea id="pi-text" rows="5" maxlength="8000" placeholder="e.g. This project uses Python and FastAPI. Follow the existing architecture. Write tests for every significant change.">${esc(pg.instr)}</textarea>
      <div class="row"><span class="small muted" id="pi-count">${pg.instr.length.toLocaleString()} / 8,000</span><span class="grow"></span>
        <button class="btn ghost small" id="pi-cancel" ${dirty ? "" : "disabled"}>Cancel</button>
        <button class="btn primary small" id="pi-save" ${dirty ? "" : "disabled"}>Save instructions</button></div></section>

    <section class="psec"><div class="psec-head"><h2>Chats</h2><span class="muted small">${pg.chats.length}</span></div>
      ${pg.chats.length ? `<div class="plist">${pg.chats.map(c => `<button class="plist-row" data-action="open-chat" data-id="${c.id}" data-project="${p.id}">
        <span class="pl-title">${esc(c.title)}</span><span class="pl-meta">${esc(ago(c.updated))}</span></button>`).join("")}</div>`
        : `<p class="small muted">No chats yet. <button class="link-btn" data-action="project-new-chat">Start one</button></p>`}</section>

    <section class="psec"><div class="psec-head"><h2>Documents</h2><span class="muted small">${pg.files.length}</span><span class="grow"></span>
      <button class="btn small" data-action="project-upload">Add documents</button>
      <input type="file" id="pf-upload" multiple hidden accept="${esc(pg.exts.join(","))}"></div>
      <p class="small muted">Text files only (Markdown, text, CSV, JSON, HTML, code; up to ${kb(pg.limit)} each; PDF and Word are not supported).
        For each question only the matching passages are used. Mark a document <strong>Private</strong> to keep it on this PC: it is used with Qwen but never sent to Claude.</p>
      ${pg.files.length ? `<div class="plist">${pg.files.map(f => `<div class="plist-row static">
        <span class="pl-title">📄 ${esc(f.name)}</span><span class="pl-meta">${kb(f.size)} · ${esc(ago(f.created))}</span>
        <label class="pl-check" title="Private documents never go to Claude"><input type="checkbox" data-action="file-private" data-id="${f.id}" ${f.private ? "checked" : ""}> Private</label>
        <button class="btn ghost small" data-action="file-view" data-id="${f.id}">View</button>
        <button class="btn ghost small" data-action="file-remove" data-id="${f.id}" data-name="${esc(f.name)}">Remove</button></div>`).join("")}</div>`
        : `<p class="small muted">No documents.</p>`}</section>

    <section class="psec"><div class="psec-head"><h2>Artifacts</h2><span class="muted small">${pg.artifacts.length}</span><span class="grow"></span>
      <button class="btn small" data-action="artifact-new" data-project="${p.id}">New artifact</button></div>
      ${pg.artifacts.length ? artifactListHtml(pg.artifacts, false) : `<p class="small muted">No artifacts. Save an answer with <em>Save as artifact</em>, or create one.</p>`}</section>

    <section class="psec danger-zone"><h2>${space ? "Delete project" : "Remove from Enoy"}</h2>
      <p class="small muted">${space
        ? "Its chats move to plain chats, its uploaded documents are deleted, and its artifacts stay in your library without the project link."
        : "The folder and its files on your PC are not touched. Its chats and documents are kept and come back if you open the folder again. Its artifacts stay in your library without the project link."}</p>
      <button class="btn danger small" data-action="project-delete">${space ? "Delete project…" : "Remove from Enoy…"}</button></section>`;
}

function bindProjectPage() {
  const ta = $("pi-text");
  if (!ta) return;
  const sync = () => {
    S.page.instr = ta.value;
    const dirty = ta.value !== (S.page.project.instructions || "");
    $("pi-save").disabled = !dirty; $("pi-cancel").disabled = !dirty;
    $("pi-state").textContent = dirty ? "Unsaved changes" : S.page.project.instructions ? "Saved" : "";
    $("pi-count").textContent = `${ta.value.length.toLocaleString()} / 8,000`;
  };
  ta.addEventListener("input", sync);
  $("pi-cancel").onclick = () => { ta.value = S.page.project.instructions || ""; sync(); };
  const save = $("pi-save");
  save.onclick = submitOnce(save, async () => {
    const d = await api("POST", `/api/projects/${S.page.id}/update`, { instructions: ta.value });
    S.page.project = d.project;
    if (S.project && S.project.id === d.project.id) Object.assign(S.project, d.project);
    sync();
    toast("Instructions saved.");
  });
  $("pf-upload").addEventListener("change", e => { uploadDocs([...e.target.files]); e.target.value = ""; });
}

async function uploadDocs(files) {
  let added = 0;
  for (const f of files) {
    if (f.size > S.page.limit) { toast(`${f.name} is larger than ${kb(S.page.limit)}.`, true); continue; }
    try {
      const data = await readDataUrl(f);
      const d = await api("POST", `/api/projects/${S.page.id}/files`, { name: f.name, data });
      if (d.created) added++; else toast(`${f.name} is already in this project.`);
    } catch (e) { toast(e.message, true); }
  }
  if (added) toast(`${added} document${added > 1 ? "s" : ""} added.`);
  await showProject(S.page.id);
}

async function confirmDeleteProject() {
  const p = S.page.project;
  const space = isSpace(p);
  const n = S.page.chats.length, f = S.page.files.length, a = S.page.artifacts.length;
  const body = space
    ? `<p><strong>${esc(p.name)}</strong> will be deleted.</p><ul>
        <li>${n} chat${n === 1 ? "" : "s"} move to plain chats (nothing is lost).</li>
        <li>${f} uploaded document${f === 1 ? "" : "s"} will be <strong>deleted</strong>.</li>
        <li>${a} artifact${a === 1 ? "" : "s"} stay in your library, without the project.</li>
        <li>The instructions are deleted.</li></ul>`
    : `<p><strong>${esc(p.name)}</strong> will be removed from Enoy.</p><ul>
        <li>The folder <code>${esc(p.path)}</code> and its files are <strong>not</strong> touched.</li>
        <li>${n} chat${n === 1 ? "" : "s"}, ${f} document${f === 1 ? "" : "s"} and the instructions are kept and come back if you open the folder again.</li>
        <li>${a} artifact${a === 1 ? "" : "s"} stay in your library, without the project.</li></ul>`;
  if (!(await confirmBox(space ? "Delete this project?" : "Remove this project?", body, space ? "Delete project" : "Remove", true))) return;
  await guarded(async () => {
    await api("POST", `/api/projects/${p.id}/delete`, { confirm: true });
    toast(space ? "Project deleted." : "Project removed. The folder was not touched.");
    if (S.project && S.project.id === p.id) { await useDefaultProject(); S.chat = null; S.chatId = null; }
    await loadProjects();
    showProjects();
  });
}

// ---------------------------------------------------------------- Artifacts library

async function showArtifacts() {
  setView("artifacts");
  S.page = { loading: true, q: (S.page && S.page.q) || "", filter: (S.page && S.page.filter) || "all" };
  renderPage();
  await loadArtifactList();
}

async function loadArtifactList() {
  const keep = { q: S.page.q || "", filter: S.page.filter || "all" };
  try {
    const [arts, projs] = await Promise.all([
      api("GET", `/api/artifacts?q=${encodeURIComponent(keep.q)}`),
      api("GET", "/api/projects?all=1"),
    ]);
    S.page = { ...keep, artifacts: arts.artifacts, projects: projs.projects.filter(p => !p.is_default) };
  } catch (e) { S.page = { ...keep, error: e.message }; }
  if (S.view === "artifacts") {
    // Keep typing focus in the search box across the re-render.
    const focused = document.activeElement && document.activeElement.id === "art-search";
    renderPage();
    if (focused) { const el = $("art-search"); el.focus(); el.setSelectionRange(el.value.length, el.value.length); }
  }
}

function artifactListHtml(list, showProject = true) {
  return `<div class="plist">${list.map(a => `<button class="plist-row ${S.art && S.art.data.id === a.id ? "active" : ""}" data-action="artifact-open" data-id="${a.id}">
    <span class="art-icon">${esc(ART_ICON[a.type] || "≡")}</span>
    <span class="pl-title">${esc(a.title)}</span>
    <span class="pl-tag">${esc(ART_TYPES[a.type] || a.type)}${a.type === "code" && a.language ? ` · ${esc(a.language)}` : ""}</span>
    ${showProject ? `<span class="pl-tag ${a.project_name ? "" : "muted"}">${a.project_name ? esc(a.project_name) : "Standalone"}</span>` : ""}
    <span class="pl-meta">${esc(ago(a.updated))}</span></button>`).join("")}</div>`;
}

function artifactsPageHtml() {
  const pg = S.page;
  const head = `<div class="page-head"><h1>Artifacts</h1><button class="btn primary" data-action="artifact-new">New artifact</button></div>
    <p class="page-sub">Documents, code and other work you saved. Each one keeps its own version history and stays here even if the chat it came from is gone.</p>`;
  if (pg.loading) return head + loadingHtml();
  if (pg.error) return head + errorHtml(pg.error, "nav-artifacts");
  const opts = [["all", "All artifacts"], ["none", "Standalone"], ...pg.projects.map(p => [String(p.id), p.name])];
  let list = pg.artifacts;
  if (pg.filter === "none") list = list.filter(a => !a.project_id);
  else if (pg.filter !== "all") list = list.filter(a => String(a.project_id) === pg.filter);
  const tools = `<div class="row page-tools"><input type="search" id="art-search" placeholder="Search titles and content" value="${esc(pg.q)}" aria-label="Search artifacts">
    <select id="art-filter" aria-label="Filter by project">${opts.map(([v, l]) => `<option value="${esc(v)}" ${v === pg.filter ? "selected" : ""}>${esc(l)}</option>`).join("")}</select></div>`;
  if (!pg.artifacts.length && !pg.q) {
    return head + `<div class="empty-state"><div class="es-icon">◇</div><h2>No artifacts yet</h2>
      <p>Under any answer, click <em>Save as artifact</em> to keep it, or save a single code block with its <em>Save</em> button. You can also start one from scratch.</p>
      <button class="btn primary" data-action="artifact-new">New artifact</button></div>`;
  }
  return head + tools + (list.length ? artifactListHtml(list) : `<p class="small muted">Nothing matches.</p>`);
}

let artSearchTimer = null;
function bindArtifactsPage() {
  const s = $("art-search");
  if (s) s.addEventListener("input", () => {
    clearTimeout(artSearchTimer);
    artSearchTimer = setTimeout(() => { S.page.q = s.value.trim(); loadArtifactList(); }, 250);
  });
  const f = $("art-filter");
  if (f) f.addEventListener("change", () => { S.page.filter = f.value; renderPage(); });
}

function openNewArtifact(projectId) {
  const projects = (S.projects || []).filter(p => !p.is_default);
  openModal(`<h2>New artifact</h2>
    <label class="field">Title<input type="text" id="na-title" maxlength="200" placeholder="e.g. Project plan"></label>
    <label class="field">Type<select id="na-type">${Object.entries(ART_TYPES).map(([k, l]) => `<option value="${k}">${l}</option>`).join("")}</select></label>
    <label class="field">Project<select id="na-project"><option value="">No project (standalone)</option>
      ${projects.map(p => `<option value="${p.id}" ${String(p.id) === String(projectId || "") ? "selected" : ""}>${esc(p.name)}</option>`).join("")}</select></label>
    <div class="row" style="justify-content:flex-end"><button class="btn ghost" data-action="close-modal">Cancel</button><button class="btn primary" id="na-ok">Create</button></div>`);
  const ok = $("na-ok");
  ok.onclick = submitOnce(ok, async () => {
    const title = $("na-title").value.trim();
    if (!title) { toast("Give the artifact a title.", true); $("na-title").focus(); return; }
    const d = await api("POST", "/api/artifacts", { title, type: $("na-type").value, content: "", project_id: Number($("na-project").value) || null });
    closeModal();
    await openArtifact(d.artifact.id, "edit");
    refreshArtifactViews();
  });
  $("na-title").focus();
}

function refreshArtifactViews() {
  if (S.view === "artifacts") loadArtifactList();
  else if (S.view === "project") showProject(S.page.id);
}

// ---------------------------------------------------------------- artifact panel

function artDirty() {
  if (!S.art) return false;
  const a = S.art.data, d = S.art.draft;
  return a.title !== d.title || a.content !== d.content || a.type !== d.type || (a.language || "") !== (d.language || "");
}

async function closeArtifactPanel(force = false) {
  if (!force && artDirty() && !(await confirmBox("Discard unsaved changes?", "<p>Your edits to this artifact have not been saved.</p>", "Discard", true))) return false;
  S.art = null;
  $("artifact").classList.add("hidden");
  $("artifact").innerHTML = "";
  $("app").classList.remove("art-open");
  return true;
}

async function openArtifact(id, tab = "preview") {
  if (S.art && S.art.data.id !== id && !(await closeArtifactPanel())) return;
  try {
    const a = (await api("GET", `/api/artifacts/${id}`)).artifact;
    S.art = { data: a, draft: { title: a.title, content: a.content, type: a.type, language: a.language || "" }, tab };
  } catch (e) { toast(e.message, true); return; }
  // The Activity panel and the artifact panel share the right side.
  if (typeof AC !== "undefined" && AC.open) setOpen(false);
  $("artifact").classList.remove("hidden");
  $("app").classList.add("art-open");
  renderArtifactPanel();
  if (S.view === "artifacts" || S.view === "project") renderPage();
}

function svgDataUrl(svg) {
  const bytes = new TextEncoder().encode(svg);
  let bin = "";
  bytes.forEach(b => { bin += String.fromCharCode(b); });
  return "data:image/svg+xml;base64," + btoa(bin);
}

function artPreviewHtml(d) {
  if (d.type === "markdown") return `<div class="ai-body art-md">${md(d.content) || `<p class="muted">Empty. Switch to Edit to write.</p>`}</div>`;
  if (d.type === "html") {
    // sandbox="" = no scripts, forms, popups or same-origin access. The page's own CSP also applies.
    return `<div class="art-note">Static preview: scripts and forms are switched off. Export the file to run it in a browser.</div>
      <iframe class="art-frame" sandbox="" referrerpolicy="no-referrer" title="HTML preview" srcdoc="${esc(d.content)}"></iframe>`;
  }
  if (d.type === "svg") {
    return `<div class="art-note">Shown as an image: scripts inside an SVG do not run here.</div>
      <div class="art-svg"><img src="${svgDataUrl(d.content)}" alt="SVG preview"></div>`;
  }
  return `<pre class="art-code"><code>${esc(d.content) || `<span class="muted">Empty.</span>`}</code></pre>`;
}

function renderArtifactPanel() {
  const st = S.art;
  if (!st) return;
  const a = st.data, d = st.draft;
  const dirty = artDirty();
  $("artifact").innerHTML = `
    <div class="art-head">
      <input class="art-title" id="art-title" value="${esc(d.title)}" maxlength="200" aria-label="Artifact title">
      <button class="icon-btn" data-action="artifact-menu" title="More" aria-label="More actions">⋯</button>
      <button class="icon-btn" data-action="artifact-close" title="Close" aria-label="Close artifact">✕</button>
    </div>
    <div class="art-bar">
      <div class="seg" role="tablist" aria-label="View">
        <button role="tab" class="${st.tab === "preview" ? "on" : ""}" aria-selected="${st.tab === "preview"}" data-action="artifact-tab" data-tab="preview">Preview</button>
        <button role="tab" class="${st.tab === "edit" ? "on" : ""}" aria-selected="${st.tab === "edit"}" data-action="artifact-tab" data-tab="edit">Edit</button>
      </div>
      <select id="art-type" aria-label="Type">${Object.entries(ART_TYPES).map(([k, l]) => `<option value="${k}" ${k === d.type ? "selected" : ""}>${l}</option>`).join("")}</select>
      ${d.type === "code" ? `<input id="art-lang" class="art-lang" value="${esc(d.language)}" placeholder="language" aria-label="Language" maxlength="30">` : ""}
      <span class="grow"></span>
      <button class="btn ghost small" data-action="artifact-copy" title="Copy the content">Copy</button>
    </div>
    <div class="art-body" id="art-body">${st.tab === "edit"
      ? `<textarea id="art-src" class="art-src" spellcheck="${d.type === "markdown" || d.type === "text"}" aria-label="Source">${esc(d.content)}</textarea>`
      : artPreviewHtml(d)}</div>
    <div class="art-foot">
      <span class="small muted" id="art-status">${dirty ? `<span class="unsaved">● Unsaved changes</span>` : `Version ${a.version} · saved ${esc(ago(a.updated))}`}${a.project_name ? ` · ${esc(a.project_name)}` : ""}</span>
      <span class="grow"></span>
      <button class="btn ghost small" data-action="artifact-revert" ${dirty ? "" : "disabled"}>Cancel</button>
      <button class="btn primary small" data-action="artifact-save" id="art-save" ${dirty && !st.saving ? "" : "disabled"}>${st.saving ? "Saving…" : "Save"}</button>
    </div>`;
  const status = () => {
    const dd = artDirty();
    $("art-status").innerHTML = dd ? `<span class="unsaved">● Unsaved changes</span>` : `Version ${a.version} · saved ${esc(ago(a.updated))}`;
    $("art-save").disabled = !dd || st.saving;
    document.querySelector('[data-action="artifact-revert"]').disabled = !dd;
  };
  $("art-title").addEventListener("input", e => { d.title = e.target.value; status(); });
  $("art-type").addEventListener("change", e => { d.type = e.target.value; renderArtifactPanel(); });
  const lang = $("art-lang");
  if (lang) lang.addEventListener("input", e => { d.language = e.target.value; status(); });
  const src = $("art-src");
  if (src) {
    src.addEventListener("input", () => { d.content = src.value; status(); });
    src.addEventListener("keydown", e => {
      if (e.key === "Tab" && !e.shiftKey) { e.preventDefault(); document.execCommand("insertText", false, "  "); }
    });
  }
}

async function saveArtifact(force = false) {
  const st = S.art;
  if (!st || st.saving || (!artDirty() && !force)) return;
  if (!st.draft.title.trim()) { toast("Give the artifact a title.", true); return; }
  st.saving = true;
  renderArtifactPanel();
  const sent = { ...st.draft };
  try {
    const d = await api("POST", `/api/artifacts/${st.data.id}/update`, { base_version: st.data.version, ...sent });
    if (S.art !== st) return;
    st.data = d.artifact;
    // Keep anything typed while the save was in flight.
    st.saving = false;
    renderArtifactPanel();
    toast("Saved.");
    refreshArtifactViews();
  } catch (e) {
    st.saving = false;
    renderArtifactPanel();
    if (/changed elsewhere/.test(e.message)) return artifactConflict();
    toast(`Not saved: ${e.message}`, true);
  }
}

async function artifactConflict() {
  const st = S.art;
  openModal(`<h2>Changed elsewhere</h2><p>This artifact was saved from another place since you opened it. Nothing was overwritten.</p>
    <div class="row" style="justify-content:flex-end"><button class="btn ghost" id="cf-load">Load the newer version (drop my edits)</button>
    <button class="btn primary" id="cf-mine">Save mine as a new version</button></div>`);
  $("cf-load").onclick = async () => { closeModal(); const id = st.data.id; S.art = null; await openArtifact(id, st.tab); };
  $("cf-mine").onclick = async () => {
    closeModal();
    try {
      const latest = (await api("GET", `/api/artifacts/${st.data.id}`)).artifact;
      st.data = latest;  // Base the save on the newest version; the older one stays in the history.
      await saveArtifact(true);
    } catch (e) { toast(e.message, true); }
  };
}

async function exportArtifact() {
  const st = S.art;
  if (artDirty() && !(await confirmBox("Export the saved version?", "<p>Unsaved edits are not included. Save first to export them.</p>", "Export saved version"))) return;
  try {
    const d = await api("POST", `/api/artifacts/${st.data.id}/export`, {});
    if (d.saved) toast(`Saved to ${d.saved}`);
    else if (d.content !== undefined) {
      const url = URL.createObjectURL(new Blob([d.content], { type: d.mime }));
      const link = document.createElement("a");
      link.href = url; link.download = d.filename;
      document.body.appendChild(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 2000);
    }
  } catch (e) { toast(`Export failed: ${e.message}`, true); }
}

async function showArtifactHistory() {
  const st = S.art;
  let versions;
  try { versions = (await api("GET", `/api/artifacts/${st.data.id}/versions`)).versions; } catch (e) { toast(e.message, true); return; }
  openModal(`<h2>Version history</h2><p class="small muted">Restoring an older version adds it as a new version, so nothing later is lost.</p>
    <div class="plist">${versions.map(v => `<div class="plist-row static">
      <span class="pl-title">Version ${v.version}${v.version === st.data.version ? " (current)" : ""}</span>
      <span class="pl-meta">${esc(v.note)} · ${v.chars.toLocaleString()} chars · ${new Date(v.created * 1000).toLocaleString()}</span>
      <button class="btn ghost small" data-action="artifact-version-view" data-v="${v.version}">View</button>
      ${v.version !== st.data.version ? `<button class="btn small" data-action="artifact-version-restore" data-v="${v.version}">Restore</button>` : ""}</div>`).join("")}</div>
    <div class="row" style="justify-content:flex-end"><button class="btn primary" data-action="close-modal">Close</button></div>`, true);
}

Object.assign(actions, {
  "nav-projects": () => { closeSidebarIfNarrow(); showProjects(); },
  "nav-artifacts": () => { closeSidebarIfNarrow(); showArtifacts(); },
  "project-create": () => openCreateProject(),
  "open-project-page": el => showProject(Number(el.dataset.id)),
  "project-page": () => { closeMenu(); if (S.project) showProject(S.project.id); },
  "project-edit": () => {
    const p = S.page.project;
    openModal(`<h2>Edit project</h2>${projectForm(p)}
      <div class="row" style="justify-content:flex-end"><button class="btn ghost" data-action="close-modal">Cancel</button><button class="btn primary" id="pf-ok">Save</button></div>`);
    const ok = $("pf-ok");
    ok.onclick = submitOnce(ok, async () => {
      const name = $("pf-name").value.trim();
      if (!name) { toast("The name cannot be empty.", true); return; }
      const d = await api("POST", `/api/projects/${p.id}/update`, { name, description: $("pf-desc").value });
      closeModal();
      S.page.project = d.project;
      if (S.project && S.project.id === p.id) Object.assign(S.project, d.project);
      await loadProjects();
      renderSidebar();
      renderPage();
      toast("Project updated.");
    });
  },
  "project-new-chat": () => guarded(async () => {
    const id = S.page.project.id;
    if (!S.project || S.project.id !== id) await useProject(id);
    setView("chat");
    actions["new-chat"]();
  }),
  "project-upload": () => { const i = $("pf-upload"); if (i) i.click(); },
  "project-delete": () => confirmDeleteProject(),
  "file-private": el => guarded(async () => {
    await api("POST", `/api/projects/${S.page.id}/files/${el.dataset.id}/private`, { on: el.checked });
    const f = S.page.files.find(x => x.id === Number(el.dataset.id));
    if (f) f.private = el.checked ? 1 : 0;
    toast(el.checked ? "Private: this document never goes to Claude." : "This document may be used with Claude (relevant passages only, after your OK).");
  }),
  "file-view": el => guarded(async () => {
    const f = (await api("GET", `/api/projects/${S.page.id}/files/${el.dataset.id}`)).file;
    openModal(`<h2>${esc(f.name)}</h2><div class="small muted">${kb(f.size)} · added ${new Date(f.created * 1000).toLocaleString()}${f.private ? " · private" : ""}</div>
      <pre class="log">${esc(f.content)}</pre><div class="row" style="justify-content:flex-end"><button class="btn primary" data-action="close-modal">Close</button></div>`, true);
  }),
  "file-remove": async el => {
    if (!(await confirmBox("Remove this document?", `<p><strong>${esc(el.dataset.name)}</strong> is removed from this project and deleted from Enoy. The original file on your PC is not touched.</p>`, "Remove", true))) return;
    await guarded(async () => { await api("POST", `/api/projects/${S.page.id}/files/${el.dataset.id}/delete`); await showProject(S.page.id); });
  },
  "artifact-new": el => openNewArtifact(el.dataset.project || (S.view === "project" ? S.page.id : "")),
  "artifact-open": el => openArtifact(Number(el.dataset.id)),
  "artifact-close": () => closeArtifactPanel(),
  "artifact-tab": el => { if (S.art) { S.art.tab = el.dataset.tab; renderArtifactPanel(); if (S.art.tab === "edit") $("art-src").focus(); } },
  "artifact-save": () => saveArtifact(),
  "artifact-revert": () => { const a = S.art.data; S.art.draft = { title: a.title, content: a.content, type: a.type, language: a.language || "" }; renderArtifactPanel(); },
  "artifact-copy": () => copy(S.art.draft.content),
  "artifact-menu": el => openMenu(el, [
    { title: "Export to a file…", desc: "Saves the last saved version", action: "artifact-export" },
    { title: "Version history", action: "artifact-history" },
    { title: "Project…", desc: S.art.data.project_name ? `In ${S.art.data.project_name}` : "Standalone", action: "artifact-project" },
    ...(S.art.data.task_id ? [{ title: "Open the chat it came from", action: "artifact-source" }] : []),
    "-",
    { title: "Delete…", action: "artifact-delete" },
  ], "right"),
  "artifact-export": () => { closeMenu(); exportArtifact(); },
  "artifact-history": () => { closeMenu(); showArtifactHistory(); },
  "artifact-version-view": el => guarded(async () => {
    const v = (await api("GET", `/api/artifacts/${S.art.data.id}/versions/${el.dataset.v}`)).version;
    openModal(`<h2>Version ${v.version}: ${esc(v.title)}</h2><div class="small muted">${esc(v.note)} · ${new Date(v.created * 1000).toLocaleString()}</div>
      <pre class="log">${esc(v.content)}</pre><div class="row" style="justify-content:flex-end"><button class="btn" data-action="artifact-history">Back</button>
      <button class="btn primary" data-action="close-modal">Close</button></div>`, true);
  }),
  "artifact-version-restore": async el => {
    if (artDirty() && !(await confirmBox("Discard unsaved changes?", "<p>Restoring replaces your unsaved edits.</p>", "Discard and restore", true))) return;
    await guarded(async () => {
      const d = await api("POST", `/api/artifacts/${S.art.data.id}/restore`, { version: Number(el.dataset.v), base_version: S.art.data.version });
      closeModal();
      S.art.data = d.artifact;
      S.art.draft = { title: d.artifact.title, content: d.artifact.content, type: d.artifact.type, language: d.artifact.language || "" };
      renderArtifactPanel();
      refreshArtifactViews();
      toast(`Restored as version ${d.artifact.version}.`);
    });
  },
  "artifact-project": () => {
    closeMenu();
    const projects = (S.projects || []).filter(p => !p.is_default);
    openModal(`<h2>Project</h2><p class="small muted">Removing the project link keeps the artifact in your library.</p>
      <label class="field">Belongs to<select id="ap-select"><option value="">No project (standalone)</option>
      ${projects.map(p => `<option value="${p.id}" ${p.id === S.art.data.project_id ? "selected" : ""}>${esc(p.name)}</option>`).join("")}</select></label>
      <div class="row" style="justify-content:flex-end"><button class="btn ghost" data-action="close-modal">Cancel</button><button class="btn primary" id="ap-ok">Save</button></div>`);
    const ok = $("ap-ok");
    ok.onclick = submitOnce(ok, async () => {
      const d = await api("POST", `/api/artifacts/${S.art.data.id}/project`, { project_id: Number($("ap-select").value) || null });
      closeModal();
      S.art.data.project_id = d.artifact.project_id; S.art.data.project_name = d.artifact.project_name;
      renderArtifactPanel();
      refreshArtifactViews();
    });
  },
  "artifact-source": () => guarded(async () => {
    closeMenu();
    const tid = S.art.data.task_id;
    try { await loadChat(tid); } catch (e) { toast("The chat this came from no longer exists. The artifact is kept.", true); return; }
    if (!S.project || S.project.id !== S.chat.task.project_id) { const id = tid; await useProject(S.chat.task.project_id); await loadChat(id); }
    setView("chat");
    renderAll(true);
  }),
  "artifact-delete": async () => {
    closeMenu();
    const a = S.art.data;
    if (!(await confirmBox("Delete this artifact?", `<p><strong>${esc(a.title)}</strong> and all ${a.version} version${a.version > 1 ? "s" : ""} are deleted permanently. Chats are not affected.</p>`, "Delete", true))) return;
    await guarded(async () => {
      await api("POST", `/api/artifacts/${a.id}/delete`, { confirm: true });
      await closeArtifactPanel(true);
      toast("Artifact deleted.");
      refreshArtifactViews();
    });
  },
  "save-artifact": el => guarded(async () => {
    const d = await api("POST", "/api/artifacts/from-message", { message_id: Number(el.dataset.id) });
    toast(d.created ? "Saved as an artifact." : "Already saved. Opened it.");
    await openArtifact(d.artifact.id);
  }),
  "save-code-artifact": el => guarded(async () => {
    const msg = el.closest("[data-msg-id]");
    if (!msg) return;
    const blocks = [...msg.querySelectorAll(".codeblock")];
    const d = await api("POST", "/api/artifacts/from-message", { message_id: Number(msg.dataset.msgId), block: blocks.indexOf(el.closest(".codeblock")) });
    toast(d.created ? "Code saved as an artifact." : "Already saved. Opened it.");
    await openArtifact(d.artifact.id);
  }),
  "move-chat": () => {
    closeMenu();
    const t = S.chat.task;
    const here = S.projects.find(p => p.id === t.project_id) || S.project;
    if (folderProject(here)) {
      openModal(`<h2>Move chat</h2><p>Chats about a code folder stay with that folder, because their changes belong to its files.</p>
        <div class="row" style="justify-content:flex-end"><button class="btn primary" data-action="close-modal">OK</button></div>`);
      return;
    }
    const targets = S.projects.filter(p => p.is_default || isSpace(p));
    openModal(`<h2>Move chat to…</h2><p class="small muted">The chat keeps its full history. Project instructions and documents of the new project apply from the next message.</p>
      <label class="field">Project<select id="mv-select">${targets.map(p => `<option value="${p.id}" ${p.id === t.project_id ? "selected" : ""}>${p.is_default ? "No project (plain chats)" : esc(p.name)}</option>`).join("")}</select></label>
      <div class="row" style="justify-content:flex-end"><button class="btn ghost" data-action="close-modal">Cancel</button><button class="btn primary" id="mv-ok">Move</button></div>`);
    const ok = $("mv-ok");
    ok.onclick = submitOnce(ok, async () => {
      const pid = Number($("mv-select").value);
      if (pid === t.project_id) { closeModal(); return; }
      await api("POST", `/api/tasks/${t.id}/move`, { project_id: pid });
      closeModal();
      await useProject(pid);
      await loadChat(t.id);
      renderAll();
      toast("Chat moved.");
    });
  },
});

// Leaving a page for a chat: wrap the chat actions so they switch the view back.
for (const name of ["open-chat", "new-chat", "suggest"]) {
  const base = actions[name];
  actions[name] = async (el, e) => {
    if (name === "open-chat" && el && el.dataset.project && (!S.project || S.project.id !== Number(el.dataset.project))) {
      await guarded(() => useProject(Number(el.dataset.project)));
    }
    setView("chat");
    return base(el, e);
  };
}
const baseToggleActivity = actions["toggle-activity"];
actions["toggle-activity"] = async (el, e) => {
  if (S.art && !(typeof AC !== "undefined" && AC.open) && !(await closeArtifactPanel())) return;
  return baseToggleActivity(el, e);
};
const baseMode = actions["mode"];
actions["mode"] = (el, e) => { setView("chat"); return baseMode(el, e); };

document.addEventListener("keydown", e => {
  if (S.art && (e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s" && document.activeElement && document.activeElement.closest("#artifact")) {
    e.preventDefault();
    saveArtifact();
  }
});
window.addEventListener("beforeunload", e => { if (artDirty()) { e.preventDefault(); e.returnValue = ""; } });
