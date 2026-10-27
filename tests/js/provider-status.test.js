"use strict";
const fs = require("fs"), vm = require("vm"), assert = require("assert"), path = require("path");
const source = fs.readFileSync(path.join(__dirname, "../../enoy/ui/app.js"), "utf8");
const buttons = [{ disabled: false }, { disabled: false }];
const dialog = { dataset: { provider: "claude" }, querySelectorAll: () => buttons };
const nodes = { "status-line": {}, "provider-dialog": dialog, "provider-feedback": {}, modal: { classList: { contains: () => false } } };
let html = "", request, focus;
const ctx = { CODEX_MARK: '<svg></svg>', S: { status: null, config: { claude: { enabled: true } } }, NAME: {codex:"Codex",claude:"Claude"},
  $: id => nodes[id], esc: s => String(s).replace(/</g,"&lt;"),
  window: { addEventListener: (event, fn) => { focus = fn; } },
  openModal: value => { html = value; }, renderModelPicker() {}, toast() {},
  api: async (...args) => { request = args; return { message: "Terminal opened" }; },
};
vm.createContext(ctx);
vm.runInContext(source.slice(source.indexOf("function renderStatusLine()"), source.indexOf("// ---------------------------------------------------------------- top bar")),ctx);
(async () => {
  ctx.renderStatusLine();
  assert(nodes["status-line"].innerHTML.includes("Unknown"));
  assert.equal((nodes["status-line"].innerHTML.match(/<button /g)||[]).length, 2);
  ctx.S.status = { codex: {available:true}, claude: {available:false, detail:"<unsafe>"} };
  ctx.renderStatusLine();
  assert(nodes["status-line"].innerHTML.includes("Ready"));
  assert(nodes["status-line"].innerHTML.includes("Unavailable"));
  ctx.showProviderStatus("claude");
  assert(html.includes("&lt;unsafe>"));
  assert(html.includes("Claude subscription"));
  assert(html.includes('data-action="usage-refresh" data-provider="claude"'));
  assert(html.includes("Open Claude Usage"));
  ctx.S.usage = {codex:{source:"Codex subscription",checked_at:Date.now()/1000,windows:[{label:"5 hours",remaining_percent:0,resets_at:Date.now()/1000+3600},{label:"Weekly",remaining_percent:75,resets_at:1}]}};
  ctx.showProviderStatus("codex");
  assert(html.includes("0% left"));
  assert(html.includes("Update needed"));
  assert(html.includes('value="0"'));
  assert(!html.includes('value="75"'));
  ctx.S.usage.claude = {source:"Claude subscription",checked_at:Date.now()/1000,windows:[{label:"5 hours",remaining_percent:100,resets_at:null}]};
  ctx.showProviderStatus("claude");
  assert(html.includes("100% left"));
  assert(html.includes("Reset time not reported"));
  assert(!html.includes("1/1/1970"));
  assert(html.includes("Launch & sign in"));
  ctx.S.config.claude.enabled = false;
  ctx.showProviderStatus("claude");
  assert(html.includes("Enable in Settings"));
  assert(!html.includes('data-action="provider-launch"'));
  ctx.S.config.claude.enabled = true;
  await ctx.providerAction({dataset:{provider:"claude",launch:"login"}});
  assert.equal(request[0],"POST");
  assert.equal(request[1],"/api/providers/claude/launch");
  assert.equal(request[2].action,"login");
  assert(html.includes("Terminal opened"));
  ctx.api = async () => ({codex:{available:true},claude:{available:true,detail:"Signed in"}});
  await focus();
  assert(html.includes("Launch CLI"));
  ctx.api = async () => { throw new Error("Status unavailable"); };
  await ctx.providerAction({dataset:{provider:"claude"}},true);
  assert(html.includes("Status unavailable"));
  assert(html.includes("danger-text"));
  console.log("Provider status UI checks passed");
})().catch(e => { console.error(e); process.exitCode=1; });
