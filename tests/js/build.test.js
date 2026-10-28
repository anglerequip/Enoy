"use strict";
// Pure helpers of the Build workspace page (enoy/ui/build.js).
const fs = require("fs"), vm = require("vm"), assert = require("assert"), path = require("path");
const source = fs.readFileSync(path.join(__dirname, "../../enoy/ui/build.js"), "utf8");
const ctx = { esc: s => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;") };
vm.createContext(ctx);
vm.runInContext(source.slice(0, source.indexOf("// ---------------------------------------------------------------- state and loading")), ctx);

// Status badges: every task state has a label; unknown states are escaped, never raw HTML.
for (const s of ["pending", "ready", "testing", "implementing", "verifying", "reviewing", "merging", "completed", "failed",
                 "blocked", "waiting_network", "interrupted", "cancelled"]) {
  assert(!ctx.buildBadge(s).includes(`>${s}<`) || s === "ready", `no label for ${s}`);
}
assert(ctx.buildBadge("completed").includes('class="bstat ok"'));
assert(ctx.buildBadge("blocked").includes("bad"));
assert(ctx.buildBadge("<img>").includes("&lt;img&gt;"));

// Roles: offline makes cloud roles unavailable but not local ones.
const cloud = { id: "ds", local: false, ok: true }, local = { id: "q", local: true, ok: true };
assert.deepEqual(ctx.roleState(cloud, true), { ok: true, text: "Cloud · ready" });
assert.equal(ctx.roleState(cloud, false).ok, false);
assert(ctx.roleState(cloud, false).text.includes("Offline"));
assert.deepEqual(ctx.roleState(local, false), { ok: true, text: "Local · ready" });
assert.deepEqual(ctx.roleState({ id: "" }, true), { ok: false, text: "Not chosen" });
assert.equal(ctx.roleState({ id: "ds", local: false, ok: false, reason: "No API key saved." }, true).text, "No API key saved.");

// Approval: only the latest version of an unapproved plan, and not while the planner is still working.
const detail = { plan: { approved_version: null }, versions: [{ version: 1 }, { version: 2 }], job: { busy: false } };
assert.equal(ctx.canApprove(detail, 2), true);
assert.equal(ctx.canApprove(detail, 1), false);
assert.equal(ctx.canApprove({ ...detail, job: { busy: true } }, 2), false);
assert.equal(ctx.canApprove({ ...detail, plan: { approved_version: 2 } }, 2), false);

// Money and progress.
assert.equal(ctx.usd(null), "unknown");
assert.equal(ctx.usd(0), "$0.00");
assert.equal(ctx.usd(0.0007), "$0.0007");
assert.equal(ctx.usd(1.5), "$1.50");
assert.deepEqual(JSON.parse(JSON.stringify(ctx.planProgress([{ status: "completed" }, { status: "ready" }]))),
                 { done: 1, total: 2, label: "1 of 2 tasks done" });
assert.equal(ctx.planProgress([]).label, "No tasks yet");
assert.equal(ctx.busyTasks([{ status: "ready" }, { status: "testing" }]), true);
assert.equal(ctx.busyTasks([{ status: "completed" }]), false);
console.log("build.test.js passed");
