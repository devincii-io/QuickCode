# Phase 1 handoff

What landed, what other owners need to do, and where this deviates from
`PLAN-AUTHORING-AND-COMPOSITION.md` §4 Phase 1 — with the reason for each
deviation. Read alongside the plan; this file only records the deltas.

## 1. What other file owners must change

Nothing here blocks Phase 1 from working. Each item is a place where a file
this phase does not own now carries a small lie.

### `frontend/js/settings/presets.js` (Phase 3/4 owner)

`Preset.to_dict()["agents"]` is now **type-discriminated**: a legacy or
built-in preset still emits the spawn *list* that `presets.js:38` renders, but
a preset with per-agent compositions emits a **dict** of compositions and that
line would render `[object Object]`.

`to_dict()` therefore also emits an unambiguous `spawns: [...]` on every
preset. **Change `presets.js:38` to read `p.spawns`.** No other frontend change
is required; `tools`, `prompt_overrides` and `default_mode` are unchanged.

The payload additionally grows `orchestrator: {...}` and, when present,
`bindings: [...]`. Both are ignored by the current renderer.

### `kernel/spec.py` (Phase 2 owner)

**No change is needed for Phase 1.** Nothing here required a new `PluginSpec`
field. Two things to know when Phase 2 opens the file:

- `kernel/spec.py:Tier` (mutability) is now the only `Tier` in the tree. The
  model-cost alias in `config.py` was renamed to `ModelTier` (§3 below), so the
  two-`Tier` hazard the plan flagged in §0 is gone and it is safe to import
  `Tier` unqualified again.
- `AgentDef` gained `source` and `path`. `.md`-loaded definitions now report
  `source="config"` and a real `path`, which `manifest.agent_specs()` already
  reads via `getattr` and which Phase 2's `PluginSpec.path` can use directly.

### `kernel/manifest.py` (Phase 2 owner)

**No change is needed.** The `runtime.permissions.default_mode` setting the
manifest already declares is now actually consumed
(`kernel/resolve.py:default_mode`), which closes the plan's §0 defect 2.

One consequence for whoever owns the general-settings UI: `PUT /api/config`
writes `Config.default_mode`, and the plugin setting now **wins** over it. If
both are ever set from the UI, the config one silently loses. The cleanest fix
is for the general-settings mode control to write the plugin setting instead;
that is a `server/app.py` + frontend change and is not Phase 1's to make.

### `server/app.py` (Phase 4 owner)

Untouched, as required. `GET /api/kernel` will now find a `problems` array in
`PluginRegistry.to_json()` — currently only ever populated with the
`local_settings_ignored` info problem, but it is the array §1.12 specifies and
Phases 4/5 fill it.

## 2. Live defects, and what was done with each

| Defect (plan §0) | Resolution |
|---|---|
| 1. `plugins.<id>.enabled` enforced nowhere | `kernel/resolve.session_pool()` filters the pool by `tool.<name>` before any preset selection; `manager.open()` calls it. The toggle is now the session-wide revoke §1.3 defines. |
| 2. `default_mode` exists twice | `kernel/resolve.default_mode()` resolves one value: the plugin setting wins, `Config.default_mode` is the fallback. `manager.open()` consumes it. No manifest change needed. |
| 3. `AgentDef.max_turns` read nowhere | **Wired, not deleted.** See §4. |
| 4. `settings.local.json` invisible to `kernel/state.py` | Asymmetry kept deliberately (the local file is for accreted "always allow" rules, not configuration). `state.local_settings_problems()` emits an `info` `Problem` when a `plugins` or `presets` key is found there; it rides in both `Resolved.problems` and `PluginRegistry.problems`. |
| 5. `active_preset_id` reads project-then-user | Documented in `load_presets`'s docstring rather than changed — the combination is correct and only surprising undocumented. |
| Two unrelated `Tier` types | `config.py:Tier` renamed to `ModelTier`; it had no importers outside `config.py`. `kernel/spec.py:Tier` keeps the name. |

