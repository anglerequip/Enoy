"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");
const source = fs.readFileSync(path.join(__dirname, "../../enoy/ui/app.js"), "utf8");

function harness() {
  const timers = new Map();
  let timer = 0;
  const requests = [];
  const renders = [];
  const S = { chatId: 1, chat: { task: { id: 1, status: "running" } },
    stream: { 1: "The answer", 2: "Other answer" }, thinking: { 1: "Working" },
    streamDone: { 2: {} }, shown: { 1: 10 } };
  const ctx = { S, document: { querySelector: () => null },
    api: () => new Promise((resolve, reject) => requests.push({ resolve, reject })),
    setTimeout: fn => { timers.set(++timer, fn); return timer; },
    clearTimeout: id => timers.delete(id), loadChats: async () => {},
    renderSidebar() {}, renderChatMenu() {}, renderComposer() {},
    renderThread: () => renders.push(S.chat),
  };
  vm.createContext(ctx);
  vm.runInContext(source.slice(source.indexOf("let chatLoadVersion"), source.indexOf("// ---------------------------------------------------------------- sidebar")) +
    source.slice(source.indexOf("let refreshTimer"), source.indexOf("function connectEvents")), ctx);
  return { S, ctx, requests, renders, schedule: () => ctx.scheduleRefresh(),
    tick: () => { const [id, fn] = timers.entries().next().value; timers.delete(id); return fn(); } };
}
const saved = { task: { id: 1, status: "completed" }, messages: [{ role: "assistant", content: "The answer" }] };

(async () => {
  // Completion while a request is in flight must not clear its stream using
  // the older snapshot. Refreshes remain serialized and fetch again.
  const h = harness();
  h.schedule();
  const first = h.tick();
  h.S.streamDone[1] = {};
  h.schedule();
  assert.equal(h.requests.length, 1);
  h.requests[0].resolve({ task: { id: 1, status: "completed" }, messages: [] });
  await first;
  assert.equal(h.renders.length, 0);
  assert.equal(h.S.stream[1], "The answer");
  const second = h.tick();
  h.requests[1].resolve(saved);
  await second;
  assert.equal(h.renders[0], saved);
  assert.equal(h.S.stream[1], undefined);
  assert.equal(h.S.stream[2], "Other answer");
  assert(h.S.streamDone[2]);

  // A temporary failure retries automatically and preserves visible text.
  const f = harness();
  f.schedule();
  const failed = f.tick();
  f.requests[0].reject(new Error("offline"));
  await failed;
  assert.equal(f.S.stream[1], "The answer");
  f.tick(); // retry delay
  const retry = f.tick();
  f.requests[1].resolve(saved);
  await retry;
  assert.equal(f.renders[0], saved);

  // Switching chats while fetching cannot put the previous chat on screen.
  const s = harness();
  s.schedule();
  const old = s.tick();
  s.S.chatId = 2;
  s.requests[0].resolve(saved);
  await old;
  assert.equal(s.renders.length, 0);
  assert.equal(s.S.stream[1], "The answer");

  // Explicit loads resolving out of order also keep the selected chat.
  const l = harness();
  const a = l.ctx.loadChat(1);
  const b = l.ctx.loadChat(2);
  const chat2 = { task: { id: 2 } };
  l.requests[1].resolve(chat2);
  await b;
  l.requests[0].resolve(saved);
  await a;
  assert.equal(l.S.chat, chat2);
  console.log("Chat refresh regression tests passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
