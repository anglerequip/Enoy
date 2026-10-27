"use strict";
// Enoy's own window header in the desktop app (see enoy/winframe.py).
// The page's header rows are the real Windows title bar (CSS app-region: drag);
// these three buttons send the standard minimize / maximize / close commands.
// In a browser tab /api/window reports custom: false and nothing here runs.

const FRAME_ICONS = {
  minimize: '<svg viewBox="0 0 10 10" aria-hidden="true"><path d="M0 5h10"/></svg>',
  maximize: '<svg viewBox="0 0 10 10" aria-hidden="true"><rect x=".5" y=".5" width="9" height="9" rx="1.2"/></svg>',
  restore: '<svg viewBox="0 0 10 10" aria-hidden="true"><rect x=".5" y="2.5" width="7" height="7" rx="1.2"/><path d="M2.5 2.5V1.7A1.2 1.2 0 0 1 3.7.5h4.6a1.2 1.2 0 0 1 1.2 1.2v4.6a1.2 1.2 0 0 1-1.2 1.2H7.5"/></svg>',
  close: '<svg viewBox="0 0 10 10" aria-hidden="true"><path d="M.5.5l9 9M9.5.5l-9 9"/></svg>',
};

function frameButton(action, label, icon) {
  // tabindex -1 like Windows' own caption buttons; the keyboard has Alt+Space, Alt+F4 and Win+arrows.
  return `<button type="button" class="wc-btn wc-${action}" data-wc="${action}" tabindex="-1" title="${label}" aria-label="${label}">${icon}</button>`;
}

function syncFrameTheme() {
  if (!document.body.classList.contains("custom-frame")) return;
  const css = getComputedStyle(document.documentElement);
  const bg = css.getPropertyValue("--bg").trim();
  const dark = matchMedia("(prefers-color-scheme: dark)").matches
    ? document.documentElement.dataset.theme !== "light" : document.documentElement.dataset.theme === "dark";
  if (/^#[0-9a-f]{6}$/i.test(bg)) api("POST", "/api/window/theme", { dark, color: bg }).catch(() => {});
}

async function syncFrameState() {
  try {
    const s = await api("GET", "/api/window");
    const btn = document.querySelector(".wc-toggle");
    if (!btn) return;
    btn.innerHTML = s.maximized ? FRAME_ICONS.restore : FRAME_ICONS.maximize;
    btn.title = btn.ariaLabel = s.maximized ? "Restore" : "Maximize";
    document.body.classList.toggle("frame-maximized", !!s.maximized);
  } catch (e) { /* the window state is cosmetic */ }
}

async function startFrame() {
  let s;
  try { s = await api("GET", "/api/window"); } catch (e) { return; }
  if (!s.custom) return;
  document.body.classList.add("custom-frame");
  const box = document.createElement("div");
  box.id = "winctl";
  box.setAttribute("role", "group");
  box.setAttribute("aria-label", "Window");
  box.innerHTML = frameButton("minimize", "Minimize", FRAME_ICONS.minimize)
    + frameButton("toggle", "Maximize", FRAME_ICONS.maximize) + frameButton("close", "Close", FRAME_ICONS.close);
  box.addEventListener("click", e => {
    const b = e.target.closest("[data-wc]");
    if (b) api("POST", `/api/window/${b.dataset.wc}`).catch(() => {});
  });
  document.body.appendChild(box);
  let t = null;
  addEventListener("resize", () => { clearTimeout(t); t = setTimeout(syncFrameState, 80); });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", syncFrameTheme);
  syncFrameState();
  syncFrameTheme();
}

startFrame();
