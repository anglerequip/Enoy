"use strict";
// Runs before the stylesheet paints, so a saved light/dark choice never flashes
// the other theme at startup. (Inline scripts are blocked by the page's CSP.)
try {
  const t = localStorage.getItem("theme") || "dark";
  if (t === "light" || t === "dark") document.documentElement.dataset.theme = t;
  else if (t === "system") delete document.documentElement.dataset.theme;
} catch (e) { /* storage unavailable: keep the default dark theme */ }
