# Plan — everything is a plugin, and a UI worth looking at

Companion to `REDESIGN-REQUIREMENTS.md` (the what). This is the how, in the
order it gets built. Written against the actual code, with the real file and
symbol names.

---

## Part A — where we actually stand

**Already plugin-shaped:** providers (`Provider` Protocol + `quickcode.providers`
entry points, `plugins/loader.py:46`), tools (`quickcode.tools` entry points,
`plugins/loader.py:29`), MCP servers (data-driven from `.quickcode/settings.json`,
`plugins/mcp.py:189`). These are the two models to generalize from.

**Not pluggable at all:** the agent loop, the permission engine, prompt
composition, subagent orchestration, the event protocol, session storage.

**The five hard-coded knots that block everything** (each must be untied before
the UI can honestly claim "all internals are plugins"):

1. *Tool identity is known by name in four places.* `permissions.py:54-55`
   (`MUTATING_TOOLS`/`READONLY_TOOLS`), `permissions.py:165` (`_protected()`
   only fires for `read`/`write`/`edit`), `loop.py:279-282` (`_match_target`
   hardcodes bash→`command`, else `file_path`/`path`), `manager.py:190`
   (`ev.name.startswith("task_")`). A plugin tool that writes files gets none
   of the protection a builtin gets.
2. *Three separate enumerations of "the tools that exist":*
   `registry.py:34-43` (`_core_tool_factories`), `registry.py:46-62`
   (`default_registry`), `definitions.py:18` (`ALL_TOOLS`, seven names —
   which is why a subagent can never be granted an MCP tool).
3. *Plan mode is welded into the loop:* `loop.py:91-97` hides `write`/`edit`
   by mode, `loop.py:224` intercepts `call.name == "plan"`.
4. *The system prompt is one format string* with `if` flags
   (`prompts/system.py:132-170`) — no section registry, no per-section
   provenance, nothing the UI can show or a plugin can extend.
5. *The wire protocol is closed:* `serialization.py:37-93` (`event_to_json`,
   `LOGGED_TYPES`) and `app.py:439-475` (`_dispatch` if/elif) — a plugin
   cannot emit a new event or accept a new client message.

---

## Part B — the plugin model

### B1. What a plugin is

```
Plugin
  id            stable slug            "tool.bash", "prompt.tone", "agent.explore"
  kind          tool | prompt_section | provider | agent | mcp_server
                | policy | hook | panel | storage
  title, description, group
  source        internal | entrypoint | config
  required      bool      internal ones that cannot be disabled
  enabled       bool
  settings      SettingSpec[]          each with its own tier
  view          how to show the raw truth (text / json / python source ref)
```

### B2. The three tiers (per *setting*, not per plugin)

| tier | meaning | UI |
|---|---|---|
| `free` | change it, nothing asks | inline edit |
| `confirm` | changeable, but changing it changes agent behaviour in ways that break things | edit opens a confirm dialog naming the risk |
| `locked` | not changeable, ever | read-only, with the full value shown |

**Everything is viewable at every tier.** `locked` means "you cannot edit
this", never "you cannot see this".

Concrete assignment (this is the contract, not an example):

- **locked** — the tool-call protocol (how tools are declared to the model and
  how calls come back: `ToolSchema` emission, `additionalProperties=False`,
  `tool_call_id` round-trip), the `<tool_use_policy>` prompt section, the
  event-log record format, protected-path denial, the loopback auth token,
  the subagent report sanitizer (`runner.py:114`).
- **confirm** — `<identity>`, `<autonomy>`, `<verification>`, orchestration
  playbook, plan-mode block, permission defaults, `MAX_ROUNDS`, read-only
  parallelism, compaction threshold, subagent depth/count caps.
- **free** — `<tone_and_style>`, `<conventions>`, `<task_management>`,
  project instructions, model selection, subagent prompt bodies, every UI
  preference, custom user-authored sections and agents.

### B3. Kernel

New package `quickcode/kernel/`:

- `spec.py` — the dataclasses above, `SettingSpec{key, type, default, tier, help}`.
- `registry.py` — `PluginRegistry`: discovery (internal manifest + entry
  points + config), `by_kind()`, `get(id)`, enable/disable, settings
  read/write with tier enforcement (a `locked` write raises, a `confirm`
  write requires an explicit `confirmed=True`).
- `manifest.py` — the internal plugin manifest: every shipped capability
  declared as a plugin, so the list the UI shows *is* the list the runtime
  uses. No parallel truth.
- `preset.py` — `Preset{id, title, plugins[], settings_overrides}`, the
  "plugin composition one session's agent runs". Built-ins: **Standard**,
  **Minimal** (read/edit/bash only), **Explore** (read-only), **Custom**.
  A session records the preset it started with and keeps it.

---

## Part C — implementation phases

Each phase ends with something that runs. No tests until a phase works.

**Status:** 0–7 ✅ (kernel, tools, prompt, hooks, agents, presets, protocol)
· 8 and 10 in progress (settings/plugin UI, trajectory rewrite)
· 11 ✅ (chat steps + measured status bar) · 12 ✅ (multi-agent columns,
windowed transcripts) · 9 pending (shared inspector, needs 10)
· 13 ✅ (release tooling, version 2.0.0, not yet tagged).

