"use strict";
const fs = require("fs"), vm = require("vm"), assert = require("assert");
const app = fs.readFileSync("enoy/ui/app.js", "utf8");
const activity = fs.readFileSync("enoy/ui/activity.js", "utf8");
const body = {};
const ctx = { S: { settingsTab: "activity" }, $: () => body,
  esc: x => String(x).replace(/</g, "&lt;"), AGENT: { codex: "ChatGPT / Codex" },
  ago: () => "just now", controlsHtml: () => "", Set, Date,
  api: async () => ({ transmissions: [{ id: 1, task_id: 2, created: 1, chars: 300,
    files: [{path: "<private>"}], usage: "input_tokens=10" }] }) };
vm.createContext(ctx);
vm.runInContext(app.slice(app.indexOf("async function settingsActivity()"), app.indexOf("function bindCfgInputs()")), ctx);
vm.runInContext(activity.slice(activity.indexOf("function activitySummaryHtml(d)"), activity.indexOf("function head(d)")), ctx);
(async () => {
  await ctx.settingsActivity();
  assert(body.innerHTML.includes("1 request(s) across 1 chat(s)"));
  assert(body.innerHTML.includes("&lt;private>"));
  assert(body.innerHTML.includes("<details>"));
  ctx.api = async () => { throw Error("Disconnected"); };
  await ctx.settingsActivity();
  assert(body.innerHTML.includes("Could not load activity: Disconnected"));
  ctx.api = async () => ({transmissions: []});
  await ctx.settingsActivity();
  assert(body.innerHTML.includes("Nothing has been sent"));
  const html = ctx.activitySummaryHtml({counts: {active:1,queued:0,completed:2,needs_you:0}, focus: {
    task: {title:"<task>",status:"running",owner:"codex",next:"Review changes"}, stage:"Editing files", last_activity:{text:"<update>",created:1} }});
  assert(html.includes("Your request is being worked on"));
  assert(html.includes("&lt;task>"));
  assert(html.includes("&lt;update>"));
  assert(html.includes("Review changes"));
  console.log("Activity summary checks passed");
})().catch(e => { console.error(e); process.exitCode=1; });
