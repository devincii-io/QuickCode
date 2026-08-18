# Phase 2 handoff — the explanation layer

Phase 2 of `PLAN-AUTHORING-AND-COMPOSITION.md` landed in `kernel/spec.py` and
`kernel/manifest.py` only. Everything below needs a file this phase did not
own. Nothing here is blocking: the payload exists and is complete, it is simply
not serialised or rendered yet.

---

## 1. `kernel/registry.py` — emit the new fields (owner: kernel)

`plugin_json()` and the settings list inside it still emit the pre-Phase-2
shape, so the prose is invisible to the frontend. Two additions, both purely
additive:

```python
# in plugin_json(), alongside "description":
"summary": spec.summary,
"affects": list(spec.affects),
"audience": spec.audience,
"consequence": spec.consequence,
"locked_because": spec.locked_because,
"recourse": _recourse_json(spec.recourse),
"docs_anchor": spec.docs_anchor,

# in the per-setting dict, alongside "risk":
"affects": list(s.affects),
"effect_detail": s.effect_detail,
"example": s.example,
"locked_because": spec.locked_because_for(s),
"recourse": _recourse_json(spec.recourse_for(s)),
```

```python
def _recourse_json(r):
    return None if r is None else {"action": r.action, "label": r.label, "target": r.target}
```

**Use the two helpers, not the raw fields, for the per-setting pair.**
`PluginSpec.locked_because_for(setting)` and `recourse_for(setting)` implement
the documented fallback to the plugin's own values. Most settings deliberately
carry no reason of their own; reading `setting.locked_because` directly gives
an empty string and a dead end in the UI.

## 2. Frontend rendering (owner: frontend)

The six questions render in this fixed order, everywhere, with no reordering
per kind — the order is the affordance:

```
WHAT        summary
AFFECTS     affects            (chips)
WHO         audience
IF CHANGED  consequence        (all tiers, neutral)
WHY FIXED   locked_because     (locked only)
INSTEAD     recourse           (locked only, a real button)
```

Tier rule, from `UX.md` §4.2, unchanged by this phase:

- `free` → `consequence` in the neutral slot.
- `confirm` → `consequence` neutral **and** `risk` amber. They are different
  sentences and both are written; do not fall back from one to the other.
- `locked` → `consequence` neutral, `locked_because` in the Fixed-by-design
  block, `recourse` as the action line.

`recourse.action` is one of `duplicate | author | settings | docs | none`.
`target` is a plugin id for `duplicate`/`settings`, a kind slug for `author`,
a doc path for `docs`.

## 3. Defect found while writing: six knobs are rendered and never read

Verified against the tree. Each of these settings exists in `manifest.py`, is
persisted by the registry, and is consulted by nothing on the runtime path.
The runtime uses a module constant instead:

| setting | what actually decides it |
|---|---|
| `runtime.agent_loop.max_rounds` | `core/loop.py:38 MAX_ROUNDS = 50` |
| `runtime.compaction.enabled` | nothing — `manager.py:323` calls `should_compact` unconditionally |
| `runtime.compaction.threshold` | `core/compact.py:20 COMPACT_RATIO = 0.8` |
| `runtime.compaction.keep_turns` | `core/compact.py:56` default `keep_turns=2` |
| `runtime.subagents.max_depth` | `subagents/runner.py:38 MAX_DEPTH = 2` |
| `runtime.subagents.max_agents` | `subagents/runner.py:39 MAX_AGENTS = 50` |

This is the same class of defect as §0.1–§0.3 in the plan (`enabled`,
`default_mode`, `max_turns`), all three of which Phase 1 fixed. These six are
the remainder. `state_store.plugin_setting(cwd, plugin_id, key)` already exists
and `resolve.py:678` already uses it for `default_mode`, so each is a one-line
read at the call site.

**How the prose handles it in the meantime.** Every `effect_detail` describes
the mechanism the number governs — what a round is, where the cut is made,
what happens at the depth limit — and none of them claims that editing the
value takes effect. Once the wiring lands nothing here needs rewriting.

## 4. Two facts the prose asserts that will change under Phase 1 §1.14

- **`audience` on all 13 prompt sections is `"orchestrator"`**, set from
  `_SECTION_AUDIENCE` in `manifest.py`. That is true today because
  `prompts/subagent.py` is still a format string and no section can reach a
  subagent. When `render_subagent_prompt` is recomposed over
  `sections.compose()`, flip the ones that reach subagents to `"all_agents"`.
  It is a single constant plus per-row overrides.
- **`prompt.orchestration`, `prompt.send_message_hint`, `prompt.plan_mode` and
  `prompt.headless` say their replacement body is used unconditionally.** That
  is `sections.py:58-62`: `PromptSection.body()` returns an override verbatim
  without consulting the render function, so a user-written orchestration
  section appears even in a session that cannot spawn. If that is ever
  considered a bug rather than a documented quirk, three sentences in
  `_SECTION_PROSE` come out with it.

## 5. Smaller corrections made inside `manifest.py`, for the record

- `runtime.permissions.protect_outside_root.help` claimed paths outside the
  project are "denied regardless of mode". `core/permissions.py:240-243`
  returns `ask`, and `deny` only in `dontask` mode. Corrected.
- `prompt.project_instructions` and `prompt.environment` had
  `tier_hint="free"`/`"locked"` taken straight from the section's declared
  tier, while their only setting is locked because they are generated. The
  hint is now `section.tier if editable else "locked"`, so the badge matches
  the only knob the card has. `required=not editable` was already computed
  that way; this makes `tier()` agree with it.
- No plugin of kind `panel` is produced anywhere in the tree. Eight of the
  nine `Kind` members have at least one live instance; `panel` has none.
