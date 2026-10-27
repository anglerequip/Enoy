"use strict";
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync('enoy/ui/app.js', 'utf8');
const requests = [], toasts = [];
const ctx = { S: {config: {chat: {experience: 'chatgpt'}}},
  mode: () => 'chat', toast: value => toasts.push(value),
  api: async (...args) => { requests.push(args); return {message: 'Opened'}; },
};
vm.createContext(ctx);
vm.runInContext(source.slice(source.indexOf('function regularChat()'), source.indexOf('function codeProjects()')), ctx);
vm.runInContext(source.slice(source.indexOf('async function send('), source.indexOf('async function runSlashCommand(')), ctx);
(async () => {
  await ctx.send('This must never go to a model');
  assert.equal(requests.length, 0);
  await ctx.openRegularChatGPT();
  assert.equal(requests.length, 1);
  assert.equal(requests[0][1], '/api/chatgpt/open');
  assert.deepEqual(Object.keys(requests[0][2]), []);
  assert(!requests.some(r => r[1].includes('/tasks')));
  ctx.S.config.chat.experience = 'integrated';
  assert.equal(ctx.regularChat(), false);
  ctx.S.config = null;
  assert.equal(ctx.regularChat(), true);
  console.log('Regular ChatGPT never sends a Codex prompt');
})().catch(e => { console.error(e); process.exitCode = 1; });
