"use strict";
// Tests for fusion_ai/ui/markdown.js. Run by tests/test_markdown_js.py with Node.
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const src = fs.readFileSync(path.join(__dirname, "..", "..", "fusion_ai", "ui", "markdown.js"), "utf8");
const ctx = {};
vm.createContext(ctx);
vm.runInContext(src + "\nthis.MD = MD;", ctx);
const MD = ctx.MD;

const results = [];
function test(name, fn) {
  try { fn(); results.push(["ok", name]); } catch (e) { results.push(["FAIL", name, e.message]); }
}
const text = html => html.replace(/<[^>]+>/g, "").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, "&");

test("plain text", () => assert.strictEqual(MD.render("Hello world"), "<p>Hello world</p>"));
test("escapes HTML from the model", () => {
  const h = MD.render('<img src=x onerror="alert(1)"> <script>bad()</script>');
  assert(!h.includes("<img") && !h.includes("<script"), h);
});
test("unicode and emoji survive", () => assert(MD.render("Ünïcödé 日本語 🚀 ✓").includes("Ünïcödé 日本語 🚀 ✓")));
test("headings and lists", () => {
  const h = MD.render("# Title\n\n- a\n- b\n\n1. one\n2. two");
  assert(h.includes("<h2>Title</h2>") && h.includes("<ul><li>a</li><li>b</li></ul>") && h.includes("<ol><li>one</li><li>two</li></ol>"), h);
});
test("nested lists", () => {
  const h = MD.render("1. first\n   - inner a\n   - inner b\n2. second");
  assert(/<ol><li>first<ul><li>inner a<\/li><li>inner b<\/li><\/ul><\/li><li>second<\/li><\/ol>/.test(h), h);
});
test("ordered list start number", () => assert(MD.render("3. c\n4. d").startsWith('<ol start="3">')));
test("multiple code blocks keep exact source", () => {
  const code = "def f(x):\n\treturn x  # tab and trailing  ";
  const h = MD.render("```python\n" + code + "\n```\n\ntext\n\n```js\nlet a = `t`;\n```");
  const blocks = h.split('<div class="codeblock').length - 1;
  assert.strictEqual(blocks, 2);
  const first = h.match(/<pre><code>([\s\S]*?)<\/code><\/pre>/)[1];
  assert.strictEqual(text(first), code);
});
test("unclosed fence while streaming renders as code, then closes", () => {
  const partial = MD.render("Intro\n\n```python\ndef f():\n    pass", true);
  assert(partial.includes('class="codeblock open"') && partial.includes("def"), partial);
  const done = MD.render("Intro\n\n```python\ndef f():\n    pass\n```");
  assert(done.includes('class="codeblock"') && !done.includes("codeblock open"));
});
test("table with alignment", () => {
  const h = MD.render("| Name | Score |\n|:-----|------:|\n| Ada | 10 |");
  assert(h.includes("<table>") && h.includes('<th style="text-align:right">Score</th>') && h.includes("<td>Ada</td>"), h);
});
test("links: only http(s)/mailto become links", () => {
  const h = MD.render("[ok](https://example.com) [bad](javascript:alert(1)) https://bare.example.org/x.");
  assert(h.includes('<a href="https://example.com" target="_blank" rel="noopener noreferrer">ok</a>'), h);
  assert(!h.includes('href="javascript'), h);
  assert(h.includes('href="https://bare.example.org/x"'), h);
});
test("inline code is not formatted inside", () => assert(MD.render("`**not bold**`").includes("<code>**not bold**</code>")));
test("blockquote and hr", () => {
  const h = MD.render("> quoted **text**\n\n---\n\nafter");
  assert(h.includes("<blockquote><p>quoted <strong>text</strong></p></blockquote>") && h.includes("<hr>"), h);
});
test("dangling bold while streaming shows no raw asterisks", () => {
  const h = MD.render("This is **importa", true);
  assert(h.includes("<strong>importa</strong>"), h);
  const opener = MD.render("Next **", true);
  assert(!opener.includes("**"), opener);
});
test("dangling inline code and partial link while streaming", () => {
  assert(MD.render("Run `npm te", true).includes("<code>npm te</code>"));
  const h = MD.render("See [the docs](https://exa", true);
  assert(!h.includes("[") && h.includes("the docs"), h);
});
test("streaming in small chunks never loses or duplicates text and ends identical", () => {
  const answer = "## Plan 🚀\n\nSome **bold** and *it* `code` [l](https://e.com).\n\n- a\n  - b\n\n```py\nx = 1\n```\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n> q\n\nEnd ✓";
  for (const step of [1, 2, 3, 7]) {
    for (let i = step; i < answer.length; i += step) {
      const h = MD.render(answer.slice(0, i), true);
      assert(typeof h === "string");
    }
  }
  const finalStream = MD.render(answer, true);
  assert.strictEqual(finalStream, MD.render(answer));
  const visible = text(MD.render(answer));
  for (const word of ["Plan", "🚀", "bold", "code", "x = 1", "End ✓"]) assert(visible.includes(word), word);
});
test("Enoy change blocks become chips, not code", () => {
  const h = MD.render("<<<FILE a.py\n```\nprint(1)\n```\n>>>END\n\n<<<RUN python -m pytest>>>");
  assert(h.includes("Wrote <code>a.py</code>") && !h.includes("print(1)") && h.includes('data-cmd="python -m pytest"'), h);
});
test("syntax highlighting keeps the exact text", () => {
  const code = 'def f(a="#not comment"):  # real comment\n    return 0x1F + 2.5';
  const h = MD.highlight(code, "python");
  assert.strictEqual(text(h), code);
  assert(h.includes('<span class="tk-c"># real comment</span>') && h.includes('<span class="tk-s">&quot;#not comment&quot;</span>'), h);
});
test("cached render matches render", () => assert.strictEqual(MD.cached("**x**"), MD.render("**x**")));

let failed = 0;
for (const r of results) { if (r[0] !== "ok") failed++; console.log(r.join(" | ")); }
console.log(`${results.length - failed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