### Phase 0 — make the current build honest *(in flight)*
- Native app window actually appears under `python.exe` and `pythonw.exe`.
- Reopening a session shows its history instead of an empty pane.
- Rebuild the installer so what's installed is what's in the tree.

### Phase 1 — kernel, with nothing behind it yet
`quickcode/kernel/{spec,registry,manifest,preset}.py`. Every existing
capability gets a manifest entry describing what it already is. Registry is
readable via a new `GET /api/plugins` v2 payload. Behaviour unchanged — this
phase only makes the truth enumerable.

### Phase 2 — tools declare themselves
- `Tool.permission = PermissionSpec{mutates, target_field, protected_paths}`
  on `tools/base.py`; every builtin declares it.
- Delete `MUTATING_TOOLS`/`READONLY_TOOLS`, `_protected()`'s name check, and
  `_match_target`'s branching — all read from the spec.
- Collapse `_core_tool_factories`, `default_registry` and `ALL_TOOLS` into
  one registry query. Subagent `tools:` resolves against the **live** registry
  with glob support (`mcp__*`, `task_*`), so plugin and MCP tools become
  grantable.
- Tool source becomes a real field, not `startswith("mcp__")` guesswork.

### Phase 3 — the prompt is a plugin
- `prompts/sections.py`: `PromptSection{id, order, tier, render(ctx)}`.
  `render_system_prompt` becomes "sort sections, render, join" — byte-stable
  within a session (the cache breakpoint in `history.py:16` depends on it).
- Sections carry their tier; user overrides live in config, not in code.
- `GET /api/prompt` returns the composed prompt **with section boundaries**,
  so the UI can show which plugin produced which bytes.

### Phase 4 — loop hooks
- `LoopHook` plugin kind: `before_request`, `after_stream`, `before_tool`,
  `after_tool`, `round_done`.
- Plan mode stops being an `if name == "plan"` (`loop.py:224`) and becomes an
  interceptor plugin; mode-based tool hiding becomes a `before_request` hook;
  the task-panel refresh stops sniffing `task_` prefixes.

### Phase 5 — agents are plugins
- `AgentDef` → agent plugin with a model policy:
  `models: {allow: [glob…], default: role|slug, selectable: bool}` — restrict
  an agent to specific models, or offer a per-agent selection.
- `explore` and `general` become internal agent plugins; user agents in
  `.quickcode/agents/*.md` are the same kind, different source.
- Depth/count caps move from constants (`runner.py:34-35`) into settings.

### Phase 6 — presets
Preset resolution at session open; the preset is recorded in the session meta
and honoured on resume. Settings shows built-in presets, duplicate-to-customise,
and which one each running session is on.

### Phase 7 — open the protocol
- `serialization.py` becomes a registry: event type → serializer + logged flag.
- `app.py:_dispatch` becomes a handler table plugins can register into.
- Unknown event types survive a round trip instead of being dropped.

### Phase 8 — settings + plugin UI *(replaces the current flat tool list)*
- Sections: General · Models · **Plugins** · **Agents & Presets** · Appearance.
- **Plugin configuration** — grouped cards by concern (Shell, Agent loop,
  Prompt, Web/MCP, Session), each expanding to a generated form driven by
  `SettingSpec`, with a tier badge per field and the confirm dialog wired in.
- **Plugin list** — searchable, filterable by kind/source/state, enable
  toggles, scales to hundreds of entries.
- Every plugin has a **View** affordance showing the raw truth (prompt text,
  tool JSON schema, MCP definition, agent definition) even when locked.

### Phase 9 — parsed views everywhere
The Summary/Payload/Result/Timing inspector (`trajectory.js:254-294`) becomes
a shared component, reachable from a chat message, a tool card, an agent call,
and the system prompt — not just the trajectory table.

### Phase 10 — trajectory rewrite
`trajectory.js` today is index-ordered 14px divs with `title` tooltips
(`trajectory.js:200-210`). Replace with:
- a real **time axis** (wall-clock, `ev.ts`), zoom + pan;
- **lanes**: Input · Model · Tools · Agents, with duration bars per event;
- a **playhead and hover crosshair** with a rich hover card: kind, start → end,
  total ms — no dots;
- the event table below, aligned and selection-synced to the lanes;
- windowed rendering so a 10k-event session stays smooth;
- follow-live keeps working (`setFollowing`, `trajectory.js:159`).

### Phase 11 — chat rendering
Group consecutive tool calls into titled steps, collapsible IN/OUT with
syntax highlighting, file links on reads, errors inline in red, and a real
status bar (turns · steps · LLM time · tool time · TTFT · tok/s · cache hit ·
in/out tokens) fed from the existing `usage` events and `Ledger`.

### Phase 12 — multi-agent panel + performance
Parallel agents as their own columns/lanes with independent scroll, plus the
structural fix: virtualize the transcript, the trajectory table and the agent
roster, and make streaming markdown incremental instead of re-parsing the
whole buffer per delta (`chat.js:53-62`, currently O(n²)).

### Phase 13 — repackage
Rebuild `packaging/quickcode.iss`, bump the version, verify the installed app
matches the tree.

---

## Part D — rules for this work

- One phase at a time, in order. A phase lands working before the next starts.
- No shortcuts, no stubs left behind, no "TODO later" in shipped paths.
- Tests only after a phase actually runs, and only where they earn their keep.
- The plugin list the UI shows must be the list the runtime uses — if those
  two ever diverge, the feature is a lie.
