// TOON in the browser: the same encoding the model reads, so the transcript
// shows what was actually sent rather than a JSON re-render of it.
//
// This mirrors `quickcode/context/toon.py`. Two implementations of one format
// is a cost, and it is paid on purpose: the server never ships tool arguments
// as text (they arrive as the model's JSON, which is the wire format), so the
// only place a TOON view of them can be built is here. Keep the two in step --
// `tests/test_toon.py` pins the Python side, and the shapes below are the ones
// it pins.
//
// INTEGRATION
//   import { toon, highlightToon } from "./toon.js";
//   pre.innerHTML = highlightToon(toon(argsObject));

const COMMA = ",";
export const TAB = "\t";

const INDENT = 2;
const MAX_DEPTH = 24;

const LITERALS = new Set(["true", "false", "null"]);
const NUMBERISH = /^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?$/;
// eslint-disable-next-line no-control-regex
const CONTROL = /[\x00-\x1f\x7f]/;
const KEY_BREAKERS = /[:"{}[\],]/;

const ESCAPES = { "\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t" };

export function toon(value, { delimiter = COMMA, indent = INDENT } = {}) {
  const lines = [];
  write(null, value, lines, 0, delimiter, indent, 0);
  return lines.join("\n");
}

// ---- the four forms ----

function write(key, value, lines, depth, d, indent, guard) {
  const pad = " ".repeat(depth * indent);
  const label = key === null ? "" : fmtKey(key, d);

  if (guard > MAX_DEPTH) {
    lines.push(`${pad}${label}: <too deeply nested>`);
    return;
  }
  if (Array.isArray(value)) {
    writeArray(label, value, lines, pad, depth, d, indent, guard);
  } else if (value !== null && typeof value === "object") {
    writeObject(label, value, lines, pad, depth, d, indent, guard);
  } else {
    lines.push(`${pad}${label}${label ? ": " : ""}${fmtScalar(value, d)}`);
  }
}

function writeObject(label, obj, lines, pad, depth, d, indent, guard) {
  const keys = Object.keys(obj);
  if (!keys.length) {
    lines.push(label ? `${pad}${label}:` : pad);
    return;
  }

  // Keyed tabular: an object whose values are all uniform objects.
  const spec = keys.length > 1 ? tableSpec(keys.map((k) => obj[k])) : null;
  if (spec) {
    lines.push(`${pad}${label}[${keys.length}:]{${header(spec, d)}}:`);
    const inner = " ".repeat((depth + 1) * indent);
    for (const k of keys) {
      lines.push(`${inner}${fmtKey(k, d)}: ${cells(obj[k], spec, d).join(d)}`);
    }
    return;
  }

  let at = depth;
  if (label) {
    lines.push(`${pad}${label}:`);
    at += 1;
  }
  for (const k of keys) write(k, obj[k], lines, at, d, indent, guard + 1);
}

function writeArray(label, items, lines, pad, depth, d, indent, guard) {
  const n = items.length;
  if (!n) {
    lines.push(`${pad}${label}[0]:`);
    return;
  }
  if (items.every(isScalar)) {
    lines.push(`${pad}${label}[${n}]: ` + items.map((x) => fmtScalar(x, d)).join(d));
    return;
  }
  const spec = tableSpec(items);
  if (spec) {
    lines.push(`${pad}${label}[${n}]{${header(spec, d)}}:`);
    const inner = " ".repeat((depth + 1) * indent);
    for (const row of items) lines.push(inner + cells(row, spec, d).join(d));
    return;
  }

  lines.push(`${pad}${label}[${n}]:`);
  const lead = (depth + 1) * indent;
  for (const item of items) {
    const block = [];
    write(null, item, block, depth + 2, d, indent, guard + 1);
    if (!block.length) block.push(" ".repeat(lead + indent));
    const first = block[0];
    block[0] = (first.slice(0, lead) + "- " + first.slice(lead + indent)).replace(/\s+$/, "");
    lines.push(...block);
  }
}

// ---- deciding whether a table is possible ----

function tableSpec(rows) {
  if (!rows.length) return null;
  const plain = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
  if (!rows.every((r) => plain(r) && Object.keys(r).length)) return null;

  const names = Object.keys(rows[0]);
  const wanted = new Set(names);
  for (const r of rows) {
    const ks = Object.keys(r);
    if (ks.length !== wanted.size || !ks.every((k) => wanted.has(k))) return null;
  }

  const spec = [];
  for (const name of names) {
    const column = rows.map((r) => r[name]);
    if (column.every(isScalar)) {
      spec.push([name, null]);
    } else if (column.every((v) => plain(v) && Object.keys(v).length)) {
      const sub = tableSpec(column);
      if (!sub) return null;
      spec.push([name, sub]);
    } else {
      return null;   // a list in a cell has nowhere to go on a flat row
    }
  }
  return spec;
}

function header(spec, d) {
  return spec
    .map(([name, sub]) => (sub ? `${fmtKey(name, d)}{${header(sub, d)}}` : fmtKey(name, d)))
    .join(d);
}

function cells(row, spec, d) {
  const out = [];
  for (const [name, sub] of spec) {
    if (sub) out.push(...cells(row[name], sub, d));
    else out.push(fmtScalar(row[name], d));
  }
  return out;
}

// ---- scalars ----

function isScalar(v) {
  return v === null || v === undefined || ["string", "number", "boolean"].includes(typeof v);
}

function fmtScalar(value, d) {
  if (value === null || value === undefined) return "null";
  if (value === true) return "true";
  if (value === false) return "false";
  if (typeof value === "number") {
    return Number.isFinite(value) ? String(value) : quote(String(value));
  }
  if (typeof value === "string") return needsQuote(value, d) ? quote(value) : value;
  return quote(String(value));
}

function needsQuote(s, d) {
  if (s === "" || s !== s.trim()) return true;
  if (s.includes(d) || s.includes('"') || CONTROL.test(s)) return true;
  if (LITERALS.has(s) || NUMBERISH.test(s)) return true;
  return "#-[{".includes(s[0]);
}

function quote(s) {
  let out = '"';
  for (const ch of s) {
    if (ESCAPES[ch]) out += ESCAPES[ch];
    else if (ch.charCodeAt(0) < 0x20 || ch.charCodeAt(0) === 0x7f) {
      out += "\\u" + ch.charCodeAt(0).toString(16).padStart(4, "0");
    } else out += ch;
  }
  return out + '"';
}

function fmtKey(key, d) {
  const s = String(key);
  if (!s || s !== s.trim()) return quote(s);
  if (s.includes(d) || KEY_BREAKERS.test(s) || CONTROL.test(s)) return quote(s);
  return s;
}

// ---- reading it ----

const ESC_HTML = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" };
const escHtml = (s) => String(s).replace(/[&<>"]/g, (c) => ESC_HTML[c]);

// A header line: `name[3]{a,b}:` or `name[2:]{a,b}:` or `name[0]:`.
const HEAD = /^(\s*)([^[\]:]*)(\[\d+:?\])(\{.*\})?:(.*)$/;

/** TOON as highlighted HTML. Structure is coloured; values are not. */
export function highlightToon(text) {
  const out = [];
  for (const line of String(text).split("\n")) {
    const head = HEAD.exec(line);
    if (head) {
      const [, pad, name, count, fields, rest] = head;
      out.push(
        pad +
          `<span class="tn-key">${escHtml(name)}</span>` +
          `<span class="tn-count">${escHtml(count)}</span>` +
          (fields ? `<span class="tn-fields">${escHtml(fields)}</span>` : "") +
          `<span class="tn-punc">:</span>` +
          (rest ? `<span class="tn-row">${escHtml(rest)}</span>` : ""),
      );
      continue;
    }
    const kv = /^(\s*)(-\s)?([^:]+): (.*)$/.exec(line);
    if (kv) {
      const [, pad, dash, key, value] = kv;
      out.push(
        pad +
          (dash ? `<span class="tn-punc">${dash}</span>` : "") +
          `<span class="tn-key">${escHtml(key)}</span>` +
          `<span class="tn-punc">:</span> ` +
          `<span class="tn-val">${escHtml(value)}</span>`,
      );
      continue;
    }
    out.push(`<span class="tn-row">${escHtml(line)}</span>`);
  }
  return out.join("\n");
}
