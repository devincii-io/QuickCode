// Minimal, safe markdown renderer for assistant text. Everything is escaped
// first; only the constructs below are re-introduced as markup. No raw HTML
// passthrough, no external requests.

import { esc } from "./util.js";

// Code spans are cut out before the other rules run and put back last: `**`
// or a link inside backticks is literal text, and markup must never land inside
// an href. NUL marks the slots, so a NUL in the text itself is dropped first
// rather than allowed to forge one.
function inline(s) {
  const spans = [];
  return s
    .replace(/\0/g, "")
    .replace(/`([^`]+)`/g, (_, c) => `\0${spans.push(c) - 1}\0`)
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[\s(])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, (m, t, u) => (/[\0<]/.test(u) ? m
      : `<a href="${u}" target="_blank" rel="noopener noreferrer">${t}</a>`))
    .replace(/\0(\d+)\0/g, (_, i) => `<code>${spans[i]}</code>`);
}

export function renderMarkdown(src) {
  const lines = esc(src).split("\n");
  const out = [];
  let inCode = false, codeBuf = [], listType = null, para = [];

  const flushPara = () => {
    if (para.length) { out.push(`<p>${inline(para.join("<br>"))}</p>`); para = []; }
  };
  const flushList = () => {
    if (listType) { out.push(`</${listType}>`); listType = null; }
  };

  for (const raw of lines) {
    if (raw.startsWith("```")) {
      if (inCode) {
        out.push(`<pre><code>${codeBuf.join("\n")}</code></pre>`);
        codeBuf = []; inCode = false;
      } else { flushPara(); flushList(); inCode = true; }
      continue;
    }
    if (inCode) { codeBuf.push(raw); continue; }

    const line = raw;
    const h = line.match(/^(#{1,4})\s+(.*)$/);
    const ul = line.match(/^\s*[-*]\s+(.*)$/);
    const ol = line.match(/^\s*\d+[.)]\s+(.*)$/);
    const bq = line.match(/^>\s?(.*)$/);

    if (h) {
      flushPara(); flushList();
      const lvl = Math.min(h[1].length, 4);
      out.push(`<h${lvl}>${inline(h[2])}</h${lvl}>`);
    } else if (ul) {
      flushPara();
      if (listType !== "ul") { flushList(); out.push("<ul>"); listType = "ul"; }
      out.push(`<li>${inline(ul[1])}</li>`);
    } else if (ol) {
      flushPara();
      if (listType !== "ol") { flushList(); out.push("<ol>"); listType = "ol"; }
      out.push(`<li>${inline(ol[1])}</li>`);
    } else if (bq) {
      flushPara(); flushList();
      out.push(`<blockquote>${inline(bq[1])}</blockquote>`);
    } else if (!line.trim()) {
      flushPara(); flushList();
    } else {
      flushList(); para.push(line);
    }
  }
  if (inCode) out.push(`<pre><code>${codeBuf.join("\n")}</code></pre>`);
  flushPara(); flushList();
  return out.join("");
}

// A buffer that is still growing. The renderer above is block-local: a blank
// line outside a code fence, or the line that closes one, leaves no parser
// state behind, so the text up to it renders the same whatever follows. Those
// blocks are rendered once and handed out as final; only the open tail is
// re-rendered on each update. Re-parsing the whole reply per delta was O(n²)
// over a streamed answer.
//
// `update(text)` takes the whole buffer so far and returns `{reset, commit,
// tail}`: `commit` is HTML to append after everything committed before, `tail`
// replaces the previous tail, and `reset` means the buffer did not extend the
// last one, so everything shown so far has to go.
export function markdownStream() {
  let seen = "";
  let committed = 0;   // seen.slice(0, committed) has been handed out as final
  let scanned = 0;     // start of the first line not yet classified
  let inCode = false;
  return {
    update(text) {
      const reset = !text.startsWith(seen);
      if (reset) { committed = 0; scanned = 0; inCode = false; }
      seen = text;
      let boundary = committed;
      for (let nl = text.indexOf("\n", scanned); nl !== -1; nl = text.indexOf("\n", scanned)) {
        const line = text.slice(scanned, nl);
        scanned = nl + 1;
        if (line.startsWith("```")) {
          inCode = !inCode;
          if (!inCode) boundary = scanned;
        } else if (!inCode && !line.trim()) {
          boundary = scanned;
        }
      }
      const commit = boundary > committed ? renderMarkdown(text.slice(committed, boundary)) : "";
      committed = boundary;
      return { reset, commit, tail: renderMarkdown(text.slice(committed)) };
    },
  };
}
