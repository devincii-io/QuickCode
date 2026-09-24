// Syntax-highlighted JSON built from text nodes and spans — never markup — so a
// payload carrying `<img onerror=…>` in a tool result renders as the characters
// it is. The tokenizer is the same shape as settings/ui.js `highlightJson`;
// this one feeds DOM construction instead of an HTML string.

const JSON_RE = /("(?:\\.|[^"\\])*")(\s*:)?|\b(true|false|null)\b|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g;

/** Split JSON text into `{ cls, text }` runs; `cls` is null for punctuation
 *  and whitespace. Concatenating every `text` gives back `src` exactly. */
export function jsonTokens(src) {
  const out = [];
  const plain = (text) => {
    if (!text) return;
    const last = out[out.length - 1];
    if (last && last.cls === null) last.text += text;
    else out.push({ cls: null, text });
  };
  let last = 0;
  let m;
  JSON_RE.lastIndex = 0;
  while ((m = JSON_RE.exec(src)) !== null) {
    plain(src.slice(last, m.index));
    if (m[1] !== undefined) {
      out.push({ cls: m[2] ? "j-key" : "j-str", text: m[1] });
      plain(m[2] || "");
    } else if (m[3] !== undefined) {
      out.push({ cls: "j-lit", text: m[3] });
    } else {
      out.push({ cls: "j-num", text: m[4] });
    }
    last = JSON_RE.lastIndex;
  }
  plain(src.slice(last));
  return out;
}

/** Fill `pre` with highlighted JSON. */
export function renderJson(pre, src) {
  const frag = document.createDocumentFragment();
  for (const { cls, text } of jsonTokens(src)) {
    if (!cls) { frag.appendChild(document.createTextNode(text)); continue; }
    const span = document.createElement("span");
    span.className = cls;
    span.textContent = text;
    frag.appendChild(span);
  }
  pre.replaceChildren(frag);
  pre.classList.add("json");
  return pre;
}

/** Pretty JSON when `text` is a JSON object or array, else null. Only those two
 *  are worth re-encoding: a bare `12345` from a shell is text, not a number. */
export function prettyJson(text) {
  const s = String(text ?? "").trim();
  if (s[0] !== "{" && s[0] !== "[") return null;
  try { return JSON.stringify(JSON.parse(s), null, 2); } catch { return null; }
}
