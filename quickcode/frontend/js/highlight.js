// Minimal syntax highlighting for tool payloads. No build step here, so no
// highlighter library: this is a single tokenizer over JSON, which is what
// every tool call and most results actually are.
//
// It escapes as it goes rather than escaping first and colouring after — the
// two orders are not equivalent, and doing it the other way round means a
// string containing "<span" can paint the rest of the document.

import { esc } from "./util.js";

const JSON_TOKEN =
  /("(?:\\.|[^"\\])*")(\s*:)?|\b(true|false|null)\b|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g;

export function highlightJson(text) {
  const src = String(text);
  let out = "";
  let last = 0;
  JSON_TOKEN.lastIndex = 0;
  let m;
  while ((m = JSON_TOKEN.exec(src)) !== null) {
    // Everything between tokens is escaped too, so this stays safe on input
    // that is not actually JSON.
    out += esc(src.slice(last, m.index));
    const [match, str, colon, literal, num] = m;
    if (str !== undefined) {
      out += `<span class="${colon ? "hl-key" : "hl-str"}">${esc(str)}</span>`;
      if (colon) out += esc(colon);
    } else if (literal !== undefined) {
      out += `<span class="hl-lit">${esc(literal)}</span>`;
    } else if (num !== undefined) {
      out += `<span class="hl-num">${esc(num)}</span>`;
    } else {
      out += esc(match);
    }
    last = m.index + match.length;
  }
  return out + esc(src.slice(last));
}

/** Pretty-print and highlight if it parses as JSON; otherwise escape as text. */
export function highlightPayload(raw) {
  const text = typeof raw === "string" ? raw : JSON.stringify(raw ?? "", null, 2);
  try {
    return highlightJson(JSON.stringify(JSON.parse(text), null, 2));
  } catch {
    return esc(text);
  }
}

/** True when a string is JSON worth pretty-printing. */
export function isJson(text) {
  const trimmed = String(text || "").trim();
  if (!trimmed || !"{[".includes(trimmed[0])) return false;
  try { JSON.parse(trimmed); return true; } catch { return false; }
}