## 3. Deviations from the plan, with reasons

Three, all deliberate.

### 3.1 Legacy `default_mode` does **not** lift to `orchestrator.ceiling`

The plan's migration table (§1.1, and BINDING §9) says legacy `default_mode`
becomes "`orchestrator.ceiling` **and** the session's starting mode". Doing
that literally locks every existing preset out of Shift+Tab: a preset saying
`default_mode: "ask"` would become a session that can never reach auto-edit.

BINDING §2.2's own role table settles it the other way — for
`role="orchestrator"`, `ceiling` is *"the **starting** mode, live-adjustable up
to the ceiling"*, which only makes sense if the two are different values. So:

- `Preset.default_mode` stays a real field and remains the **starting** mode.
- `orchestrator.ceiling` is a new, **opt-in** key and is the cap. `set_mode`
  refuses anything above it (plan §1.5's requirement, now real).
- An orchestrator with no stated ceiling seeds at `yolo`, i.e. uncapped. The
  `Composition.ceiling` default of `ask` is right for subagents (it is
  `mode_cap`) and would be a silent session-wide cap if applied at depth 0.

### 3.2 Preset section bodies now win over plugin section bodies

`manager.open()` previously composed
`{**preset.prompt_overrides, **prompt_overrides(cwd)}` — the plugin state won.
The plan's layer table puts `preset` at layer 3, above `user` (1) and `project`
(2), so the preset now wins. This only bites a user who sets the same section's
body in both `plugins.prompt.X.settings.body` and `presets.Y.prompt_overrides`;
the layer table is authoritative and the provenance chain now names which one
was used, which is the point.

### 3.3 The delegation pair reaches the orchestrator by spawns, not by pattern

Invariant 10 — "the delegation pair is granted by depth, never by allowlist" —
is now applied at depth 0 too: `agent`/`send_message` are in the orchestrator's
resolved tools whenever its `spawns` is non-empty, regardless of whether the
tool patterns name them.

Visible consequence: the built-in **`explore` preset gains the `agent` tool**.
Today it declares `agents: ["explore"]` and `tools: ["read","glob","grep"]`, so
`select_tools` drops the delegation pair while `render_system_prompt` still
receives `orchestration=True` — the prompt promises delegation the model has no
tool for. The new behaviour resolves that inconsistency in the direction the
prompt already claimed. `minimal` (spawns `[]`) still gets no delegation, and
`standard` is unchanged.

## 4. `AgentDef.max_turns`: wired, and as what

**Wired**, as the child's **delegation budget**: one turn for the spawn, one per
`send_message` resume. `resume_subagent` refuses past it with a message telling
the model to spawn a fresh agent. State lives in `SubagentDeps.turns` /
`.budgets`, shared down the tree like `counter`/`roster`.

Deleting it was the alternative and was rejected: `Composition.max_turns` is in
the plan's object model, the settings UI already renders the knob, and BINDING
§2.2 defines it for a subagent as "the child's budget, **enforced per
delegation**" — which is exactly this.

It is **not** a per-round budget. Capping tool rounds means overriding
`core/loop.py:MAX_ROUNDS` per agent, and neither `core/loop.py` nor
`core/agent.py` is owned by this phase. If a round budget is wanted, the change
is one parameter threaded from `AgentInstance` into `loop.run_turn`.

Per the role discriminator, `max_turns` is a no-op on `@orchestrator`: nothing
reads `Resolved.max_turns` for a conversation. The UI should grey it out rather
than pretend.

## 5. Not done in Phase 1

Each of these is in the plan's Phase 1 but outside the brief this phase was
given, and none of them is depended on by what landed.

- **The subagent prompt rewrite (§1.14).** `prompts/subagent.py` is still a
  single format string; `prompts/sections.py` did not gain `role`/`agent_id`.
  `Resolved.sections` is computed and carried but nothing consumes it yet.
  Consequence: `applies_to: [subagents]` is still inexpressible, and a section
  binding to `@subagents` currently reaches only `Resolved.section_bodies`,
  where a subagent's prompt renderer does not read it. The golden-file guard
  the plan asks for (byte-identical `explore`/`general` prompts before and
  after) should be captured before that rewrite starts.
- **Advisory conflicts as the child's first-turn `<system-reminder>`** and the
  `binding_narrowed` bus event. `Resolved.advisories()` exists and returns
  them; nothing emits them yet. Adding the reminder changes the child's prompt
  bytes, so it wants the golden files from the item above first.
- **`kernel/bootstrap.py` enrichment** — `agent_specs` still carries the
  definition's declared metadata rather than resolved metadata. Harmless; it is
  a Phase 2/4 display concern.
- **Bases across presets.** `Composition.base` resolves against the agent
  *definitions* dict (`_expand_bases`), which covers the plan's
  `implement: {base: "general"}` example. A base naming another *preset's*
  per-agent composition is not resolved and is silently ignored.

## 6. What Phase 4 will depend on in `server/manager.py`

The pieces Phase 4's `/resolved` and `/preview` routes need, all already
present:

- `Conversation.resolved: Resolved` — the session's frozen composition. This is
  what `?conv=<id>` must read, and `resolved.digest()` is what it compares
  against a live re-resolve to detect drift.
- The session's `composition` **meta record**, written by `store.append_meta(
  composition=resolved.to_json())` for new sessions only. `session/store.py` was
  not touched, as the plan predicted. `Resolved.from_json()` rebuilds it and
  returns `None` (rather than raising) for anything unusable, so a session with
  a corrupt record falls back to re-resolving instead of failing to open.
- `ConversationManager._frozen_composition(store, resuming)` — the resume rule
  in one place: a recorded composition is used as-is, a session without one
  re-resolves exactly as it did before this phase, including the fallback to
  `standard` when the preset is gone.
- `ConversationManager._resolve_role(spec)` — role-to-slug, passed to
  `resolve_composition(resolve_model=...)` so `model_outside_set` can be checked
  against both the role name and the slug it resolves to. `/preview` should pass
  the same callable or model policy checks will differ between preview and
  runner.
- The subagent deps carry everything a preview needs to reproduce a child
  exactly: `deps.pool` (the session pool), `deps.parent` (the orchestrator's
  `Resolved`), `deps.defs` (the frozen definition snapshot) and `deps.preset`.
  A preview of "agent X under this session" is
  `resolve_composition(X, pool=deps.pool, preset=deps.preset, defs=deps.defs,
  cwd=..., parent=deps.parent, depth=0)` — the same call the runner makes, not a
  reconstruction.

`server/manager.py` also stopped calling `preset_module.select_tools()`. The
function is kept and still works (it now reads `preset.spawns`), because the
settings UI can answer "what would this preset grant" without a definitions
snapshot. Delete it once Phase 4's `/resolved` covers that.

## 7. Verification

`uv run pytest tests/ -q` — 171 passed (160 pre-existing, 11 new in
`tests/test_composition.py`). `uv run ruff check quickcode/ tests/` — clean.

No test drives a real provider: `tests/test_composition.py` uses a fake that
raises on `stream_chat`, and the one spawn test uses a scripted local
generator.

**Not verified, because it needs a live model turn** — worth folding into a
single cheap smoke test:

1. A real session under a delegation-only preset actually spawning a `general`
   subagent that writes a file. The resolver half is asserted
   (`test_a_delegation_only_orchestrator_still_hands_its_children_the_pool`),
   but the child's registry is built inside `spawn_subagent` and only a real
   spawn exercises `build_registry(list(resolved.tools), ...)` end to end.
2. `set_mode` refusing a mode above a preset's `ceiling` over the WebSocket
   (the error-event shape is the same one `yolo` already uses).
3. `set_model` re-rendering the system prompt from the frozen section bodies —
   the assertion is that the emitted `system_prompt` event still contains a
   preset's overridden section after the preset file is edited mid-session.
