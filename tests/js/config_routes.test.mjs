import test from "node:test";
import assert from "node:assert/strict";
import { canonicalHref, recourseHref } from "../../quickcode/frontend/js/config/kinds.js";
import { compositionId } from "../../quickcode/frontend/js/config/create/composition.js";

const plugins = [
  { id: "runtime.permissions", kind: "policy" },
  { id: "prompt.tone", kind: "prompt_section" },
  { id: "agent.explore", kind: "agent" },
];

test("a settings recourse names a plugin and lands on that plugin's page", () => {
  const tool = { id: "tool.bash", kind: "tool", source: "internal" };
  const href = recourseHref({ action: "settings", target: "runtime.permissions" }, tool, plugins);
  assert.equal(href, canonicalHref(plugins[0]));
  assert.equal(href, "#/config/parts/policies/runtime.permissions");
  // A target that is not installed here is not a link to somewhere else.
  assert.equal(recourseHref({ action: "settings", target: "gone" }, tool, plugins), "");
});

test("author opens your own file, and is a duplicate on a built-in section", () => {
  const mine = { id: "tool.pytest-failed", kind: "tool", source: "authored" };
  assert.equal(recourseHref({ action: "author", target: "tool" }, mine, plugins),
    "#/config/edit/tool.pytest-failed");
  const env = { id: "prompt.environment", kind: "prompt_section", source: "internal" };
  assert.equal(recourseHref({ action: "author", target: "prompt_section" }, env, plugins), "");
  const other = { id: "hook.x", kind: "hook", source: "internal" };
  assert.equal(recourseHref({ action: "author", target: "tool" }, other, plugins),
    "#/config/new/tool");
});

test("docs and duplicate are actions, not navigations", () => {
  const p = { id: "runtime.session_log", kind: "storage", source: "internal" };
  assert.equal(recourseHref({ action: "docs", target: "docs/ARCHITECTURE.md" }, p, plugins), "");
  assert.equal(recourseHref({ action: "duplicate", target: "agent.explore" }, p, plugins), "");
});

test("a composition name previews the id the server stores it under", () => {
  assert.equal(compositionId("Review only"), "review-only");
  assert.equal(compositionId("  --Deep  Review!!  "), "deep-review");
  assert.equal(compositionId("2nd pass"), "c-2nd-pass");
  assert.equal(compositionId("???"), "");
});
