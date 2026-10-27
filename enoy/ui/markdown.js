"use strict";
// Markdown for answers and artifacts. Everything is HTML-escaped first: this page
// holds the API token, so model output must never become live markup.
//
// The text is split into blocks (paragraph, list, code, table, ...). Each block
// renders to exactly one element, so a streaming answer can be updated block by
// block: finished blocks keep their DOM (no flicker, selection survives) and only
// the block still being written is redrawn. The finished answer uses the same
// renderer, so the switch from streaming to done looks identical.

const MD = (() => {
  const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  const escape = s => String(s ?? "").replace(/[&<>"']/g, c => ESC[c]);

  // ------------------------------------------------------------ inline

  // While streaming, an opening ** or ` without its partner would show raw
  // markers and then flip to formatting when the partner arrives. Close (or drop)
  // the dangling marker for display only; the stored text is never changed.
  function settle(src) {
    let s = src;
    const link = s.match(/\[([^\]\n]*)(\]\([^)\s]*)?$/);
    if (link) s = s.slice(0, link.index) + link[1];
    const ticks = (s.match(/`/g) || []).length;
    if (ticks % 2) s = /`\s*$/.test(s) ? s.replace(/`\s*$/, "") : s + "`";
    const bold = (s.match(/\*\*/g) || []).length;
    if (bold % 2) s = /\*\*\s*$/.test(s) ? s.replace(/\*\*\s*$/, "") : s + "**";
    return s;
  }

  function emphasis(s) {
    return s
      .replace(/\*\*(?=\S)([\s\S]*?\S)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^\w])__(?=\S)([\s\S]*?\S)__(?!\w)/g, "$1<strong>$2</strong>")
      .replace(/(^|[^*\w])\*(?=[^\s*])([^*\n]*?[^\s*])\*(?![*\w])/g, "$1<em>$2</em>")
      .replace(/(^|[^\w])_(?=[^\s_])([^_\n]*?[^\s_])_(?!\w)/g, "$1<em>$2</em>")
      .replace(/~~(?=\S)([^~\n]*?\S)~~/g, "<del>$1</del>");
  }

  function inline(src, streaming = false) {
    let s = streaming ? settle(src) : src;
    const keep = [];
    const hold = html => `\u0000${keep.push(html) - 1}\u0000`;
    // Code spans first: nothing inside them is formatted.
    s = s.replace(/(`+)([^`]|[^`][\s\S]*?[^`])\1(?!`)/g, (m, t, body) => hold(`<code>${escape(body.replace(/^ ([\s\S]*) $/, "$1"))}</code>`));
    // Links: only web and mail addresses; they open in the system browser.
    s = s.replace(/\[([^\]\n]+)\]\(((?:https?:\/\/|mailto:)[^\s)]+)\)/g,
      (m, text, url) => hold(`<a href="${escape(url)}" target="_blank" rel="noopener noreferrer">${emphasis(escape(text))}</a>`));
    s = s.replace(/(^|[\s(])(https?:\/\/[^\s<>()]*[^\s<>().,:;!?'"])/g,
      (m, pre, url) => pre + hold(`<a href="${escape(url)}" target="_blank" rel="noopener noreferrer">${escape(url)}</a>`));
    s = emphasis(escape(s));
    return s.replace(/\u0000(\d+)\u0000/g, (m, i) => keep[Number(i)]);
  }

  // ------------------------------------------------------------ code highlighting

  const KW = {
    py: "and as assert async await break class continue def del elif else except False finally for from global if import in is lambda None nonlocal not or pass raise return True try while with yield self match case",
    js: "async await break case catch class const continue debugger default delete do else export extends false finally for from function get if import in instanceof let new null of return set static super switch this throw true try typeof undefined var void while yield interface type enum implements readonly as",
    lua: "and break continue do else elseif end false for function goto if in local nil not or repeat return then true until while self export type",
    c: "abstract auto bool boolean break case catch char class const continue default delete do double else enum extern false final finally float fn for func go goto if impl import int interface let long loop match mod mut namespace new null nullptr override package private protected pub public return self short static string struct super switch template this throw trait true try type typedef typename union unsigned use using var virtual void volatile while",
    sh: "if then else elif fi for while until do done case esac in function return export local echo exit set unset param foreach",
    sql: "select from where and or not insert into values update set delete create table drop alter join left right inner outer full on group by order having limit offset as distinct null is in like between primary key foreign references index view union all case when then else end",
  };
  const FAMILY = {
    python: "py", py: "py", javascript: "js", js: "js", jsx: "js", ts: "js", typescript: "js", tsx: "js", json: "js", mjs: "js",
    lua: "lua", luau: "lua", c: "c", h: "c", cpp: "c", "c++": "c", cs: "c", csharp: "c", "c#": "c", java: "c", kotlin: "c",
    go: "c", rust: "c", rs: "c", swift: "c", php: "c", dart: "c", scala: "c", bash: "sh", sh: "sh", shell: "sh", zsh: "sh",
    powershell: "sh", ps1: "sh", ps: "sh", sql: "sql",
  };
  const COMMENT = {
    py: String.raw`#[^\n]*`, sh: String.raw`#[^\n]*`, sql: String.raw`--[^\n]*`,
    lua: String.raw`--\[\[[\s\S]*?(?:\]\]|$)|--[^\n]*`,
    js: String.raw`\/\/[^\n]*|\/\*[\s\S]*?(?:\*\/|$)`, c: String.raw`\/\/[^\n]*|\/\*[\s\S]*?(?:\*\/|$)`,
  };
  const STRING = {
    py: String.raw`"""[\s\S]*?(?:"""|$)|'''[\s\S]*?(?:'''|$)|"(?:\\.|[^"\\\n])*"?|'(?:\\.|[^'\\\n])*'?`,
    js: String.raw`\x60(?:\\[\s\S]|[^\x60\\])*\x60?|"(?:\\.|[^"\\\n])*"?|'(?:\\.|[^'\\\n])*'?`,
    lua: String.raw`\[\[[\s\S]*?(?:\]\]|$)|"(?:\\.|[^"\\\n])*"?|'(?:\\.|[^'\\\n])*'?`,
    default: String.raw`"(?:\\.|[^"\\\n])*"?|'(?:\\.|[^'\\\n])*'?`,
  };
  const tokenizers = {};
  function tokenizer(fam) {
    if (!tokenizers[fam]) {
      const words = KW[fam].split(" ").map(w => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|");
      const parts = [`(${COMMENT[fam]})`, `(${STRING[fam] || STRING.default})`, String.raw`(\b(?:0x[\da-fA-F]+|\d+(?:\.\d+)?(?:e[+-]?\d+)?)\b)`,
                     `(\\b(?:${words})\\b)`, String.raw`(\b[A-Za-z_]\w*(?=\s*\())`];
      tokenizers[fam] = new RegExp(parts.join("|"), fam === "sql" ? "gi" : "g");
    }
    tokenizers[fam].lastIndex = 0;
    return tokenizers[fam];
  }
  const CLS = [null, "tk-c", "tk-s", "tk-n", "tk-k", "tk-f"];

  function highlight(code, lang) {
    const fam = FAMILY[lang];
    if (!fam || code.length > 200000) return escape(code);
    const re = tokenizer(fam);
    let out = "", last = 0, m;
    while ((m = re.exec(code))) {
      if (!m[0]) { re.lastIndex++; continue; }
      out += escape(code.slice(last, m.index));
      const k = m.findIndex((g, i) => i > 0 && g !== undefined);
      out += `<span class="${CLS[k]}">${escape(m[0])}</span>`;
      last = m.index + m[0].length;
    }
    return out + escape(code.slice(last));
  }

  // ------------------------------------------------------------ blocks

  const RE = {
    fence: /^\s{0,3}(`{3,}|~{3,})\s*([\w+#.-]*)/,
    heading: /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/,
    hr: /^\s{0,3}([-*_])(?:\s*\1){2,}\s*$/,
    quote: /^\s{0,3}>/,
    item: /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/,
    tableSep: /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/,
    enoy: /^<<<(FILE|EDIT)\s+(.+)$/,
    enoyOne: /^<<<(DELETE|RUN)\s+(.+?)\s*>>>$/,
  };
  const indentOf = l => l.match(/^\s*/)[0].replace(/\t/g, "    ").length;
  const isTableStart = (lines, i) => lines[i].includes("|") && i + 1 < lines.length && RE.tableSep.test(lines[i + 1]) && lines[i + 1].includes("-");
  const startsBlock = (lines, i) => {
    const l = lines[i], t = l.trim();
    return RE.fence.test(l) || RE.heading.test(l) || RE.hr.test(l) || RE.quote.test(l) || RE.item.test(l)
      || RE.enoy.test(t) || RE.enoyOne.test(t) || isTableStart(lines, i);
  };

  function parse(text) {
    const lines = String(text ?? "").replace(/\r\n?/g, "\n").split("\n");
    const blocks = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      const t = line.trim();
      if (!t) { i++; continue; }
      const start = i;
      let m;
      if ((m = line.match(RE.fence))) {
        const mark = m[1];
        const close = new RegExp(`^\\s{0,3}${mark[0] === "`" ? "`" : "~"}{${mark.length},}\\s*$`);
        const buf = [];
        let closed = false;
        i++;
        while (i < lines.length) { if (close.test(lines[i])) { closed = true; i++; break; } buf.push(lines[i++]); }
        blocks.push({ type: "code", lang: m[2].toLowerCase(), code: buf.join("\n"), closed, src: lines.slice(start, i).join("\n") });
        continue;
      }
      if ((m = t.match(RE.enoy))) {
        let closed = false;
        i++;
        while (i < lines.length) { if (lines[i].trim() === ">>>END") { closed = true; i++; break; } i++; }
        blocks.push({ type: "enoy", verb: m[1], path: m[2], closed, src: lines.slice(start, i).join("\n") });
        continue;
      }
      if ((m = t.match(RE.enoyOne))) { blocks.push({ type: "enoy", verb: m[1], path: m[2], closed: true, src: line }); i++; continue; }
      if ((m = line.match(RE.heading))) { blocks.push({ type: "heading", level: m[1].length, text: m[2], src: line }); i++; continue; }
      if (RE.hr.test(line)) { blocks.push({ type: "hr", src: line }); i++; continue; }
      if (RE.quote.test(line)) {
        const buf = [];
        while (i < lines.length && lines[i].trim() && (RE.quote.test(lines[i]) || !startsBlock(lines, i))) buf.push(lines[i++].replace(/^\s{0,3}> ?/, ""));
        blocks.push({ type: "quote", text: buf.join("\n"), src: lines.slice(start, i).join("\n") });
        continue;
      }
      if (isTableStart(lines, i)) {
        i += 2;
        while (i < lines.length && lines[i].trim() && lines[i].includes("|")) i++;
        blocks.push({ type: "table", lines: lines.slice(start, i), src: lines.slice(start, i).join("\n") });
        continue;
      }
      if (RE.item.test(line)) {
        const base = indentOf(line);
        const kind = l => (/\d/.test(l.match(RE.item)[2]) ? "ol" : "ul");
        const sameList = l => RE.item.test(l) && indentOf(l) <= base && kind(l) === kind(line);
        i++;
        while (i < lines.length) {
          const l = lines[i];
          if (!l.trim()) {
            // A blank line ends the list unless more of the same list follows.
            let j = i + 1;
            while (j < lines.length && !lines[j].trim()) j++;
            if (j < lines.length && (indentOf(lines[j]) > base || sameList(lines[j]))) { i = j; continue; }
            break;
          }
          if (RE.item.test(l) && indentOf(l) <= base && !sameList(l)) break;  // Bullets switch to numbers: a new list.
          if (indentOf(l) <= base && !RE.item.test(l) && startsBlock(lines, i)) break;
          if (indentOf(l) < base && !RE.item.test(l)) break;
          i++;
        }
        blocks.push({ type: "list", lines: lines.slice(start, i), src: lines.slice(start, i).join("\n") });
        continue;
      }
      const buf = [line];
      i++;
      while (i < lines.length && lines[i].trim() && !startsBlock(lines, i)) buf.push(lines[i++]);
      blocks.push({ type: "para", text: buf.join("\n"), src: buf.join("\n") });
    }
    return blocks;
  }

  function cells(row) {
    let r = row.trim();
    if (r.startsWith("|")) r = r.slice(1);
    if (r.endsWith("|") && !r.endsWith("\\|")) r = r.slice(0, -1);
    return r.split(/(?<!\\)\|/).map(c => c.trim().replace(/\\\|/g, "|"));
  }

  function renderList(lines, streaming) {
    const base = indentOf(lines[0]);
    const ordered = /\d/.test(lines[0].match(RE.item)[2]);
    const startNum = ordered ? parseInt(lines[0].match(RE.item)[2], 10) : 1;
    const items = [];
    for (const l of lines) {
      const m = l.match(RE.item);
      if (m && indentOf(l) <= base + 1) items.push({ first: m[3], rest: [], indent: indentOf(l) + m[2].length + 1 });
      else if (items.length) items[items.length - 1].rest.push(l);
    }
    const loose = lines.some((l, k) => !l.trim() && k < lines.length - 1);
    const lis = items.map((it, n) => {
      const last = n === items.length - 1;
      const sub = it.rest.map(l => l.replace(new RegExp(`^ {0,${it.indent}}`), "")).join("\n");
      const task = it.first.match(/^\[([ xX])\]\s+(.*)$/);
      const lead = task ? `<input type="checkbox" disabled ${task[1] !== " " ? "checked" : ""}> ${inline(task[2], streaming && last && !sub.trim())}`
                        : inline(it.first, streaming && last && !sub.trim());
      if (!sub.trim()) return `<li>${loose ? `<p>${lead}</p>` : lead}</li>`;
      return `<li>${loose ? `<p>${lead}</p>` : lead}${render(sub, streaming && last)}</li>`;
    });
    return ordered ? `<ol${startNum !== 1 ? ` start="${startNum}"` : ""}>${lis.join("")}</ol>` : `<ul>${lis.join("")}</ul>`;
  }

  function renderBlock(b, streaming) {
    switch (b.type) {
      case "code": {
        const label = b.lang || "code";
        return `<div class="codeblock${b.closed ? "" : " open"}"><div class="cb-head"><span class="cb-lang">${escape(label)}</span>
          <span class="cb-actions"><button class="btn ghost small cb-save" data-action="save-code-artifact" title="Save this code in Artifacts">Save</button>
          <button class="btn ghost small" data-action="copy-code" title="Copy the code">Copy</button></span></div><pre><code>${highlight(b.code, b.lang)}</code></pre></div>`;
      }
      case "enoy": {
        if (DUPES.has(b)) return `<span class="chip-dup" hidden></span>`;  // Same file again: one chip is enough.
        if (b.verb === "DELETE") return `<div class="chip-row"><span class="file-chip">🗑 Delete <code>${escape(b.path)}</code></span></div>`;
        if (b.verb === "RUN") return `<div class="chip-row"><span class="file-chip">▶ Suggested check <code>${escape(b.path)}</code>
          <button class="btn small" data-action="run-cmd" data-cmd="${escape(b.path)}">Run</button></span></div>`;
        const verb = b.verb === "FILE" ? "Wrote" : "Edited";
        return `<div class="chip-row"><span class="file-chip">✎ ${!b.closed && streaming ? "Writing" : verb} <code>${escape(b.path)}</code></span></div>`;
      }
      case "heading": {
        const tag = `h${Math.min(b.level + 1, 6)}`;
        return `<${tag}>${inline(b.text, streaming)}</${tag}>`;
      }
      case "hr": return "<hr>";
      case "quote": return `<blockquote>${render(b.text, streaming) || "<p></p>"}</blockquote>`;
      case "table": {
        const head = cells(b.lines[0]);
        const align = cells(b.lines[1]).map(c => c.startsWith(":") && c.endsWith(":") ? "center" : c.endsWith(":") ? "right" : "");
        const td = (c, k, tag) => `<${tag}${align[k] ? ` style="text-align:${align[k]}"` : ""}>${inline(c)}</${tag}>`;
        const rows = b.lines.slice(2).map(r => `<tr>${head.map((_, k) => td(cells(r)[k] ?? "", k, "td")).join("")}</tr>`).join("");
        return `<div class="table-wrap"><table><thead><tr>${head.map((c, k) => td(c, k, "th")).join("")}</tr></thead><tbody>${rows}</tbody></table></div>`;
      }
      case "list": return renderList(b.lines, streaming);
      default: return `<p>${inline(b.text, streaming).replace(/\n/g, "<br>")}</p>`;
    }
  }

  // File blocks (EDIT/FILE/DELETE) that repeat an earlier one for the same file and action.
  let DUPES = new Set();
  function markDupes(blocks) {
    const seen = new Set();
    DUPES = new Set();
    for (const b of blocks) {
      if (b.type !== "enoy" || b.verb === "RUN") continue;
      const key = b.verb + " " + b.path;
      if (seen.has(key)) DUPES.add(b); else seen.add(key);
    }
  }

  function render(text, streaming = false) {
    const blocks = parse(text);
    markDupes(blocks);
    return blocks.map((b, i) => renderBlock(b, streaming && i === blocks.length - 1)).join("");
  }

  // Finished answers are rendered once and reused (re-renders of the thread are free).
  const cache = new Map();
  function cached(text) {
    let html = cache.get(text);
    if (html === undefined) {
      html = render(text);
      cache.set(text, html);
      if (cache.size > 400) cache.delete(cache.keys().next().value);
    }
    return html;
  }

  // Update `el` to show `text`, touching only the blocks that changed.
  function renderInto(el, text, streaming) {
    const blocks = parse(text);
    markDupes(blocks);
    const keys = el._mdKeys && el.childElementCount === el._mdKeys.length ? el._mdKeys : (el.textContent = "", []);
    const next = [];
    blocks.forEach((b, i) => {
      const active = streaming && i === blocks.length - 1;
      const key = (active ? "~" : "=") + b.src;
      next.push(key);
      if (keys[i] === key) return;
      const tpl = document.createElement("template");
      tpl.innerHTML = renderBlock(b, active);
      const node = tpl.content.firstElementChild || document.createElement("p");
      const old = el.children[i];
      // A block that appears while streaming fades in. The block being written
      // is rebuilt as text arrives; the new node continues the same fade
      // (negative delay) instead of restarting it.
      const born = old ? old._born : streaming && keys.length ? performance.now() : 0;
      if (born && performance.now() - born < 260) {
        node._born = born;
        node.classList.add("md-in");
        node.style.animationDelay = `-${Math.round(performance.now() - born)}ms`;
      }
      if (old) el.replaceChild(node, old); else el.appendChild(node);
    });
    while (el.childElementCount > blocks.length) el.lastElementChild.remove();
    el._mdKeys = next;
  }

  return { render, cached, renderInto, inline, escape, highlight, parse };
})();
