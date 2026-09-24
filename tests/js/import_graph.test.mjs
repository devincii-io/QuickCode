import test from "node:test";
import assert from "node:assert/strict";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { dirname, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const JS = resolve(dirname(fileURLToPath(import.meta.url)), "../../quickcode/frontend/js");

function source(file) {
  return readFileSync(file, "utf8")
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/^\s*\/\/.*$/gm, "");
}

const local = (file, specs) => specs.filter((s) => s.startsWith(".")).map((s) => resolve(dirname(file), s));

// Static imports only: what a module pulls in the moment it is loaded.
function staticImports(file) {
  const src = source(file);
  return local(file, [...src.matchAll(/^\s*(?:import|export)\b[^;(]*?\bfrom\s*["']([^"']+)["']/gm)]
    .concat([...src.matchAll(/^\s*import\s*["']([^"']+)["']/gm)])
    .map((m) => m[1]));
}

// `import("./x.js")` with a literal path: loaded later, but loaded.
function dynamicImports(file) {
  return local(file, [...source(file).matchAll(/\bimport\(\s*["']([^"']+)["']\s*\)/g)].map((m) => m[1]));
}

function closure(entry, { dynamic = false } = {}) {
  const seen = new Set();
  const visit = (file) => {
    if (seen.has(file)) return;
    assert.ok(existsSync(file), `${relative(JS, file)} is imported but missing`);
    seen.add(file);
    staticImports(file).forEach(visit);
    if (dynamic) dynamicImports(file).forEach(visit);
  };
  visit(resolve(JS, entry));
  return new Set([...seen].map((f) => relative(JS, f).replaceAll("\\", "/")));
}

test("every module is reachable from entry.js", () => {
  // A module nothing imports is left over from a split or a merge: it is still
  // served, still syntax-checked, and still read by whoever greps for a name.
  const reached = closure("entry.js", { dynamic: true });
  const all = readdirSync(JS, { recursive: true })
    .filter((f) => f.endsWith(".js"))
    .map((f) => f.replaceAll("\\", "/"));
  const orphans = all.filter((f) => !reached.has(f));
  assert.deepEqual(orphans, [], `imported by nothing: ${orphans.join(", ")}`);
});

test("the outer workspace window never loads the pane's socket", () => {
  // ws.js registers window and document listeners at import time and exists for
  // exactly one agent pane's conversation; the shell has none.
  const shell = closure("workspaces.js");
  assert.ok(!shell.has("ws.js"), "workspaces.js reaches ws.js");
  assert.ok(!shell.has("reviews.js"), "workspaces.js reaches the review queue");
  // The shell's palette and notifications are its own; the pane's reach ws.js.
  assert.ok(shell.has("palette.js") && shell.has("notify.js"), "the shell lost its palette or notices");
  assert.ok(!shell.has("palette_pane.js") && !shell.has("pane_notices.js"), "the shell loads a pane's palette or notices");
});

test("the outer workspace window never loads the configuration view", () => {
  // home.js reaches trust.js, which refreshes an open configuration page after a
  // trust decision; that refresh imports config/view.js on demand, since the
  // shell itself never shows the view and would otherwise load the whole tree.
  const shell = closure("workspaces.js");
  const config = [...shell].filter((m) => m.startsWith("config/"));
  assert.deepEqual(config, [], `workspaces.js reaches ${config.join(", ")}`);
});

test("an agent pane still loads everything it wires", () => {
  const pane = closure("main.js");
  for (const m of ["ws.js", "reviews.js", "menus.js", "composer/slash.js", "statusbar.js",
    "connbanner.js", "sessionbar.js", "ui/modal.js", "ui/menu.js", "config/view.js",
    "palette_pane.js", "pane_notices.js"]) {
    assert.ok(pane.has(m), `main.js no longer reaches ${m}`);
  }
});

test("the modal shell stays free of the socket and the review queue", () => {
  const shell = closure("ui/modal.js");
  assert.ok(!shell.has("ws.js") && !shell.has("reviews.js"), [...shell].join(", "));
});
