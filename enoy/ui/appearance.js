"use strict";
// Workspace colour preferences. Only validated hex values reach CSS.
const EnoyAppearance = (() => {
  const storageKey = "enoy-colours-v1";
  const palettes = [
    {id:"clay", name:"Clay", light:"#f7f4ef", dark:"#252320", accent:"#b36749", night:"#dc9c7e"},
    {id:"sage", name:"Sage", light:"#f3f6f1", dark:"#202622", accent:"#52755c", night:"#a5c6aa"},
    {id:"graphite", name:"Graphite", light:"#f5f5f5", dark:"#222324", accent:"#5c626a", night:"#c1c7cf"},
    {id:"ocean", name:"Ocean", light:"#f1f5f9", dark:"#1c2630", accent:"#3b709a", night:"#8ebce0"},
    {id:"lavender", name:"Lavender", light:"#f6f3fa", dark:"#282330", accent:"#7a5eaa", night:"#c0a7e6"},
    {id:"rose", name:"Rose", light:"#faf2f3", dark:"#302328", accent:"#ac5b73", night:"#e0a1b5"},
    {id:"amber", name:"Amber", light:"#faf6ed", dark:"#2c271e", accent:"#946e24", night:"#dfbc70"},
    {id:"teal", name:"Teal", light:"#eff7f6", dark:"#1d2b2a", accent:"#377c78", night:"#8bc6c0"},
    {id:"forest", name:"Forest", light:"#f0f5ef", dark:"#1c281f", accent:"#427149", night:"#9ccca0"},
    {id:"indigo", name:"Indigo", light:"#f2f3fa", dark:"#22253a", accent:"#5b65ac", night:"#aeb7f1"},
    {id:"coffee", name:"Coffee", light:"#f7f1eb", dark:"#2d2520", accent:"#886143", night:"#d6ae8c"},
    {id:"contrast", name:"High contrast", light:"#ffffff", dark:"#101112", accent:"#174f86", night:"#c1dcff"},
  ];
  const validHex = value => typeof value === "string" && /^#[\da-f]{6}$/i.test(value);
  const channels = hex => [1,3,5].map(i => parseInt(hex.slice(i,i+2),16));
  function mix(a,b,ratio) {
    const x=channels(a), y=channels(b);
    return "#"+x.map((v,i)=>Math.round(v*(1-ratio)+y[i]*ratio).toString(16).padStart(2,"0")).join("");
  }
  function luminance(hex) {
    const c=channels(hex).map(v=>{ v/=255; return v<=.04045 ? v/12.92 : ((v+.055)/1.055)**2.4; });
    return .2126*c[0]+.7152*c[1]+.0722*c[2];
  }
  function contrast(a,b) { const x=luminance(a), y=luminance(b); return (Math.max(x,y)+.05)/(Math.min(x,y)+.05); }
  const defaultId = mode => mode === "code" ? "clay" : "sage";
  function read() {
    try { const value=JSON.parse(localStorage.getItem(storageKey)||"{}"); return value && typeof value==="object" && !Array.isArray(value) ? value : {}; }
    catch (_) { return {}; }
  }
  let preferences=read();
  function preference(mode) {
    const item=preferences[mode]||{};
    return {preset:palettes.some(p=>p.id===item.preset) ? item.preset : defaultId(mode),
      custom:Object.fromEntries(["accent","background","sidebar","panel"].filter(k=>validHex(item.custom?.[k])).map(k=>[k,item.custom[k]]))};
  }
  function dark() { return document.documentElement.dataset.theme === "dark" || (!document.documentElement.dataset.theme && matchMedia("(prefers-color-scheme: dark)").matches); }
  function colours(mode, night=dark(), pref=preference(mode)) {
    const palette=palettes.find(p=>p.id===pref.preset)||palettes[0];
    const background=pref.custom.background||(night?palette.dark:palette.light);
    const ink=luminance(background)>.35 ? "#27251f" : "#eeece6";
    const sidebar=pref.custom.sidebar||mix(background,night?"#000000":"#8d8271",night?.19:.065);
    const panel=pref.custom.panel||mix(background,"#ffffff",night?.045:.6);
    const accent=pref.custom.accent||(night?palette.night:palette.accent);
    return {background,sidebar,panel,accent,ink,
      raised:mix(panel,ink,.045), hover:mix(panel,ink,.065), active:mix(panel,accent,.12),
      muted:mix(background,ink,.66), faint:mix(background,ink,.51), border:mix(background,ink,.13),
      strong:mix(background,ink,.24), soft:mix(background,accent,.12), bubble:mix(panel,accent,.10),
      accentInk:contrast(accent,"#ffffff")>contrast(accent,"#161616") ? "#ffffff":"#161616"};
  }
  function apply(mode=document.documentElement.dataset.mode||"chat") {
    const c=colours(mode);
    const vars={bg:c.background,sidebar:c.sidebar,panel:c.panel,raised:c.raised,hover:c.hover,active:c.active,
      text:c.ink,muted:c.muted,faint:c.faint,border:c.border,"border-strong":c.strong,accent:c.accent,
      "accent-hover":mix(c.accent,c.ink,.14),"accent-soft":c.soft,focus:c.accent,"user-bubble":c.bubble,
      "code-bg":mix(c.background,"#000000",.12),"code-text":c.ink,"accent-ink":c.accentInk};
    Object.entries(vars).forEach(([key,value])=>document.documentElement.style.setProperty("--"+key,value));
    return c;
  }
  function save(mode, pref) {
    if (!["chat","code"].includes(mode)) return "Unknown workspace.";
    if (!pref || !palettes.some(p=>p.id===pref.preset) || !pref.custom ||
        Object.entries(pref.custom).some(([key,value])=>!["accent","background","sidebar","panel"].includes(key)||!validHex(value))) return "Choose a valid preset and hex colours.";
    const c=colours(mode,dark(),pref);
    if ([c.background,c.sidebar,c.panel].some(surface=>contrast(surface,c.ink)<4.5)) return "Keep the background, sidebar and panels similar in brightness so text stays readable.";
    const next={...preferences,[mode]:pref};
    try { localStorage.setItem(storageKey,JSON.stringify(next)); }
    catch (_) { /* the app database remains the durable source */ }
    preferences=next;
    apply();
    return "";
  }
  function restore(values) {
    preferences={};
    for (const mode of ["chat","code"]) {
      const item=values?.[mode];
      if (item && palettes.some(p=>p.id===item.preset) && item.custom && typeof item.custom==="object") {
        const pref={preset:item.preset,custom:Object.fromEntries(Object.entries(item.custom).filter(([key,value])=>["accent","background","sidebar","panel"].includes(key)&&validHex(value)))};
        const c=colours(mode,dark(),pref);
        preferences[mode]=[c.background,c.sidebar,c.panel].some(surface=>contrast(surface,c.ink)<4.5)?{preset:item.preset,custom:{}}:pref;
      }
    }
    try { localStorage.setItem(storageKey,JSON.stringify(preferences)); } catch (_) {}
    apply();
  }
  const system=matchMedia("(prefers-color-scheme: dark)");
  system.addEventListener("change",()=>{ apply(); if (typeof syncFrameTheme==="function") syncFrameTheme(); });
  apply();
  return {palettes,preference,colours,apply,save,restore,defaultId,validHex,contrast};
})();
