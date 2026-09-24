import test from "node:test";
import assert from "node:assert/strict";

const root = { dataset: {} };
let osReduced = false;
globalThis.document = { documentElement: root };
globalThis.window = { matchMedia: () => ({ matches: osReduced }) };

const { motionAllowed } = await import("../../quickcode/frontend/js/appearance.js");

test("motion needs both the system preference and the Animate setting to allow it", () => {
  // The activity line's spinner is advanced by script, so the CSS rules that
  // stop every animation under data-motion="false" never reach it.
  root.dataset.motion = "true"; osReduced = false;
  assert.equal(motionAllowed(), true);
  root.dataset.motion = "false";
  assert.equal(motionAllowed(), false, "Animate activity indicators is off");
  root.dataset.motion = "true"; osReduced = true;
  assert.equal(motionAllowed(), false, "the system asks for reduced motion");
  delete root.dataset.motion; osReduced = false;
  assert.equal(motionAllowed(), true, "before the appearance is applied, the default animates");
});
