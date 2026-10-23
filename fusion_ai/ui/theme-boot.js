"use strict";
// Runs before the stylesheet paints, so a saved light/dark choice never flashes
// the other theme at startup. (Inline scripts are blocked by the page's CSP.)
try {
  const t = localStorage.getItem("theme");
  if (t === "light" || t === "dark") document.documentElement.dataset.theme = t;
} catch (e) { /* storage unavailable: follow the system theme */ }
