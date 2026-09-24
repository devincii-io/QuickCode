import test from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const FRONTEND = resolve(dirname(fileURLToPath(import.meta.url)), "../../quickcode/frontend");

// Classes whose names are put together at runtime from a value, so the full
// name never appears in the source: `"pill mode-" + s.mode`, `toast-${kind}`.
const RUNTIME_PREFIXES = [
  "chip-", "k-", "mode-", "pt-", "qt-", "r-", "s-", "src-", "st-", "tier-", "toast-",
  "upd-state-",
];

function files(dir, ext) {
  return readdirSync(dir, { recursive: true })
    .filter((f) => f.endsWith(ext))
    .map((f) => join(dir, f));
}

/** Every selector prelude in a stylesheet: the text before each `{`, minus
 *  at-rules. Comments and strings go first, so `url("a.png")` is no class. */
function selectors(css) {
  const src = css
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'/g, '""');
  const out = [];
  let start = 0;
  for (let i = 0; i < src.length; i++) {
    if (src[i] === "{") { out.push(src.slice(start, i).trim()); start = i + 1; }
    else if (src[i] === "}" || src[i] === ";") start = i + 1;
  }
  return out.filter((s) => s && !s.startsWith("@"));
}

const classes = new Map();   // class -> stylesheets that select it
for (const file of files(join(FRONTEND, "css"), ".css")) {
  for (const sel of selectors(readFileSync(file, "utf8"))) {
    for (const [, name] of sel.matchAll(/\.(-?[_a-zA-Z][\w-]*)/g)) {
      if (!classes.has(name)) classes.set(name, new Set());
      classes.get(name).add(relative(FRONTEND, file).replaceAll("\\", "/"));
    }
  }
}

const js = files(join(FRONTEND, "js"), ".js").map((f) => readFileSync(f, "utf8")).join("\n");
const html = files(FRONTEND, ".html").map((f) => readFileSync(f, "utf8")).join("\n");
const tokens = new Set(`${js}\n${html}`.match(/[\w-]+/g));
const runtime = (name) => RUNTIME_PREFIXES.find((p) => name.startsWith(p));

test("every class a stylesheet selects is used by the markup or the scripts", () => {
  assert.ok(classes.size > 500, `only ${classes.size} classes parsed`);
  const dead = [...classes]
    .filter(([name]) => !tokens.has(name) && !runtime(name))
    .map(([name, sheets]) => `.${name} (${[...sheets].join(", ")})`);
  assert.deepEqual(dead, [], `selected but never used:\n  ${dead.join("\n  ")}`);
});

test("each runtime prefix is still built at runtime and still needed", () => {
  const escape = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  for (const prefix of RUNTIME_PREFIXES) {
    const built = new RegExp(`[\\s"'\`]${escape(prefix)}(?:\\$\\{|["'\`]\\s*\\+)`);
    assert.ok(built.test(js), `nothing in js/ builds a "${prefix}…" class any more`);
    const needed = [...classes.keys()].some((c) => runtime(c) === prefix && !tokens.has(c));
    assert.ok(needed, `every "${prefix}…" class is spelled out in full: drop the prefix`);
  }
});
