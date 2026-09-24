// Plain-text matching, the same rules the server's session search applies
// (quickcode/session/search.py): case-insensitive terms split on whitespace,
// a double-quoted run counting as one term, and every term required.

import { esc } from "./util.js";

const TERM = /"([^"]+)"|([^\s"]+)/g;

export function queryTerms(query) {
  const out = [];
  for (const m of String(query ?? "").matchAll(TERM)) {
    const term = (m[1] ?? m[2]).split(/\s+/).filter(Boolean).join(" ").toLowerCase();
    if (term && !out.includes(term)) out.push(term);
  }
  return out;
}

export function matchesAll(text, terms) {
  const low = String(text ?? "").toLowerCase();
  return terms.every((t) => low.includes(t));
}

/** `text` as escaped HTML with every occurrence of a term in <mark>. */
export function highlightHtml(text, terms) {
  const s = String(text ?? "");
  const low = s.toLowerCase();
  // Only when lower-casing kept every offset: otherwise marks would land beside
  // the words they are meant to cover.
  if (!terms.length || low.length !== s.length) return esc(s);
  const marks = [];
  for (const t of terms) {
    for (let at = low.indexOf(t); at !== -1 && t; at = low.indexOf(t, at + t.length)) {
      marks.push([at, at + t.length]);
    }
  }
  marks.sort((a, b) => a[0] - b[0]);
  let html = "", pos = 0;
  for (const [start, end] of marks) {
    if (end <= pos) continue;
    const from = Math.max(start, pos);
    html += esc(s.slice(pos, from)) + "<mark>" + esc(s.slice(from, end)) + "</mark>";
    pos = end;
  }
  return html + esc(s.slice(pos));
}
