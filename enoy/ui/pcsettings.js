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
    pcs = await api("GET", "/api/pc");
  } catch (e) { body.innerHTML = `<div class="page-state bad">Could not load: ${esc(e.message)}</div>`; return; }
  if (S.settingsTab !== "pc" || !$("set-body")) return;
  body.innerHTML = `
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
}

Object.assign(actions, {
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
      </table>
      <div class="row" style="justify-content:flex-end"><button class="btn" data-action="open-settings" data-tab="pc">Back</button><button class="btn primary" data-action="close-modal">Close</button></div>`, true);
  }),
});
