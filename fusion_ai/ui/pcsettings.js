"use strict";
// Settings → Memory & PC: memory mode with live measurements, PC access profile,
// approved programs and the hardware inventory. Values shown here are measured by
// the backend; nothing is estimated in the page.

let memTimer = null;

async function settingsMemoryPc() {
  clearInterval(memTimer);
  const body = $("set-body");
  let mem, pcs;
  try {
    [mem, pcs] = await Promise.all([api("GET", "/api/memory?refresh=1"), api("GET", "/api/pc")]);
  } catch (e) { body.innerHTML = `<div class="page-state bad">Could not load: ${esc(e.message)}</div>`; return; }
  if (S.settingsTab !== "pc" || !$("set-body")) return;
  body.innerHTML = `
    <div class="set-section"><h3>Memory mode for Qwen</h3>
      <div class="small muted">A ceiling for the RAM the local model may use, so Windows stays responsive. These are limits, not targets.</div>
      <div class="mode-cards">${Object.entries(mem.modes).map(([k, m]) => `
        <button class="mode-card ${mem.mode === k ? "on" : ""}" data-action="mem-mode" data-mode="${k}" aria-pressed="${mem.mode === k}">
          <span class="mc-name">${esc(m.label)}</span><span class="mc-mib">${m.ceiling_mib.toLocaleString()} MiB</span></button>`).join("")}</div>
      <div id="mem-live">${memLiveHtml(mem)}</div>
    </div>
    <div class="set-section"><h3>PC access</h3>
      <div class="small muted">What Enoy may do on this PC when you ask in a chat (for example "open youtube.com", "minimize Spotify", "what GPU do I have?").
        Checked by the app before every action and logged in Activity. A model's own text never grants access.</div>
      <div class="mode-cards">${Object.entries(pcs.profiles).map(([k, p]) => `
        <button class="mode-card ${pcs.profile === k ? "on" : ""}" data-action="pc-profile" data-profile="${k}" aria-pressed="${pcs.profile === k}">
          <span class="mc-name">${esc(p.label)}</span><span class="mc-desc">${esc(p.desc)}</span></button>`).join("")}</div>
      <table class="cap-table"><tr><th>Capability</th><th>Status</th></tr>${pcs.capabilities.map(c => `
        <tr><td><strong>${esc(c.label)}</strong><div class="small muted">${esc(c.desc)}</div></td>
        <td>${!c.implemented ? `<span class="muted">Not available</span>` : c.allowed ? `<span class="badge-ok">Allowed</span>` : `<span class="muted">Off</span>`}</td></tr>`).join("")}</table>
      ${toggleRow("pc", "hardware_read", "Read hardware inventory", "Read-only. Lets Enoy answer questions about your CPU, RAM, GPU and disks from Windows itself.")}
      <div class="row"><button class="btn small" data-action="pc-revoke">Revoke to Observe</button>
        <button class="btn small" data-action="pc-hardware">Show hardware inventory</button></div>
    </div>
    <div class="set-section"><h3>Approved programs</h3>
      <div class="small muted">Only these can be started by asking ("open notepad"). One per line as <code>Name = full path to .exe</code>.</div>
      <textarea id="pc-apps" rows="4" placeholder="Notepad = C:\\Windows\\notepad.exe">${esc(pcs.approved_apps.map(a => `${a.name} = ${a.path}`).join("\n"))}</textarea>
      <div class="row"><button class="btn small primary" data-action="pc-apps-save">Save programs</button></div>
    </div>`;
  bindCfgInputs();
  memTimer = setInterval(async () => {
    if (S.settingsTab !== "pc" || !$("mem-live") || $("modal").classList.contains("hidden")) { clearInterval(memTimer); return; }
    try { $("mem-live").innerHTML = memLiveHtml(await api("GET", "/api/memory")); } catch (e) { /* keep last */ }
  }, 2000);
}

const MEM_STATE = {
  idle: ["No model loaded", ""], within: ["Within limit", "ok"], approaching: ["Approaching limit", "warn"],
  exceeded: ["Limit exceeded", "bad"], unloading: ["Unloading model", "warn"], recovering: ["Recovery in progress", "warn"],
  measurement_failed: ["Measurement failed", "bad"],
};

