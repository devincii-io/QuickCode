import test from "node:test";
import assert from "node:assert/strict";

// slash.js reaches ws.js, which wires window/document listeners at import.
globalThis.window = { addEventListener() {} };
globalThis.document = { addEventListener() {} };
globalThis.location = { host: "127.0.0.1:1", hash: "", search: "" };

const { slashRows } = await import("../../quickcode/frontend/js/help/shortcuts.js");
const { entriesFor } = await import("../../quickcode/frontend/js/composer/slash.js");

test("the help lists exactly the slash commands the composer's menu offers", () => {
  const menu = entriesFor("/").map((e) => [e.label, e.arg || "", e.desc]);
  assert.deepEqual(slashRows(), menu);
  assert.ok(menu.some(([name]) => name === "/profile"), JSON.stringify(menu));
});
