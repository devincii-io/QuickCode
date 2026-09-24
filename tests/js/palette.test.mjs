import test from "node:test";
import assert from "node:assert/strict";
import { groupItems, rankItems, scoreItem } from "../../quickcode/frontend/js/palette.js";
import { workspaceItems } from "../../quickcode/frontend/js/workspace_palette.js";
import { SLASH_COMMANDS } from "../../quickcode/frontend/js/composer/commands.js";
import { SLASH } from "../../quickcode/frontend/js/help/shortcuts.js";

const item = (title, more = {}) => ({ title, run() {}, ...more });
const titles = (items) => items.map((i) => i.title);

test("an empty query keeps every item in its given order", () => {
  const items = [item("b"), item("a"), item("c")];
  assert.deepEqual(titles(rankItems(items, "  ")), ["b", "a", "c"]);
});

test("the start of a title beats the start of a word, which beats the middle", () => {
  const items = [item("Recompute"), item("/compact"), item("Undo compact"), item("Compact now")];
  assert.deepEqual(titles(rankItems(items, "comp")), ["Compact now", "/compact", "Undo compact", "Recompute"]);
});

test("every term must match somewhere; the title outranks the hint and keywords", () => {
  const items = [
    item("Switch to plan mode", { hint: "Read-only exploration", group: "Permission mode" }),
    item("Plan review", { keywords: "mode" }),
    item("Models", { group: "Settings" }),
  ];
  assert.deepEqual(titles(rankItems(items, "plan mode")), ["Switch to plan mode", "Plan review"]);
  assert.deepEqual(titles(rankItems(items, "settings")), ["Models"]);
  assert.deepEqual(titles(rankItems(items, "plan zebra")), []);
});

test("a single run of letters in order is a weak match of its own", () => {
  const items = [item("New agent pane"), item("Show or hide the sidebar")];
  assert.deepEqual(titles(rankItems(items, "nwag")), ["New agent pane"]);
  assert.equal(scoreItem(item("New agent pane"), ["n"]), 100);
  // Two scattered terms are not fuzzy-matched: that would match nearly anything.
  assert.deepEqual(titles(rankItems(items, "nw ag")), []);
});

test("ties keep the order the caller gave", () => {
  const items = [item("Open alpha"), item("Open beta"), item("Open gamma")];
  assert.deepEqual(titles(rankItems(items, "open")), ["Open alpha", "Open beta", "Open gamma"]);
});

test("groups come out in the order they first appear", () => {
  const groups = groupItems([item("a", { group: "X" }), item("b", { group: "Y" }), item("c", { group: "X" })]);
  assert.deepEqual(groups.map(([name, list]) => [name, titles(list)]), [["X", ["a", "c"]], ["Y", ["b"]]]);
});

test("the help lists and the palette read the one slash-command table", () => {
  assert.deepEqual(SLASH.map(([name]) => name), SLASH_COMMANDS.map((c) => c.name));
  assert.ok(SLASH.some(([name, arg]) => name === "/mode" && arg.includes("plan")));
});

test("the shell's palette offers what the shell can do, where it can", () => {
  const calls = [];
  const act = new Proxy({}, { get: (_, name) => (...args) => calls.push([name, ...args]) });
  const state = {
    home: false, active: "p1", zoomed: null, undo: true,
    workspaces: [
      { id: "p1", name: "Site", ready: true, focused: "a", panes: [{ id: "a", title: "Fix login", state: "Working" }] },
      { id: "p2", name: "Portal", ready: true, focused: null, panes: [{ id: "b", title: "Docs", state: "Ready" }] },
    ],
  };
  const items = workspaceItems(state, act, [{ slug: "keyboard", title: "Keyboard & commands" }]);
  const byTitle = (t) => items.find((i) => i.title === t);
  assert.ok(byTitle("Reopen closed pane") && byTitle("Split right") && byTitle("Maximize the focused pane"));
  assert.equal(byTitle("Fix login").current, true);
  assert.equal(byTitle("Docs").hint, "Portal · Ready");
  byTitle("Docs").run();
  byTitle("Split below").run();
  byTitle("Keyboard & commands").run();
  assert.deepEqual(calls, [["focusPane", "p2", "b"], ["split", "v"], ["route", "#/help/keyboard"]]);

  const atHome = workspaceItems({ ...state, home: true, undo: false }, act);
  assert.equal(atHome.find((i) => i.title === "New agent pane"), undefined);
  assert.equal(atHome.find((i) => i.title === "Fix login").current, false);
  assert.ok(atHome.find((i) => i.title === "Open folder…"));
});