function memLiveHtml(m) {
  if (!m.available) return `<div class="small muted" style="margin-top:8px">${esc(m.detail || "Not running.")}</div>`;
  const blocked = m.blocked && ["idle", "within"].includes(m.state) && !m.active;
  const [label, cls] = blocked ? ["Model loading blocked", "bad"] : MEM_STATE[m.state] || [m.state, ""];
  const pct = Math.min(100, (m.total_mib / m.ceiling_mib) * 100);
  const procs = m.processes.map(p => `${esc(p.name)} (pid ${p.pid}, ${p.role}): ${p.private_ws_mib == null ? "access denied" : `${p.private_ws_mib.toLocaleString()} MiB`}`).join("<br>");
  const profiles = Object.entries(m.profiles || {}).map(([k, v]) => { const [model, ctx] = k.split("|"); return `${esc(model)} at ${Number(ctx).toLocaleString()} tokens: ${v.toLocaleString()} MiB`; });
  return `<div class="mem-live">
    <div class="row"><span class="dot ${cls}"></span><strong>${esc(label)}</strong>
      ${m.hard_cap ? "" : `<span class="pill small" title="${esc(m.enforcement)}">Hard cap unavailable</span>`}<span class="grow"></span>
      <span class="small">${m.total_mib.toLocaleString()} / ${m.ceiling_mib.toLocaleString()} MiB · peak ${m.peak_mib.toLocaleString()} MiB</span></div>
    <div class="mem-bar"><span style="width:${pct.toFixed(1)}%" class="${cls}"></span><i style="left:${(m.warn_mib / m.ceiling_mib * 100).toFixed(1)}%" title="Warning threshold"></i></div>
    <div class="small muted">Measured: ${esc(m.metric)}. GPU memory (VRAM) is not counted.</div>
    <div class="small" style="margin-top:6px">${procs || "No Ollama model process is running."}</div>
    ${m.active ? `<div class="small">Working: ${esc(m.active.model)}, ${Number(m.active.ctx).toLocaleString()}-token context.</div>` : ""}
    ${m.last_action ? `<div class="small">Last action: ${esc(m.last_action.text)} (${new Date(m.last_action.at * 1000).toLocaleTimeString()})</div>` : ""}
    ${profiles.length ? `<details class="small"><summary>Measured model sizes</summary>${profiles.join("<br>")}</details>` : ""}
    ${blocked ? `<div class="small">The last request for ${esc(m.blocked.model)} was refused before loading (${new Date(m.blocked.at * 1000).toLocaleTimeString()}): it would not fit in this mode.</div>` : ""}
    <div class="small muted" style="margin-top:6px"><strong>How it is enforced:</strong> ${esc(m.enforcement)}</div>
    <div class="small muted">${esc(m.shared_note || "")}${m.keep_alive_s ? ` In this mode the model is unloaded ${m.keep_alive_s >= 60 ? `${m.keep_alive_s / 60} minute${m.keep_alive_s === 60 ? "" : "s"}` : `${m.keep_alive_s} seconds`} after an answer to give the memory back.` : ""}</div></div>`;
}

Object.assign(actions, {
  "mem-mode": el => guarded(async () => {
    await api("POST", "/api/memory/mode", { mode: el.dataset.mode });
    toast("Memory mode changed.");
    settingsMemoryPc();
  }),
  "pc-profile": async el => {
    const order = ["observe", "assist", "automation"];
    const cur = (await api("GET", "/api/pc")).profile;
    const up = order.indexOf(el.dataset.profile) > order.indexOf(cur);
    if (up && !(await confirmBox("Give Enoy more access?", `<p>Enoy will be allowed to open web pages, start your approved programs and switch windows when you ask in a chat. It never types, clicks, logs in or changes settings. You can revoke this at any time.</p>`, "Allow"))) { settingsMemoryPc(); return; }
    await guarded(async () => {
      await api("POST", "/api/pc/profile", { profile: el.dataset.profile, confirm: up });
      toast("PC access changed.");
      S.settingsTab = "pc"; renderSettings();
    });
  },
  "pc-revoke": () => guarded(async () => {
    await api("POST", "/api/pc/profile", { profile: "observe" });
    toast("PC access set to Observe (read-only).");
    S.settingsTab = "pc"; renderSettings();
  }),
  "pc-apps-save": () => guarded(async () => {
    const apps = $("pc-apps").value.split("\n").map(l => l.trim()).filter(Boolean).map(l => {
      const i = l.indexOf("=");
      return i < 0 ? { name: "", path: l } : { name: l.slice(0, i).trim(), path: l.slice(i + 1).trim() };
    });
    await api("POST", "/api/pc/apps", { apps });
    toast("Approved programs saved.");
  }),
  "pc-hardware": () => guarded(async () => {
    const inv = (await api("GET", "/api/hardware?refresh=1")).inventory;
    const gpu = inv.gpus.map(g => `${esc(g.name)}${g.vram_total_mb ? ` · ${g.vram_total_mb.toLocaleString()} MB VRAM (${g.vram_used_mb.toLocaleString()} MB in use)` : ""}`).join("<br>") || "Not reported";
    const rt = inv.runtime || {};
    openModal(`<h2>Hardware inventory</h2><p class="small muted">Read from Windows just now. Read-only.</p>
      <table class="cap-table">
        <tr><td>CPU</td><td>${esc(inv.cpu.name || "unknown")} · ${inv.cpu.logical_processors} logical processors</td></tr>
        <tr><td>RAM</td><td>${inv.memory.total_gb} GB total · ${inv.memory.free_gb} GB free</td></tr>
        <tr><td>GPU</td><td>${gpu}</td></tr>
        <tr><td>Disks</td><td>${inv.disks.map(d => `${esc(d.drive)} ${d.free_gb} GB free of ${d.total_gb} GB`).join("<br>")}</td></tr>
        <tr><td>Windows</td><td>${esc([inv.windows.name, inv.windows.displayVersion, inv.windows.editionID, inv.windows.architecture].filter(Boolean).join(" · "))}</td></tr>
        <tr><td>Local model</td><td>${rt.ollama_version ? `Ollama ${esc(rt.ollama_version)} · ${esc(rt.configured_model || "")}<br>${(rt.loaded_models || []).map(m => `${esc(m.name)} loaded: ${m.size_mib.toLocaleString()} MiB (${m.vram_mib.toLocaleString()} MiB on GPU), context ${m.context_length}`).join("<br>") || "No model loaded"}` : esc(rt.error || "")}</td></tr>
      </table>
      <div class="row" style="justify-content:flex-end"><button class="btn" data-action="open-settings" data-tab="pc">Back</button><button class="btn primary" data-action="close-modal">Close</button></div>`, true);
  }),
});
