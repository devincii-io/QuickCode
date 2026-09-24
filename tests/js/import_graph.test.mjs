import test from "node:test";
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { dirname, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const JS = resolve(dirname(fileURLToPath(import.meta.url)), "../../quickcode/frontend/js");

// Static imports only: what a module pulls in the moment it is loaded.
function staticImports(file) {
  const src = readFileSync(file, "utf8")
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/^\s*\/\/.*$/gm, "");
  const specs = [...src.matchAll(/^\s*(?:import|export)\b[^;]*?\bfrom\s*["']([^"']+)["']/gm)]
    .concat([...src.matchAll(/^\s*import\s*["']([^"']+)["']/gm)])
    .map((m) => m[1])
    .filter((s) => s.startsWith("."));
  return specs.map((s) => resolve(dirname(file), s));
}

function closure(entry) {
  const seen = new Set();
  const visit = (file) => {
    if (seen.has(file)) return;
    assert.ok(existsSync(file), `${relative(JS, file)} is imported but missing`);
    seen.add(file);
    staticImports(file).forEach(visit);
  };
  visit(resolve(JS, entry));
  return new Set([...seen].map((f) => relative(JS, f).replaceAll("\\", "/")));
}

test("the outer workspace window never loads the pane's socket", () => {
  // ws.js registers window and document listeners at import time and exists for
  // exactly one agent pane's conversation; the shell has none.
  const shell = closure("workspaces.js");
  assert.ok(!shell.has("ws.js"), "workspaces.js reaches ws.js");
  assert.ok(!shell.has("modals.js"), "workspaces.js reaches the modals.js compatibility re-export");
  assert.ok(!shell.has("reviews.js"), "workspaces.js reaches the review queue");
});

test("an agent pane still loads everything it wires", () => {
  const pane = closure("main.js");
  for (const m of ["ws.js", "reviews.js", "menus.js", "composer/slash.js", "statusbar.js",
    "connbanner.js", "sessionbar.js", "ui/modal.js", "ui/menu.js"]) {
    assert.ok(pane.has(m), `main.js no longer reaches ${m}`);
  }
});

test("the modal shell stays free of the socket and the review queue", () => {
  const shell = closure("ui/modal.js");
  assert.ok(!shell.has("ws.js") && !shell.has("reviews.js"), [...shell].join(", "));
});
