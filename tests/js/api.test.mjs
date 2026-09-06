import test from "node:test";
import assert from "node:assert/strict";
import { api, initAuth, setProject } from "../../quickcode/frontend/js/api.js";

test("embedded views recover auth from this tab without a token in their URL", () => {
  globalThis.location = { hash: "", search: "?pane=1&project=alpha&resume=conversation" };
  globalThis.sessionStorage = { getItem: () => "tab-token" };
  assert.deepEqual(initAuth(), { token: "tab-token", project: "alpha", resumeHint: "conversation" });
});

test("credits use the authenticated install endpoint even inside a project", async () => {
  setProject("alpha");
  let call;
  globalThis.fetch = async (path, options) => {
    call = { path, options };
    return { ok: true, status: 200, json: async () => ({ available: 5 }) };
  };
  assert.deepEqual(await api.credits(), { available: 5 });
  assert.equal(call.path, "/api/credits");
  assert.equal(call.options.headers["x-quickcode-token"], "tab-token");
});
