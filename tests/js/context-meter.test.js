const assert = require('assert');
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync('enoy/ui/app.js', 'utf8');
const start = source.indexOf('function latestContextReading()');
const end = source.indexOf('function progressHtml(', start);
const ctx = {
  S: { chat: null },
  NAME: { codex: 'Codex', claude: 'Claude' },
  esc: s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
  messageModelInfo: m => m.info || {},
};
vm.createContext(ctx);
vm.runInContext(source.slice(start, end), ctx);

assert.strictEqual(ctx.contextMeterHtml(), '', 'hide the meter until a provider reports usable data');
ctx.S.chat = { messages: [
  { role: 'assistant', backend: 'codex', info: { context: { used_percent: 12.3, used_tokens: 129150, window_tokens: 1050000 } } },
  { role: 'user', backend: 'enoy' },
] };
const html = ctx.contextMeterHtml();
assert(html.includes('Context ≈12.3%'));
const title = html.match(/title="([^"]+)"/)[1];
const titleDigits = title.replace(/\D/g, '');
assert(titleDigits.includes('129150') && titleDigits.includes('1050000'), 'tooltip reports localized input and context token counts');
assert(html.includes('context-meter-track'));

ctx.S.chat.messages.push({ role: 'assistant', backend: 'claude', info: { context: { used_percent: 6, used_tokens: 60000, window_tokens: 1000000 } } });
assert.strictEqual(ctx.latestContextReading().provider, 'claude', 'latest assistant response wins');
assert(ctx.contextMeterHtml().includes('Context ≈6%'));

ctx.S.chat.messages.at(-1).info.context.used_percent = 140;
assert(ctx.contextMeterHtml().includes('Context ≈100%'), 'progress is clamped to the bar range');
console.log('context meter tests passed');
