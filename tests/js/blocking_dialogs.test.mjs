import test from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const JS = resolve(dirname(fileURLToPath(import.meta.url)), "../../quickcode/frontend/js");

test("nothing asks through window.confirm, alert or prompt", () => {
  // They block the one event loop every pane of the window shares: while one
  // sits unanswered in Settings, no agent pane handles a socket message or
  // paints a streamed token. Ask through ui/modal.js confirmModal, or
  // settings/ui.js confirmSheet over a sheet.
  const found = [];
  for (const file of readdirSync(JS, { recursive: true }).filter((f) => f.endsWith(".js"))) {
    const src = readFileSync(join(JS, file), "utf8")
      .replace(/\/\*[\s\S]*?\*\//g, "")
      .replace(/^\s*\/\/.*$/gm, "");
    for (const m of src.matchAll(/\b(?:window\.)?(confirm|alert|prompt)\(/g)) {
      const before = src.slice(Math.max(0, m.index - 1), m.index);
      if (before === "." && !m[0].startsWith("window.")) continue;   // someone.confirm(...)
      found.push(`${file.replaceAll("\\", "/")}: ${m[0]}`);
    }
  }
  assert.deepEqual(found, []);
});
