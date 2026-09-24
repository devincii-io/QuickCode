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

test("a structured refusal keeps its detail, and its message still reads as text", async () => {
  setProject("alpha");
  const conflicts = [{ path: "a.py", conflicts: [{ kind: "modified", detail: "changed" }] }];
  let call;
  globalThis.fetch = async (path, options) => {
    call = { path, options };
    return {
      ok: false, status: 409, statusText: "Conflict",
      json: async () => ({ detail: { message: "files changed since their checkpoints", conflicts } }),
    };
  };
  const err = await api.rewindFiles("c0nv", { turn: 2, paths: ["a.py"] }).catch((e) => e);
  assert.equal(call.path, "/api/projects/alpha/sessions/c0nv/checkpoints/rewind");
  assert.deepEqual(JSON.parse(call.options.body), { turn: 2, paths: ["a.py"] });
  assert.equal(err.message, "409: files changed since their checkpoints");
  assert.equal(err.status, 409);
  assert.deepEqual(err.detail.conflicts, conflicts);

  globalThis.fetch = async () => ({
    ok: false, status: 409, statusText: "Conflict",
    json: async () => ({ detail: "conversation is busy (a turn is running); rewind once it is idle" }),
  });
  const busy = await api.rewindFiles("c0nv", { turn: 2 }).catch((e) => e);
  assert.equal(busy.message, "409: conversation is busy (a turn is running); rewind once it is idle");
  assert.equal(busy.detail, "conversation is busy (a turn is running); rewind once it is idle");
});
