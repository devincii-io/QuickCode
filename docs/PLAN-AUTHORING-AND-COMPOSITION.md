# Plan — authoring and composition

One implementable plan reconciling `design/AUTHORING.md`, `design/BINDING.md`
and `design/UX.md`. Those three were written independently and disagree in
about a dozen places. This document resolves each disagreement, states the
final object model, and orders the work. Where it contradicts a design doc,
this document wins; the design docs stay as the reasoning behind the choice.

Written for an implementer who has not read the three sources. Every claim is
grounded in the current tree.

**Already done, do not re-plan.** `SubagentDeps.child()` takes `tool_pool=`
and `spawn_subagent` passes `tool_pool=list(registry.tools.values())` — the
child's own granted tools — down to the grandchild (`subagents/runner.py:82`,
`:103`, `:218`). BINDING item 7, the privilege-escalation bug where an
`explore` agent granted `read/glob/grep` handed a spawned `general` the full
session pool, is fixed and verified; 144 tests pass. Phase 1 builds on that
fix and adds the one thing it does not yet have: a depth-0 carve-out (§1.13).

---

## 0. Where the code actually is

Facts the plan depends on, verified in the tree:

- `kernel/spec.py` — `PluginSpec`, `SettingSpec`, `PluginView`, `Tier`
  (`free|confirm|locked`), `Kind` (9 members), `Source`
  (`internal|entrypoint|config`). `locked` is documented as "you cannot edit
  this, never you cannot see this" (`spec.py:17`).
- `kernel/manifest.py` (453 lines) — 8 hand-written core `PluginSpec`s
  (`runtime.tool_protocol`, `runtime.agent_loop`, `runtime.compaction`,
  `runtime.permissions`, `runtime.subagents`, `hook.plan_mode`,
  `runtime.session_log`, `prompt.system`) carrying 14 hand-written
  `SettingSpec`s, plus five *generators*: `prompt_section_specs`,
  `tool_specs`, `agent_specs`, `provider_specs`, `mcp_specs`, contributing 7
  more `SettingSpec` templates (1 + 1 + 5). Generated specs' settings are
  written once per generator, not per instance. 21 plugins are statically
  declared (8 core + 13 sections); tools, agents, providers and MCP servers
  are derived from live objects at build time.
- `prompts/sections.py` — 13 `PromptSection`s at orders 10–130
  (`identity, tone, autonomy, conventions, task_management, tool_use_policy,
  verification, environment, project_instructions, orchestration,
  send_message_hint, plan_mode, headless`). Joined with `"\n\n"` in `order`;
  empty sections dropped. Byte-stability is the cache breakpoint.
- `prompts/subagent.py` — a single format string. No section can reach a
  subagent today.
- `kernel/preset.py` — `Preset{id,title,description,builtin,base,tools,
  agents,prompt_overrides,settings,default_mode}`; `select_tools(preset,pool)`.
  `resolve()` never raises; a missing preset falls back to `standard`.
- `subagents/definitions.py` — `AgentDef{name,description,tools,model,models,
  model_selectable,mode_cap,max_turns,color,skip_project_instructions,
  prompt_body}`. `_split_frontmatter` is a 15-line `key: value` reader.
- `tools/registry.py:78 select()` — names, aliases, `fnmatchcase` globs. A
  pattern matching nothing is silently empty. `build_registry(None, pool=)`
  inherits the pool; `agent`/`send_message` are stripped and re-added by
  depth, never by allowlist (`registry.py:117`). `plan` is never included.
- `core/permissions.py` — `Mode{plan,ask,auto_edit,dontask,yolo}`,
  `PermissionSpec{mutates,target_field,path_target,shell}`, `DEFAULT_SPEC`
  is mutating-and-prompted. `registry_specs()` is a process-level cache built
  from `default_registry()`.
- `session/store.py` — JSONL, append-only, `{"kind":"meta",...}` records
  merged later-wins by `store.meta()`; `{"kind":"event","seq":n,...}` with a
  monotonic `seq`. `append_meta(**fields)` accepts arbitrary keys. **This is
  why Phase 1 needs no change to `store.py` at all.**
- `server/app.py` (819 lines) — existing routes below in §3. Every public
  route already has a `/api/projects/{pid}/…` twin. **Routes can be
  registered from another module**: `server/gitinfo.py:123
  register_git_routes(app, hub)` already does exactly that for
  `/api/git/status` and `/api/git/diff`. This is the precedent Phase 4 uses
  to reduce its `app.py` diff to one line. `app.py` also already exposes
  `register_event(...)` and `register_client_message(kind, handler)` as
  extension seams.

Five live defects the surveys turned up, each adjacent to this work and
each fixed as part of the phase that touches it:

1. **`plugins.<id>.enabled` is persisted and rendered but never enforced on
   the tool pool.** `PluginRegistry.is_enabled()` exists and the UI writes the
   flag, but `plugins/loader.load_tool_plugins()` and
   `manager.registry_factory()` never consult it. §1.3 defines `enabled` as
   the session-wide revoke, so Phase 1 must make the flag real or the whole
   concept is decorative.
2. **`default_mode` exists twice** — as `Config.default_mode` in
   `~/.quickcode/config.json` and as the `runtime.permissions.default_mode`
   plugin setting in `settings.json`. `manager.open()` reads only the first,
   so the plugin knob is shown and ignored. Phase 1 resolves it to one value
   (the plugin setting, falling back to the config value).
3. **`AgentDef.max_turns` is stored and rendered but read nowhere in
   `runner.py`.** Another knob that does nothing. Phase 1 wires it, since
   `Composition.max_turns` is being introduced anyway.
4. **`settings.local.json` is invisible to `kernel/state.py`.** Permissions
   and MCP read it; plugin and preset state do not. A plugin setting placed
   there is silently ignored. Decision: leave the asymmetry (the local file is
   for accreted "always allow" rules, not configuration) and emit an `info`
   `Problem` when a `plugins` or `presets` key is found there.
5. **`active_preset_id()` reads project-then-user, the reverse of
   `load_presets()`'s user-then-project.** Both are defensible in isolation
   and the combination is correct — the most specific file names the active
   preset, and the most specific file wins when defining it — but it is
   undocumented and must be stated in the layer table, not rediscovered.

One naming hazard: **there are two unrelated `Tier` types.**
`kernel/spec.py:Tier` is mutability (`free|confirm|locked`); `config.py:83
Tier = str` is model cost (`quality|balanced|cheap`, used by
`CatalogEntry.tier` and `Profile.models_for`). Phase 1 and Phase 2 both touch
modules that import one or the other. Never import both unqualified into one
file.

---

## 1. Conflict ledger

Fifteen disagreements, each resolved with a reason.

### 1.1 One object model, one name

UX §2.2 says a preset and an agent definition "are the same object". BINDING
§2.1 says a preset is "the orchestrator's composition plus the compositions
available to spawn". AUTHORING §1.5 makes `preset` one of five authorable
markdown kinds. Three incompatible claims.

**Decision — two types, and `Preset` keeps its name.**

- **`Composition`** — the configuration of *one* agent: tools, spawns, prompt
  sections and bodies, model policy, ceiling, limits. BINDING's term, and
  load-bearing in the resolver.
- **`AgentDef`** — an *identity* (`id`, `title`, `description`, `role`,
  `source`, `path`) owning exactly one `Composition`. UX's "same object" claim
  is true at the level of the editor and only there: the workbench edits a
  `Composition` and both entry points reach one. That simplification survives
  intact.
- **`Preset`** — a named set `{orchestrator: Composition, agents: {id:
  Composition}, bindings: [Binding]}`, keeping the name in the UI, on the wire
  and on disk.

UX's rename of presets to "Compositions" is **rejected**: one word cannot mean
both "one agent's config" and "a set of agents' configs", and the kernel needs
it for the first. `presets` and `active_preset` are already on disk in users'
settings files and in session meta records; a nav label is cheaper to change.

AUTHORING's `kind: preset` markdown file is **rejected**. AUTHORING's own case
for markdown (§2.1) is that every authorable kind "has a large free-text
payload at its heart"; a preset has none — it is a reference object naming
agents and patterns, with a two-sentence description. It stays in
`settings.json` beside `active_preset`, which is where the session records it.
Authorable kinds drop from five to four here, and to three in §1.15.

**On-disk shape** (`~/.quickcode/settings.json` and
`<cwd>/.quickcode/settings.json`, same schema, project shadows user):

```json
{
  "active_preset": "delegator",
  "presets": {
    "delegator": {
      "title": "Delegator",
      "description": "Plans and delegates. Cannot touch files itself.",
      "base": "standard",
      "orchestrator": {
        "tools": ["read", "glob", "grep", "task_*", "agent", "send_message", "plan"],
        "spawns": ["explore", "implement"],
        "models": ["orchestrator"],
        "ceiling": "auto-edit",
        "section_bodies": {"prompt.autonomy": "You do not edit files yourself…"}
      },
      "agents": {
        "explore": {},
        "implement": {"base": "general", "tools": ["read","write","edit","bash"],
                      "models": ["worker"], "ceiling": "auto-edit", "max_turns": 40}
      },
      "bindings": [{"plugin": "prompt.verification", "to": "agent:implement"}]
    }
  },
  "plugins": {"tool.bash": {"enabled": true, "settings": {"timeout_s": 120}}},
  "permissions": {"…": "unchanged"},
  "mcpServers": {"…": "unchanged"}
}
```

Legacy preset bodies (no `orchestrator` key) are lifted per BINDING §9:
`tools → orchestrator.tools`, `agents → orchestrator.spawns`,
`prompt_overrides → orchestrator.section_bodies`, `default_mode →
orchestrator.ceiling` and the session's starting mode, `base →
orchestrator.base`, `settings` stays at preset level.

One trap: legacy `agents` is a **list of names**, new `agents` is a **dict of
compositions**. Discriminate by type at parse time. A `list` means legacy and
lifts to `orchestrator.spawns`; a `dict` means the new shape.

`prompt_overrides` lifts to `@orchestrator`, never `@all`. Lifting it to every
agent would silently rewrite every existing preset's subagent prompts. The new
reach is opt-in.

### 1.2 Where bindings physically live

AUTHORING wants markdown files in `.quickcode/plugins/`. BINDING wants
`Binding` records that are "authored and diffable". The brief asks whether a
binding belongs in the plugin's frontmatter, in the composition, or in a third
file.

**Decision — bindings live only in `presets.<id>.bindings`. No binding file,
no binding in a plugin's frontmatter, with one narrow exception.**

A binding is a statement about a *relationship* and neither end owns it.
Frontmatter would let a plugin declare its own reach — the same category error
AUTHORING §1.6 refuses for `source` ("provenance is the one thing a plugin
cannot be trusted to say about itself"), and a cloned repository must not ship
a tool that attaches itself to your orchestrator. Writing it into each
composition duplicates one `@subagents` fact N times. A third file adds a
fifth layer to a table that already has four.

**Bindings are sugar over composition edits.** The resolver desugars each into
contributions to compositions before resolution runs. `Binding` survives as a
type because it is the diffable one-row-per-statement form, and because
`@subagents` cannot be expressed by enumerating agents that may not exist on
another machine.

**The exception: `applies_to` on a `kind: prompt` file**, normalised into a
`Binding` at load with provenance pointing at the file. Allowed because
BINDING §5 already establishes that prompt sections are *not* capabilities —
adding one grants nothing — and because requiring a `settings.json` edit
before an authored section does anything would kill the "write a file, it
works" property that is the point of authoring. No other kind gets one: a
`kind: tool` file never says which agents get it.

### 1.3 `enabled` vs selectors vs the tool picker

AUTHORING keeps per-plugin `enabled` in `settings.json`. BINDING introduces a
`@session` selector for pool admission. UX has a per-agent tool picker editing
patterns. Three concepts for what is nearly two.

**Decision — collapse to two, and define `enabled` as the third's only
surface.**

1. **Pool admission.** `plugins.<id>.enabled = false` removes the plugin from
   the pool entirely — every agent, every depth. This *is* BINDING's
   `@session` revoke ("to restrict the session, revoke at `@session`",
   BINDING §7). So: **`enabled: false` is defined as the `@session` revoke,
   and the toggle is its entire authoring surface.**
   This is currently a lie: the flag is written by the UI and read by nobody
   on the tool path (§0 defect 1). Phase 1 makes `manager.registry_factory()`
   filter the pool by `registry.is_enabled(f"tool.{t.name}")` before the
   preset selection runs. Until that lands, the toggle is decoration and this
   decision has no teeth.
2. **Grant.** Membership in one agent's `Composition.tools` / `sections` /
   `spawns`. Edited by the tool picker, stored as patterns in a preset.
3. There is no third concept.

Consequence: `@session` survives as an internal resolver layer (layer 0's seed
set is filtered by `enabled`) but **is not an author-facing selector**. That
deletes it from the selector table, and with it the five "meaningless —
refused" rows in BINDING §3 that only existed to say what `@session` does not
mean.

`agents:<a>,<b>` is also cut: it is `@all` with a list, and two bindings say
the same thing more legibly with better provenance.

**Four selectors survive:** `@orchestrator`, `agent:<id>`, `@subagents`,
`@all`.

### 1.4 One provenance shape, one problem shape

BINDING's response carries `provenance{layer,source,path,rule,via_bundle}`
plus ad-hoc `reason`, `overridden`, `previous_layer`, `narrowed_by` and
`resolved_via` depending on which field it decorates. UX wants a `←` column
and a five-slot chain strip. AUTHORING has a separate problem shape
`{plugin_id,kind,scope,path,line,field,code,severity,message,fix}`.

**Decision — one `Provenance`, one `Problem`, both flat, both with closed
vocabularies. See §2 for the dataclasses.**

`Provenance` loses BINDING's ad-hoc extras because a *chain* subsumes them:
`Resolved.chain[field]` is a `list[Provenance]`, last entry winning.
"Overridden" is `len(chain) > 1`; "previous_layer" is `chain[-2].layer`;
`narrowed_by`, `reason` and `resolved_via` become `rule` plus one free-text
`note`. UX's five-slot strip is a renderer over `chain`, not a new type.

`Problem` absorbs AUTHORING's validation problems **and** BINDING's resolution
conflicts — they are the same thing at two different times: something the user
wrote does not do what they think. One red card, one badge, one endpoint, one
renderer. BINDING's `blocking|advisory` maps to `severity` `error|warning`;
AUTHORING's `plugin_id`/`kind`/`scope`/`path` collapse into `subject` plus the
shared `Provenance`. Largest single de-duplication in the ledger.

### 1.5 Reload and freeze semantics

AUTHORING §6.1 gives a per-kind table (`tool` → next session, `agent` → next
spawn, `mcp` → project reopen…). BINDING §11 gives seven collisions with the
frozen-composition invariant. UX wants a live preview.

**Decision — one sentence: definition is live, resolution is frozen per
session, preview resolves live and says so.**

A session freezes exactly one thing at open: the `Resolved` composition for
`@orchestrator`, plus a snapshot of the agent-definitions dict, written into a
`composition` meta record with a `digest`. Every value a running conversation
depends on — tool list, section bodies, ceiling, spawnable agents, subagent
definitions — is read from that record and nowhere else.

AUTHORING's per-kind table therefore collapses to one row: **everything takes
effect at the next session open.** The row that changes is `agent`: AUTHORING
said "next spawn", BINDING §11.5 said "snapshot at open". **BINDING wins** —
"next spawn" changes an agent's behaviour mid-conversation, the same lie the
preset freeze exists to prevent, and worse for subagents because the parent
was already told the agent roster in the `agent` tool's schema. The agent
editor says "takes effect in new sessions".

MCP is the one exception and both docs agree: server processes are per
project, not per session. `POST /api/projects/{pid}/mcp/reload` respawns them;
running sessions keep their frozen tool lists regardless.

Live preview: `/resolved` without `?conv=` resolves against current settings
(`"frozen": false`); with `?conv=` it reads the session's frozen record
(`"frozen": true`). Both render distinctly. The same flag fixes AUTHORING
§8.1's worry that `GET /api/prompt` would start lying about running sessions.

Drift: AUTHORING's per-authored-plugin content hashes are **cut** for
BINDING's single `digest` — one hash per session, compared against a live
re-resolve, answers the same question for a fraction of the bookkeeping.

Three BINDING §11 decisions become real behaviour changes and land in Phase 1:

- `Conversation.set_mode` must refuse any mode above `resolved.ceiling`, using
  the error-event shape it already uses for yolo. Without this a preset's
  `ceiling` is decoration.
- `set_model` must re-render the system prompt from the **frozen**
  `section_bodies`, not from a fresh `prompt_overrides(cwd)` read.
- Permission *rules* stay live (`Rules.persist_allow` keeps appending). Rules
  decide *this call*; the ceiling decides *what is ever possible*. Only the
  second is composition and only the second is frozen.

### 1.6 Where authored files live

AUTHORING wants `.quickcode/plugins/*.md`, flat, kind in the frontmatter. UX
§9.1 tabulates `.quickcode/agents/<name>.md`, `.quickcode/tools/<name>.json`,
`.quickcode/prompt/<id>.md`.

**Decision — AUTHORING wins entirely.** One flat directory,
`.quickcode/plugins/*.md`, markdown with `---` frontmatter, for every
authorable kind. UX's own text defers ("Exact paths and formats are the
AUTHORING sibling's call"). Per-kind directories duplicate the `kind:` key and
create a second truth that can disagree with the first. JSON tool files are
refused for AUTHORING's stated reason: a tool's description is its primary
content and JSON renders a paragraph as `\n` escapes, after which nobody edits
the file. `.quickcode/agents/*.md` keeps loading unchanged as `kind: agent`.

### 1.7 Command-tool template syntax

UX §9.3's form mock shows `uv run pytest -q ${path} ${mark:+-k "$mark"}` — a
shell string with shell quoting. AUTHORING §1.1 mandates argv-first: a JSON
array, one element per token, `create_subprocess_exec`, no shell.

**Decision — AUTHORING wins.** UX's mock predates the argv decision and would
reintroduce exactly the injection surface AUTHORING closed. The threat model
is not the user; it is the model, which fills the parameters and is the one
component here nobody can audit. The New-tool form edits an argv token list
(a repeatable row of tokens). The dry-run panel stays and gets *better*: it
renders the resolved argv array, which shows the exact tokens rather than a
string a reader has to parse in their head.

Shell mode is deferred entirely — see §6.

### 1.8 Machine room membership

UX §3 puts four "entirely locked" plugins in a Machine room:
`runtime.tool_protocol`, `runtime.session_log`, `hook.plan_mode`, and "the
sanitizer half of `runtime.subagents`". But `runtime.subagents` also owns
`max_depth` and `max_agents`, which UX §8.2 renders on a Parts page. A plugin
cannot live in two rooms and keep a single URL.

**Decision — Machine room is a filter, not a location.** It is a view over
plugins whose `tier()` is `locked`, plus an index of locked *settings* that
live on plugins elsewhere. `runtime.subagents` has exactly one canonical page,
under Parts ▸ Policies & limits; its locked `sanitize_reports` renders in the
Fixed-by-design block on that page and is *linked* from Machine room. One
canonical page per plugin, always. UX's intent — locked things get a room that
reads as documentation rather than as breakage — survives.

### 1.9 Negative tool patterns

UX §12 requires `select()` to grow `!pattern`, "two lines", to let the picker
offer "exclude this one from `mcp__*`" without freezing the glob.

**Decision — deferred.** It exists only to serve a picker affordance that only
matters once a 24-tool MCP server is attached, and no MCP server ships by
default. In the first pass the picker offers "expand this glob into names"
with an explicit warning that expansion freezes the set. `select()` is 12
lines; the exclusion is cheap to add when the need is real.

### 1.10 `affects` vs `layer` — two vocabularies, do not confuse

UX introduces `Effect = Literal["prompt","tool_list","loop","storage","ui",
"permissions","models"]` for `PluginSpec.affects`. BINDING introduces a
`layer` vocabulary for provenance. They are orthogonal and both survive:
`affects` says *what surface a plugin touches*; `layer` says *which
configuration source set a value*. Nothing merges them.

### 1.11 Tier of authored things

AUTHORING §4 says a duplicated copy is born `source="authored"`,
`required=False`, `tier="free"` on every setting — including settings that are
locked on the original.

**Decision — keep it, and state it as a rule:** *tier is a property of a
plugin's source, not of its content.* Internal plugins carry their declared
tier; authored plugins are always `free`. "The tier system protects
QuickCode's internals from you; it does not protect your own files from you."

### 1.12 Two problem surfaces vs one

AUTHORING §5.3 puts problems in `GET /api/kernel` plus a dedicated
`GET /api/kernel/problems` for polling, a Problems card, a tab badge, a toast
and a `quickcode doctor` section. BINDING puts conflicts inside
`Resolved.conflicts`.

**Decision — one array, two views.** `Problem` records are produced by both
the authoring validator and the resolver and land in one registry-level list.
`GET /api/kernel` carries `problems` (all of them); `/resolved` carries the
subset whose `subject` is that agent. `GET /api/kernel/problems` is cut — a
second endpoint to poll a field the first endpoint already returns is not
worth a route. `doctor` and the toast stay.

### 1.13 The depth-0 pool exception, and what the landed fix does not yet cover

BINDING §7 contains a carve-out that is easy to implement backwards. Children
are intersected against the parent's **pool**, not its **grant**, at depth 0
only; below depth 0 a child's pool *is* its grant, so narrowing compounds.
Rationale: restricting the orchestrator's tools states what the orchestrator
does with its own hands, not the session's capability envelope. Without the
carve-out, "delegate everything" is indistinguishable from "this session
cannot edit files".

**This matters for the already-landed fix.** `spawn_subagent` passes
`tool_pool=list(registry.tools.values())` — the spawner's *grant* — as the
child's pool. Correct at depth ≥ 1, and it is what fixed the grandchild
escalation. At depth 0 it is wrong: the delegator preset's `implement` agent,
asking for `write`/`edit`/`bash` under an orchestrator holding none of them,
would resolve to nothing.

**Decision — Phase 1 adds `SubagentDeps.pool` (the session pool, set once at
open) alongside the parent's `Resolved`.** The resolver intersects against
`deps.pool` at `depth == 0` and `parent.tools` otherwise. The landed fix is
not reverted; it becomes the `depth >= 1` branch.

The UI must state the consequence: **restricting the orchestrator's tools does
not restrict the session.** To restrict the session, disable the plugin
(§1.3).

### 1.14 The subagent prompt is a format string

BINDING §10.6 wants `render_subagent_prompt` reimplemented over
`sections.compose()`. UX §11 wants the UI to tell a user that `prompt.tone` is
"not in `explore`'s prompt" because subagent prompts come from a different
template. These are the before and after of one change.

**Decision — do the rewrite in Phase 1.** It is what makes
`applies_to: [subagents]` expressible at all, and it is what lets the
workbench preview show real bytes for a subagent rather than a
reconstruction. UX's "not in explore's prompt" note stops being a hardcoded
caveat and becomes a computed fact.

Byte-stability guard: capture today's composed subagent prompt for `explore`
and `general` as golden files *before* the rewrite. The rewrite must reproduce
them byte-for-byte, or the diff is reviewed once and the golden updated
deliberately. `compose()` itself — the `"\n\n"` join and the empty-section
drop — is not reopened.

### 1.15 Authorable kinds: five, four, or three

AUTHORING lists five (`tool`, `agent`, `prompt`, `mcp`, `preset`). §1.1 cut
`preset`.

**Decision — three for the first pass: `tool`, `agent`, `prompt`.** `kind:
mcp` is cut because `mcpServers` in `settings.json` already works and pasting
a Claude config is the reason that shape exists; the file form buys a title, a
description and a secret-in-a-committed-file warning, which is polish rather
than capability. The Adopt action that rewrites a blob into a file goes with
it.

Three kinds is exactly what the first pass's goal needs: understand an agent,
see what it gets, change it, create a command tool and a custom agent, attach
them.

---

## 2. Final object model

Every type, with the fields it ships with. Python dataclass signatures.

### 2.1 New: `kernel/problems.py`

```python
Layer = Literal["default", "user", "project", "preset", "agent",
                "session", "call", "parent", "runtime"]
Severity = Literal["error", "warning", "info"]

@dataclass(frozen=True)
class Provenance:
    layer: Layer
    source: str = ""        # "manifest.py" | "preset:delegator" | "builtin:explore"
                            # | ".quickcode/plugins/reviewer.md"
    path: str = ""          # real path, optionally "<file>#/json/pointer"
    rule: str = ""          # the pattern or key that matched: "mcp__docs__*", "body"
    via_bundle: str = ""    # set when a bundle produced the rule (unused until bundles land)
    note: str = ""          # the one free-text slot: "capped by cap_mode(auto-edit, ask)"

@dataclass(frozen=True)
class Problem:
    code: str                               # stable, machine-readable
    severity: Severity                      # error = skipped/refused; warning = loaded but wrong
    message: str                            # what is wrong, in the user's vocabulary
    fix: str = ""                           # the next action, imperative
    subject: str = ""                       # plugin id or agent id this is about
    field: str = ""                         # "argv", "tools", "model"
    provenance: Provenance | None = None
    line: int = 0                           # best-effort; authored files only
```

Error vocabulary (`code`), authoring half: `missing_key`, `bad_kind`,
`bad_slug`, `id_reserved`, `id_duplicate`, `missing_block`, `bad_json`,
`unknown_param_type`, `unknown_placeholder`, `param_substitution_in_shell`,
`list_placeholder_not_alone`, `bad_enum_choice`, `timeout_out_of_range`,
`path_escapes_project`, `unknown_agent_ref`, `order_conflict`.
Resolution half: `model_not_selectable`, `model_outside_set`,
`tool_withheld_by_parent`, `spawn_withheld_by_parent`, `unknown_agent`,
`pattern_matched_nothing`, `tool_not_installed`, `unknown_section`,
`ceiling_capped`.

### 2.2 New: `kernel/composition.py`

```python
@dataclass(frozen=True)
class Composition:
    """What is attached to one agent. Authored; never the resolved answer."""
    tools: tuple[str, ...] | None = None          # patterns; None = inherit
    spawns: tuple[str, ...] | None = None         # agent ids; None = inherit
    sections: tuple[str, ...] | None = None       # prompt section ids; None = default set
    section_bodies: dict[str, str] = field(default_factory=dict)
    models: tuple[str, ...] = ()                  # allowed set; empty = any
    model: str = ""                               # default pick within that set
    model_selectable: bool = True
    ceiling: Mode = Mode.ask
    max_turns: int = 30                           # role-conditional: no-op on @orchestrator
    color: str = "cyan"
    skip_project_instructions: bool = False
    settings: dict[str, dict[str, Any]] = field(default_factory=dict)
    base: str = ""                                # another composition to start from

Selector = Literal["@orchestrator", "@subagents", "@all"] | str  # or "agent:<id>"
Effect_ = Literal["grant", "revoke", "set"]        # "narrow" folded into grant + intersection

@dataclass(frozen=True)
class Binding:
    """One authored statement contributing to one or more compositions."""
    plugin: str                  # "tool.bash", "prompt.verification", "agent.explore"
    to: str                      # a selector
    effect: Effect_ = "grant"
    value: Any = None            # effect-dependent: a section body, a settings dict

@dataclass(frozen=True)
class Resolved:
    """What an agent actually has. Derived; stored only as a session snapshot."""
    id: str
    role: Literal["orchestrator", "subagent"]
    tools: tuple[str, ...]                          # concrete tool names, not patterns
    denied_tools: tuple[str, ...]                   # in the pool, not granted — listed, never omitted
    spawns: tuple[str, ...]
    sections: tuple[str, ...]
    section_bodies: dict[str, str]
    models: tuple[str, ...]
    model: str
    model_selectable: bool
    ceiling: Mode
    max_turns: int
    settings: dict[str, dict[str, Any]]
    chain: dict[str, tuple[Provenance, ...]]        # "tools.write", "model", "ceiling" -> layers, last wins
    problems: tuple[Problem, ...]

    def to_json(self) -> dict[str, Any]: ...
    def digest(self) -> str: ...                    # sha256 over the canonical JSON
```

`Resolved.chain` keys are dotted paths (`"tools.write"`, `"model"`,
`"ceiling"`, `"settings.runtime.agent_loop.max_rounds"`); UX's five-slot strip
renders one entry per slot. `Effect="narrow"` from BINDING is dropped —
capability fields are intersected unconditionally (§6), so "narrow" is what
`grant` already does.

### 2.3 New: `kernel/resolve.py`

```python
def resolve_composition(
    agent_id: str,
    *,
    pool: list[Tool],                  # the session pool, post-`enabled` filter
    preset: Preset,
    defs: dict[str, AgentDef],         # frozen snapshot, not a live load_defs()
    cwd: Path | None,
    parent: Resolved | None = None,    # None at depth 0
    depth: int = 0,
    overrides: dict[str, Any] | None = None,   # per-call, e.g. the agent tool's model=
) -> Resolved: ...
```

Total: never raises. Layer order, capability fields intersected, value fields
last-writer-wins:

| # | Layer name | Source | Capability | Value |
|---|---|---|---|---|
| 0 | `default` | dataclass defaults, `manifest.py`, `enabled` filter | seed | seed |
| 1 | `user` | `~/.quickcode/settings.json` | ∩ | overwrite |
| 2 | `project` | `<cwd>/.quickcode/settings.json` | ∩ | overwrite |
|   |  | *(`settings.local.json` is not a plugin/preset layer — §0 defect 4)* | | |
| 3 | `preset` | `presets.<id>` (+ its `bindings`) | ∩ | overwrite |
| 4 | `agent` | `AgentDef.composition` (builtin or authored file) | ∩ | overwrite |
| 5 | `session` | recorded in session meta at open | ∩ | overwrite |
| 6 | `call` | the `agent` tool's `model=` argument | may not widen | overwrite, re-checked |
| — | `parent` | `parent.tools` (depth ≥ 1) or `pool` (depth 0) | ∩, always last | — |
| — | `runtime` | `MAX_DEPTH`, delegation-pair-by-depth | ∩ | — |

Capability fields: `tools`, `spawns`, `models`, `ceiling`. Value fields:
`model`, `max_turns`, `section_bodies`, `settings`, `model_selectable`,
`color`, `skip_project_instructions`. `model` is the hybrid: a value field
that must be a member of the intersected `models` set, else `model_outside_set`
(severity `error`).

Prompt sections are **not** intersected. They are not capabilities; a child
legitimately needs sections its parent lacks.

### 2.4 Changed: `subagents/definitions.py`

```python
@dataclass
class AgentDef:
    name: str
    description: str
    role: Literal["orchestrator", "subagent"] = "subagent"
    composition: Composition = field(default_factory=Composition)
    source: Source = "internal"
    path: str = ""

    # Backward compatibility: every existing scalar becomes a read-through
    # property over `composition`, so manifest.agent_specs(), the settings UI
    # and every current caller keep working unchanged.
    @property
    def tools(self) -> list[str] | None: ...
    @property
    def model(self) -> str: ...
    @property
    def models(self) -> list[str]: ...
    @property
    def model_selectable(self) -> bool: ...
    @property
    def mode_cap(self) -> Mode: ...          # -> composition.ceiling
    @property
    def max_turns(self) -> int: ...
    @property
    def color(self) -> str: ...
    @property
    def skip_project_instructions(self) -> bool: ...
    prompt_body: str = ""                    # stays a real field; it is the markdown body
```

`load_defs` reserves the id `@orchestrator` and refuses `role: orchestrator`
on any other id. The leading `@` is why no ordinary agent name can collide.
New optional frontmatter keys: `role`, `base`, `spawns`, `sections`. A file
with none of them parses exactly as it does today.

Role gates four things: `plan` eligibility (orchestrator only —
`build_registry` already strips it for children); the permission callback
(WebSocket round-trip vs `_deny_cb`); `ceiling` semantics (a live-adjustable
starting mode vs a hard cap applied by `cap_mode`); and `max_turns`
applicability (no-op on the orchestrator, and greyed out in the UI rather than
pretended).

### 2.5 Changed: `kernel/preset.py`

```python
@dataclass(frozen=True)
class Preset:
    id: str
    title: str
    description: str = ""
    builtin: bool = False
    base: str = ""
    orchestrator: Composition = field(default_factory=Composition)
    agents: dict[str, Composition] = field(default_factory=dict)
    bindings: tuple[Binding, ...] = ()
    settings: dict[str, dict[str, Any]] = field(default_factory=dict)

    # Legacy read-only properties so select_tools() and the current UI keep
    # working through the transition. Deleted once nothing calls them.
    @property
    def tools(self) -> tuple[str, ...]: return self.orchestrator.tools or ("*",)
    @property
    def prompt_overrides(self) -> dict[str, str]: return self.orchestrator.section_bodies
    @property
    def default_mode(self) -> str: ...
```

Note `Preset.agents` changes meaning from `tuple[str,...]` to
`dict[str, Composition]`. The spawn list moves to `orchestrator.spawns`. The
legacy `agents` list on disk lifts there; `from_dict` discriminates by type.

### 2.6 Changed: `kernel/spec.py` — the explanation fields

```python
Effect = Literal["prompt", "tool_list", "loop", "storage",
                 "ui", "permissions", "models"]
Audience = Literal["orchestrator", "named_agents", "all_agents", "install"]

@dataclass(frozen=True)
class Recourse:
    action: Literal["duplicate", "author", "settings", "docs", "none"]
    label: str
    target: str = ""

@dataclass
class PluginSpec:
    # …every existing field unchanged…
    summary: str = ""                        # <= 90 chars, one sentence, no ids
    affects: tuple[Effect, ...] = ()
    audience: Audience = "install"
    consequence: str = ""                    # what becomes different; neutral at every tier
    locked_because: str = ""                 # required when tier() == "locked"
    recourse: Recourse | None = None         # required whenever locked_because is set
    docs_anchor: str = ""                    # "docs/PERMISSIONS.md#modes"
    path: str = ""                           # on-disk source, authored plugins only

@dataclass(frozen=True)
class SettingSpec:
    # …every existing field unchanged, including `risk`…
    affects: tuple[Effect, ...] = ()
    effect_detail: str = ""                  # the mechanical sentence
    locked_because: str = ""                 # falls back to the plugin's
    recourse: Recourse | None = None         # falls back to the plugin's
    example: str = ""                        # a good non-default value, used as placeholder
```

`risk` is neither duplicated nor replaced. Rendering rule: `free` → show
`consequence` in the neutral slot; `confirm` → `consequence` neutral **and**
`risk` amber (they are different sentences and the existing `risk` strings in
`manifest.py` are already written correctly); `locked` → `consequence`
neutral, `locked_because` in the Fixed-by-design block, `recourse` as an
action button.

Also added to `Source`: `"authored"`. `factory: Callable[[], Any] | None` is
**deferred** with entry-point plugins; `path` lands in Phase 5.

**Backward compatibility.** Every new field on `PluginSpec` and `SettingSpec`
has a default, so no existing construction site breaks. `PluginSpec` is a
plain (non-frozen) dataclass and `SettingSpec` is frozen with defaults; adding
keyword fields at the end is safe for both.

### 2.7 Sessions already on disk

Nothing breaks and no recorded session changes behaviour.

- A session with **no `composition` meta record** — every session written
  before this work — falls back to `preset.resolve(cwd, recorded_id)` and
  re-resolves, which is exactly today's behaviour including the fallback to
  `standard` when the preset is gone.
- A session **with** a `composition` meta record resumes from the recorded
  payload and does not re-resolve. This is strictly better than today:
  deleting a preset no longer degrades a resumed conversation to `standard`.
- New sessions write both, keeping the existing `preset=<id>` record for
  display and for "start a new session like this one".

`store.append_meta(**fields)` already accepts arbitrary keys and
`store.meta()` already merges later-wins, so **`session/store.py` needs no
change**.

Settings files written by the new version and read by an older build lose
`orchestrator`/`agents`/`bindings` and fall back to the legacy fields, which
`save_preset` keeps emitting alongside the new shape for one release. The
asymmetry is deliberate: an old reader gets a degraded but valid composition,
never a broken one.

### 2.8 Deferred types, named so they are not invented twice

`Bundle` (`PluginSpec(kind="bundle")`, `@files`/`@search`/`@shell`), the
`factory` field, and the trust record in `~/.quickcode/trust.json`. See §6.

---

## 3. Final API table

Every route the three docs name, deduped, with the collisions against today's
`server/app.py` marked. **Every route below also gets its
`/api/projects/{pid}/…` twin**, matching the pairing app.py already does for
every public route.

### 3.1 Routes that exist today (unchanged unless noted)

`GET /api/health` · `GET /api/bootstrap` · `GET /api/sessions` ·
`POST /api/sessions/delete` · `POST /api/sessions/cleanup` ·
`DELETE /api/sessions/{conv_id}` · `POST /api/sessions/{conv_id}/archive` ·
`POST /api/sessions/{conv_id}/unarchive` · `POST /api/conversations` ·
`GET /api/models` · `GET /api/plugins` · `GET /api/projects` ·
`POST /api/projects/open` · `GET /api/dir` · `PUT /api/config` ·
`POST /api/apikey` · `WS /ws/conversation/{conv_id}` ·
`GET /api/git/status` · `GET /api/git/diff` (the last two registered from
`server/gitinfo.py`, not `app.py` — the precedent §4 Phase 4 follows)

Changed in place:

| Route | Change |
|---|---|
| `GET /api/kernel` | payload grows `problems: Problem[]` and the new `PluginSpec` fields |
| `GET /api/kernel/plugins/{id}` | grows the new fields; grows `used_by` in Phase 7 |
| `PUT /api/kernel/plugins/{id}` | unchanged; 403 on locked, 409 on unconfirmed `confirm` — the existing protocol is correct |
| `GET /api/presets` | returns the new `Preset` shape (orchestrator/agents/bindings) plus legacy keys |
| `PUT /api/presets/active` | unchanged |
| `GET /api/prompt` | accepts optional `?conv=<id>` to render a session's frozen prompt |

### 3.2 New routes

| Method | Path | Request | Response | Errors |
|---|---|---|---|---|
| GET | `/api/kernel/agents` | — | `{agents:[{id,title,role,source,path,tool_count,model,ceiling}]}` incl. `@orchestrator` | — |
| GET | `/api/kernel/agents/{id}/resolved` | query `?preset=`, `?parent=`, `?conv=` | `Resolved.to_json()` + `{frozen,resolved_against,digest,prompt:{text,sections:[{id,title,order,start,end,tier,provenance}]},tool_schemas:[…]}` | 404 unknown agent |
| POST | `/api/kernel/agents/{id}/preview` | `{composition?, prompt_body?}` (an unsaved draft) | same shape as `/resolved`, `frozen:false`, writes nothing | 400 malformed draft |
| GET | `/api/kernel/authored` | — | `{plugins:[{id,kind,scope,path,title,enabled}], problems:[…]}` | — |
| POST | `/api/kernel/authored` | `{kind, name, scope, from_template?}` | the created plugin + `path` | 409 `id_duplicate`, 400 `id_reserved` |
| GET | `/api/kernel/authored/{id}/source` | — | `{path, text, problems:[…]}` | 404 |
| PUT | `/api/kernel/authored/{id}/source` | `{text}` | `{path, problems:[…]}` — **writes regardless**, advisory validation | 404 |
| DELETE | `/api/kernel/authored/{id}` | — | `{trashed_to}` | 404 |
| POST | `/api/kernel/authored/validate` | `{kind, text, scope}` | `{problems:[…]}` — no write | — |
| POST | `/api/kernel/plugins/{id}/duplicate` | `{scope, name?}` | the new authored plugin + `path` | 400 not duplicable (with the reason), 409 name taken |
| POST | `/api/projects/{pid}/mcp/reload` | — | `{servers:[…], problems:[…]}` | — |

### 3.3 Collisions and renames

- **`GET /api/agents` → `GET /api/kernel/agents`.** BINDING and UX both name
  `/api/agents`. There is no HTTP collision today, but `js/panels/agents.js`
  already owns the phrase "agents" for the *runtime roster* of spawned agents
  in a live session (delivered over the WebSocket). Putting configuration
  under `/api/kernel/` matches `/api/kernel/plugins/…`, keeps `/api/agents`
  free for a future runtime-roster route, and removes the ambiguity.
- **`/api/authored/{kind}/{id}` (UX) vs `/api/kernel/authored/{id}`
  (AUTHORING) → the latter.** The kind is already in the id
  (`tool.pytest-failed`); a second path segment for it is the same duplicated
  truth AUTHORING's own §2.2 rejects for directories.
- **`/file` (UX) vs `/source` (AUTHORING) → `/source`,** matching the
  `PluginView` vocabulary already in the codebase.
- **`GET /api/kernel/problems` → cut.** `GET /api/kernel` already carries the
  array; a second route to poll one field is not worth it.
- **`GET /api/bindings`, `GET /api/bundles` → cut.** The first is the
  transpose of `/resolved` and is computable client-side from
  `GET /api/presets`; the second goes with bundles.
- **`GET /api/kernel/plugins/{id}/usage` → cut as a route**, folded into
  `GET /api/kernel/plugins/{id}` as `used_by` (UX's own suggestion: cheaper
  than a separate endpoint and needed on every detail render). Phase 7.
- **`GET|POST /api/projects/{pid}/trust`, `/export`, `/import` → cut**
  with the trust gate and the import/export flow.

---

## 4. Phased implementation plan

Each phase is independently shippable, leaves the app working, and delivers
something a person can see. The order follows the brief's steer; §4.8 records
the one permitted swap.

### Phase 1 — kernel: Composition, resolution, provenance, orchestrator-as-agent

**Goal.** Make "what does this agent actually get" a single computable answer,
with every value carrying where it came from.

**Files created.** `quickcode/kernel/problems.py`,
`quickcode/kernel/composition.py`, `quickcode/kernel/resolve.py`.

**Files changed.** `kernel/preset.py` (new shape + legacy lift + legacy
properties); `subagents/definitions.py` (`role`, `composition`, read-through
properties, `@orchestrator` reserved); `subagents/runner.py` (`SubagentDeps`
gains `parent: Resolved` and `pool`; `spawn_subagent` calls
`resolve_composition`, raises on any `error`-severity problem *before* minting
an agent id, builds the registry from `resolved.tools`; `child()` passes the
child's `Resolved` down; the depth-0 pool carve-out per §1.13);
`prompts/sections.py` (`PromptContext` gains `role` and `agent_id`;
`ordered(extra=None)` merges authored sections by `(order, id)`);
`prompts/subagent.py` (recomposed over `sections.compose()` per §1.14);
`server/manager.py` (resolve `@orchestrator` at open, build the `ToolRegistry`
from `resolved.tools`, pass `resolved.section_bodies` into
`render_system_prompt`, clamp the starting mode to `resolved.ceiling`,
construct `SubagentDeps` with a frozen `defs` snapshot, `append_meta(
composition=…, digest=…)`); `core/agent.py` / `server/manager.py` for
`set_mode` and `set_model` (refuse above the ceiling; re-render from frozen
bodies).

**Three live defects fixed here** (§0), because Phase 1 is already in these
files and leaving them costs more later than fixing them now:

- `registry_factory()` filters the pool by `is_enabled(f"tool.{name}")`, so
  the `enabled` toggle becomes the session-wide revoke §1.3 defines it as.
- `default_mode` resolves once: the `runtime.permissions.default_mode` plugin
  setting wins, falling back to `Config.default_mode`. One value, one place.
- `Composition.max_turns` is enforced per delegation in `_run_and_finish`,
  which is what `AgentDef.max_turns` has always claimed to do.

**Not changed.** `session/store.py` — `append_meta(**fields)` is already
generic (§2.7).

**What the user can newly do.** Write a preset with an `orchestrator` block in
`.quickcode/settings.json` that removes `write`, `edit` and `bash` from the
main agent, and have a spawned `implement` subagent still get them. That is
the delegator pattern, and it is not expressible at all today. Also: the
permission ceiling is enforced on `set_mode` for the first time, so a preset's
`ceiling: auto-edit` stops being decoration.

`server/app.py` is owned by another agent (§5), so Phase 1 ships with
`settings.json` as its only interface. That is honest for a kernel phase.

**Tests worth writing** — one new file, `tests/test_composition.py`, eight
cases:

1. A legacy preset dict with no `orchestrator` key resolves to the same tool
   list and spawn list as `select_tools` produces today.
2. Intersection: a child asking for a superset of its parent gets the parent's
   set.
3. Depth-0 carve-out: an orchestrator granted only `read` still lets
   `implement` receive `write` from the session pool.
4. Depth-1 narrowing: `explore` (read, glob, grep) spawning `general`
   (`tools: None`) yields exactly read/glob/grep — the already-fixed
   escalation, now asserted at the resolver level rather than at the wiring.
5. A literal tool the parent holds-not-but-the-pool-has is a `blocking`
   problem and `spawn_subagent` raises before minting an agent id.
6. `resolve_composition` on a syntactically broken preset returns a `Resolved`
   with problems and does not raise.
7. Every entry in `Resolved.tools` has a non-empty `chain` entry.
8. Golden: `explore`'s composed subagent prompt is byte-identical before and
   after the `prompts/subagent.py` rewrite.

**Risks.** The depth-0 carve-out is easy to implement backwards — test 3 is
the guard. The subagent prompt rewrite can silently change bytes — test 8 is
the guard. `Preset.agents` changes type from tuple to dict; every reader must
be found (`grep -rn "preset.agents"`). `select_tools` must keep working until
`manager.py` stops calling it.

### Phase 2 — the explanation layer, and its content

**Goal.** Every plugin and every setting answers the same six questions, and
the answers are written.

**Files changed.** `kernel/spec.py` (the fields in §2.6, all defaulted);
`kernel/registry.py` (`plugin_json` and `to_json` emit them);
`kernel/manifest.py` (the 8 core specs and their 21 settings get their prose
inline). **File created:** `kernel/explain.py` — per-id prose tables for the
things `manifest.py` produces from *generators* (13 tools, 13 prompt sections,
2 agents, providers, MCP), so the generators stay generators and the prose is
one reviewable table rather than logic interleaved with strings.

**Also:** a five-line change to `frontend/js/settings/plugins.js` rendering
`summary` under the card title and `affects` as chips. That file is not
blocked, and it means the writing is visible in the *current* settings modal
immediately, which both proves the payload and de-risks Phase 3.

**What the user can newly do.** Read a one-line plain-language summary and an
"if changed" consequence for every one of the 37 plugins, instead of
`description` and a `LOCKED_NOTE` constant reused for all 14 locked settings.

**Size — flag this: it is mostly prose.** Roughly 200 authored strings, of
which 30–40 are multi-sentence:

| surface | count | fields each | notes |
|---|---|---|---|
| 8 core plugins | 8 | summary, affects, audience, consequence (+ locked_because, recourse where locked) | hand-written in `manifest.py` |
| their settings | 14 | affects, effect_detail, example (+ locked pair) | hand-written in `manifest.py` |
| the 5 generators' setting templates | 7 | same | written once each, not per instance |
| 13 prompt sections | 13 | summary, audience, consequence | `explain.py` table; `affects` is always `("prompt",)` |
| 13 tools | 13 | summary, audience, consequence | `explain.py` table; `affects` is always `("tool_list",)` |
| 2 agents + provider + mcp | 4 | summary, audience, consequence | `explain.py` table |

Estimate ~700–900 lines added to `explain.py` and ~250 to `manifest.py`. This
is a writing task with an engineering harness, not the reverse: budget it as
one focused pass with a read-through afterwards, and write in the order tools
→ prompt sections → runtime → agents, because tools are what a user meets
first.

**Tests worth writing** — two, and only completeness checks, because that is
the only thing worth asserting about prose. Add to a new
`tests/test_explain.py`:

1. Every `PluginSpec` in a freshly built registry has a non-empty `summary`
   and a non-empty `affects`, and has both `locked_because` and `recourse`
   whenever `tier() == "locked"`.
2. Every `summary` is ≤ 90 characters, and a `prompt_section` spec's `affects`
   includes `"prompt"` (the machine-checkable half of "the prose matches the
   behaviour").

**Risks.** Prose drifting from behaviour as the code changes; test 2's
kind/affects consistency check is the only cheap guard. Writing fatigue
producing 37 restatements of the title — the ≤ 90-char limit and the "no ids,
no 'this plugin'" rule exist to prevent that and should be enforced in review.

### Phase 3 — configuration as a top-level view

**Goal.** Configuration stops being a modal and becomes a linkable view with
a rail, per-kind Parts pages, and a visual language that distinguishes a
prompt section from a tool from a policy.

**Files created.** `frontend/css/config.css` (the `--kind-*` tokens, the
stripe/texture rules, the sigil tile);
`frontend/js/config/{view,rail,kinds,explain,parts,detail,machineroom}.js`.

**Files changed.** `frontend/index.html` (add
`<section id="view-config" class="view">` and the stylesheet link);
`frontend/js/main.js` (`showConfig()`, a third view state, `#/config/…` hash
routing — and **`showConfig()` must not call `disconnect()`**, unlike
`showHome()` at `main.js:27`); `frontend/js/settings/plugins.js` (retired as a
page, its rendering moving into `parts.js`);
`frontend/css/settings.css` (loses `.plug-card`/`.plug-row`; keeps the form,
sheet and raw rules).

**Blocked.** `frontend/js/modals.js` owns `openSettings()`, which should
become a redirect into the view; `frontend/css/app.css` is where the
`--kind-*` tokens naturally sit next to `--chip-*`. Mitigation: put the
`--kind-*` tokens in `config.css` (they are used nowhere else), and ship this
phase with the old settings modal still reachable as a second entry point
until `modals.js` clears. Two entry points for one phase is acceptable; two
entry points forever is not.

**What the user can newly do.** Open `#/config/parts/tools/bash` directly.
See a tool card that does not look like a prompt-section card. Read the Phase-2
explanations laid out as the six questions. Reach a locked plugin's
Fixed-by-design block with a real recourse button instead of a greyed input.

**Tests worth writing.** None. There is no JavaScript test harness in this
repo (all 19 test files are Python), and inventing one for a rendering phase
is exactly the over-testing the project's working agreement rules out. The
check is running the app and clicking through the five rail sections.

**Risks.** `showConfig()` accidentally inheriting `disconnect()` and dropping
the WebSocket every time someone changes a setting — that friction is the
thing this phase exists to remove. Hash routing colliding with existing hash
use. The largest phase by line count; if it slips, Phase 4 is the better next
thing to land (§4.8).

### Phase 4 — the agent workbench and the preview endpoint

**Goal.** One page per agent showing the exact bytes it will receive and the
exact schemas it will be given, with provenance on every value.

**Files created.** `quickcode/server/kernel_api.py` exposing
`register_kernel_routes(app, hub)` — every new route in this plan
(Phases 4, 5 and 6 alike) is declared there, unscoped and project-scoped
twin together. **This follows the pattern `server/gitinfo.py:123
register_git_routes(app, hub)` already established for the git routes**, so
it is not a new idea, and it reduces the diff to the blocked `app.py` to a
single import plus a single call. `frontend/js/config/{agent,preview,
compositions}.js`.

**Files changed.** `server/app.py` (**blocked** — one line);
`frontend/js/settings/presets.js` (folded into `compositions.js`).

**What the user can newly do.** Open `explore` and read its actual composed
system prompt with section boundaries drawn, and its actual tool schemas with
a byte count. See why it does not have `write` — listed as denied with a
reason, not omitted. Edit its instructions and watch the preview change before
saving. Open a running session's frozen view with `?conv=` and see it labelled
as frozen.

**Tests worth writing** — four, in `tests/test_kernel_api.py`:

1. `/resolved` for `@orchestrator` returns a chain entry for every granted
   tool and lists denied tools with a reason.
2. `/resolved?conv=<id>` returns `frozen: true` and a digest equal to the one
   recorded in the session's `composition` meta record.
3. `/preview` with a draft `prompt_body` changes the returned prompt bytes and
   writes nothing to disk.
4. `/preview` and the runner produce identical prompt bytes for the same
   inputs — the guard against the preview becoming a reconstruction.

**Risks.** `app.py` contention (§5). The preview drifting from the runner —
test 4 is the guard, and the structural guard is that `preview` calls
`resolve_composition` and `sections.compose()` rather than any copy of them.

### Phase 5 — the authoring backend

**Goal.** A markdown file in `.quickcode/plugins/` becomes a real plugin,
indistinguishable from an internal one in the registry, the tool list, the
permission gate and the trajectory.

**Files created.** `kernel/authoring/format.py` (`parse_document(text) ->
Document{meta, body, blocks}`, generalising
`subagents/definitions.py:_split_frontmatter`); `authoring/schema.py`
(per-kind key tables, `validate(doc,*,scope,reserved) -> list[Problem]`; pure,
no filesystem — this is where the tests go); `authoring/model.py`
(`AuthoredPlugin` + `to_agent_def()`, `to_prompt_section()`, `to_tool()`,
`to_spec()`); `authoring/discovery.py` (scan the layers, resolve shadowing by
id, return `(plugins, problems)`; never raises);
`authoring/store.py` (slug allocation, create-from-template, save,
delete-to-trash); `authoring/templates/{tool,agent,prompt}.md` (commented
examples, which is what the New flow writes before opening the editor);
`tools/command.py` (`CommandTool(Tool)` — builds a pydantic `Input` with
`pydantic.create_model` so `Tool.schema()` and the strict-schema contract are
untouched, implements the argv substitution rules, execs with
`create_subprocess_exec`, truncates with `tools/base.py:truncate`, and sets
`permission = PermissionSpec(mutates=not read_only, target_field=…,
path_target=…)`).

**Files changed.** `kernel/spec.py` (`"authored"` added to `Source`, `path`
added to `PluginSpec`); `kernel/manifest.py` (`authored_specs(plugins)`, one
branch per kind, every setting `free`); `kernel/registry.py` (carry
`problems`; `register` records an `id_duplicate` problem instead of only
logging); `kernel/bootstrap.py` (run discovery inside `_safe`, register
authored specs *after* the internal ones so a reserved-id collision loses);
`subagents/definitions.py` (`_split_frontmatter` delegates to
`format.parse_document` so the two loaders cannot drift);
`server/projects.py` (build command tools into `extra` alongside
`plugin_tools` and `mcp_tools`); `server/app.py` (**blocked** — the authored
routes, again registered from `kernel_api.py`).

**What the user can newly do.** Write
`.quickcode/plugins/pytest-failed.md`, start a session, and have the model
call it with typed parameters and an approval modal that shows the exact argv.
Write `.quickcode/plugins/house-style.md` with `after: prompt.conventions` and
`applies_to: [main, subagents]` and have it land in both prompts. Make a typo
and get a red Problems card naming the file, the line, what is wrong and what
to do about it.

**Tests worth writing** — eight, in `tests/test_authoring.py`, all against
`schema.validate` and the substitution rules, which are pure:

1. `{param}` inside an element does not re-split on whitespace: a value
   containing spaces produces one argv element.
2. An element that is exactly `{param}` with a `list` parameter expands to one
   element per item.
3. An element that is exactly `{param}` with an empty value is dropped.
4. An unknown `{name}` is an `unknown_placeholder` error, not a silent empty.
5. A `path` parameter resolving outside the project root is refused by the
   tool, not the gate.
6. A file with an error-severity problem produces zero specs and
   `build_registry` does not raise.
7. `tool.bash` as an authored id is refused with `id_reserved`.
8. `.trash/` contents are not discovered.

**Risks.** `permissions.registry_specs()` is a process-level cache built from
`default_registry()` and will not contain authored tools; the session path
must be verified to pass `ToolRegistry.permission_specs()` explicitly, or an
authored `read_only: true` tool falls back to `DEFAULT_SPEC` and keeps
prompting — annoying rather than dangerous, but it will be read as a bug.
`glob("*.md")` on the plugins directory must never become recursive or it
walks into `.trash/`. Renaming an authored tool mid-session produces a wire
name the running conversation has never heard of — the rename path must say so
and offer a new session.

### Phase 6 — creation flows and duplicate-to-customise

**Goal.** The flows that turn "I could edit a file" into "I pressed a button
and got a file I own".

**Files created.**
`frontend/js/config/create/{scaffold,agent,tool,prompt}.js`,
`frontend/js/config/empty.js` (the empty states, each naming one real existing
thing and offering to duplicate it).

**Files changed.** `kernel/authoring/store.py` (the duplicate table:
internal `agent` yes; built-in `preset` yes; authored anything yes; `prompt`
at `locked` tier yes but as a *sibling* with `after: <original>`; internal
`tool` **no**, with the reason shown — "a Python tool's behaviour is not
expressible as an argv template" — and a "New command tool" button in its
place); `server/app.py` (**blocked** — duplicate route);
`frontend/js/config/{agent,parts,detail}.js` (wire the buttons).

**What the user can newly do.** Press Duplicate on `agent.explore` — a locked,
required, built-in plugin — and get an editable markdown file with
`derived_from: agent.explore` in which every previously-locked line is plain
text. Press New command tool, fill a form with a live schema preview and a dry
run showing the resolved argv, and get a tool the model can call. Attach it to
an agent from that agent's Tools section.

This phase is the point of the whole pass.

**Tests worth writing** — three, added to `tests/test_authoring.py`:

1. Duplicating a locked internal agent yields an authored spec with
   `source="authored"`, `required=False`, every setting `free`, and
   `derived_from` set — and the original is untouched and still enabled.
2. Name allocation goes `-copy`, `-copy-2`, `-copy-3`.
3. Duplicating an internal `tool` is refused, and the refusal carries the
   sentence explaining why plus the `New command tool` recourse.

**Risks.** The form becoming the only path — "Edit as file" must be reversible
while the content still parses, and the raw editor is the primary surface, not
the escape hatch, because the body is the interesting part of every kind.
"Run it once to check" must go through the normal permission path, not around
it.

### Phase 7 — bundles, traceability, install

**Goal.** The remainder, mostly deferred (§7). In scope if reached: `used_by`
on `GET /api/kernel/plugins/{id}` and the USED BY block on every detail page;
cross-links from the trajectory and chat into `#/config/…`; the global search
box. Bundles, entry-point plugins and the trust gate stay out.

**Files.** `kernel/registry.py`, `frontend/js/config/{detail,search}.js`,
`frontend/js/trajectory.js`.

**Tests.** One: `used_by` for `tool.bash` names every preset whose
orchestrator grants it and every agent definition listing it.

**Risks.** `used_by` needs a resolve per agent per plugin render — cache it
per registry build or the Parts list goes quadratic (note that `_registry_for`
already rebuilds the registry on every request, deliberately, so the cache
must be per-request, not process-level).

### 4.8 Order justification, and the one permitted swap

The steer's order holds. Phase 2 must precede Phase 3 because a new view
rendering `description` and a single `LOCKED_NOTE` constant is not better than
the old one — the content is what makes the view worth building. Phase 5 must
precede Phase 6 because the creation UI has nothing to create against
otherwise.

**Permitted swap: Phase 3 ↔ Phase 4.** If `app.py` frees up before the
frontend work starts, land Phase 4 first. `/resolved` is the highest-value
single artifact in this plan — it is what makes an agent legible — and it can
be rendered by a plain page inside the existing settings modal. Phase 3 is the
largest phase by line count and the least reversible.

---

## 5. File ownership map

A concurrent agent owns six files plus `tests/`. **Blocked until clear:**

```
quickcode/server/app.py
quickcode/session/store.py
quickcode/frontend/js/home.js
quickcode/frontend/js/modals.js
quickcode/frontend/css/home.css
quickcode/frontend/css/app.css
tests/
```

| Phase | Files it owns | Blocked-file dependency |
|---|---|---|
| 1 | `kernel/{problems,composition,resolve}.py`, `kernel/preset.py`, `subagents/{definitions,runner}.py`, `prompts/{sections,subagent}.py`, `server/manager.py`, `core/agent.py` | **none** — `store.py` needs no change (§2.7) |
| 2 | `kernel/{spec,manifest,explain,registry}.py`, `frontend/js/settings/plugins.js` | none |
| 3 | `frontend/index.html`, `frontend/js/main.js`, `frontend/css/config.css`, `frontend/js/config/*.js`, `frontend/css/settings.css` | `modals.js` (the `openSettings()` redirect), `app.css` (the `--kind-*` tokens) — both worked around, see Phase 3 |
| 4 | `server/kernel_api.py`, `frontend/js/config/{agent,preview,compositions}.js`, `frontend/js/settings/presets.js` | **`app.py`** — one import + one `register_kernel_routes(app, hub)` call, shared with Phases 5 and 6 |
| 5 | `kernel/authoring/*`, `tools/command.py`, `kernel/{spec,manifest,registry,bootstrap}.py`, `subagents/definitions.py`, `server/projects.py`, `server/kernel_api.py` | none beyond Phase 4's one line |
| 6 | `kernel/authoring/store.py`, `frontend/js/config/create/*`, `frontend/js/config/empty.js`, `server/kernel_api.py` | none beyond Phase 4's one line |
| 7 | `kernel/registry.py`, `frontend/js/config/{detail,search}.js`, `frontend/js/trajectory.js` | none |

**Phases 1, 2, 3 and 7 can start immediately and in parallel** — their file
sets are disjoint apart from `kernel/spec.py` and `kernel/registry.py`, which
Phases 2 and 5 both touch and must therefore be sequenced (2 before 5, which
the order already does).

**Phases 4, 5 and 6 are all gated on `app.py`, but only barely.** Every new
handler lives in `server/kernel_api.py` behind
`register_kernel_routes(app, hub)`, following the existing
`register_git_routes` precedent, so `app.py`'s total diff across all three
phases is **one import and one call**. The three phases can be developed and
tested in full against a locally constructed app and merged the moment
`app.py` clears.

`tests/` is owned by the concurrent agent, so every phase's tests are blocked.
Mitigation: all tests proposed above go in **new** files
(`test_composition.py`, `test_explain.py`, `test_kernel_api.py`,
`test_authoring.py`), which do not collide with edits to existing test files.
Coordinate before adding them; do not touch the 19 existing test files.

---

## 6. Invariants to not break

Each with the one-line test that would catch a violation.

1. **The system prompt is byte-stable within a session.** The cache breakpoint
   sits on the system message (`core/history.py`). *Test:* compose the prompt
   twice from one frozen `PromptContext` and assert byte equality; and assert
   that a session's second turn sends the same system bytes as its first.
2. **The composition is frozen at session open.** Tools, section bodies,
   ceiling, spawnable agents and subagent definitions come from the session's
   `composition` meta record, never from a live re-read. *Test:* open a
   session, mutate the preset on disk, take a turn, assert the tool list is
   unchanged.
3. **The session log is append-only with a monotonic `seq`.** *Test:* append
   20 events across two `SessionStore` instances over one file and assert
   `[e["seq"] for e in events()] == list(range(...))` and that no existing
   line was rewritten.
4. **Locked means uneditable, never hidden.** *Test:* every plugin with
   `tier() == "locked"` still returns a non-`None` `view()` and a
   `plugin_json` containing the full current value of every locked setting.
5. **Subagents never prompt for permission.** A headless child has no
   interactive reviewer; it gets `_deny_cb`, and it is built with an empty
   `Rules()` so it inherits none of the project's persisted "always allow"
   rules either. *Test:* spawn a subagent whose composition would require an
   `ask` decision and assert the tool result is a denial, not a pending
   permission request; and assert a persisted `allow` rule in
   `settings.local.json` does not reach the child.
6. **Narrowing only: no layer, definition or argument can widen a child.**
   `child.f = ask(f) ∩ parent.f` for `tools`, `spawns`, `models`, `ceiling`.
   *Test:* the depth-1 case (an `explore` parent spawning a `tools: None`
   child yields exactly read/glob/grep) plus the depth-0 carve-out (an
   orchestrator granted only `read` still admits a child holding `write` from
   the pool).
7. **A broken authored plugin never breaks startup.** *Test:* write a
   `.quickcode/plugins/x.md` containing `kind: nonsense` and garbage, then
   assert `bootstrap.build_registry()` returns a registry containing all 37
   internal plugins plus one `Problem`, and does not raise.
8. **The plugin list the UI shows is the list the runtime uses.** *Test:* for
   a built registry, the set of `kind == "tool"` spec ids equals
   `{f"tool.{t.name}" for t in session_registry.tools.values()}`.
9. **`resolve_composition` never raises; `spawn_subagent` refuses loudly.**
   A session must always open; a spawn must fail before minting an agent id.
   *Test:* resolve against a deliberately corrupt preset and assert a
   `Resolved` with problems; then assert `spawn_subagent` raises `ValueError`
   on an `error`-severity problem and that no agent id was allocated.
10. **The delegation pair is granted by depth, never by allowlist.**
    `build_registry` strips `agent`/`send_message` and re-adds them by depth
    (`tools/registry.py:117`). *Test:* a composition explicitly listing
    `agent` at the depth floor still resolves without it.

---

## 7. Cut line

The three documents together describe considerably more than is worth
building in one pass. The target for the first complete pass, restated:

> A human can understand what an agent is, see exactly what it will get,
> change it, create a command tool and a custom agent, and attach them.

Everything below is judged against that sentence.

### In

- `Composition`, `Binding`, `Resolved`, the seven-layer resolver, intersection
  for capability fields, provenance chains on every value, the orchestrator as
  an `AgentDef` under `@orchestrator`, the depth-0 pool carve-out.
- One `Problem` type covering validation and resolution; one `Provenance`
  type.
- The explanation fields on `PluginSpec`/`SettingSpec` **and the written
  content for all 37 internal plugins**. The fields without the content are
  worthless; this is why Phase 2 is not deferrable.
- Configuration as a linkable third view, per-kind Parts pages, the kind-hue /
  tier-texture visual language, the Machine room as a filter.
- The agent workbench with a live preview backed by `/resolved` and
  `/preview`.
- Markdown authoring for **three** kinds — `tool`, `agent`, `prompt` — from
  `.quickcode/plugins/*.md`, with argv-only command tools, validation, and the
  Problems surface.
- Duplicate-to-customise, New agent, New command tool, and the empty states
  that lead into them.

### Out, with the reason

| Deferred | Reason |
|---|---|
| **Bundles** (`@files`, `@search`, `kind="bundle"`) | A convenience over patterns. `select()` globs already cover `task_*` and `mcp__*`, and there are 13 tools — writing three names is not the friction. Bundles earn their place when a user has 50 tools, and the provenance shape already reserves `via_bundle` so nothing has to be redesigned. |
| **Entry-point third-party plugins** (`quickcode.plugins`, `PluginSpec.factory`) | Nobody is shipping a QuickCode plugin package yet. The `Source` literal already has room, and the loader pattern (`plugins/loader.py`) already exists for tools and providers, so this is additive whenever it is wanted. |
| **The trust gate** (`~/.quickcode/trust.json`, `needs_trust`) | Deferred with a stated security consequence: project-scope authored plugins load without a prompt. This does not open a new class of hole — `.quickcode/settings.json`'s `mcpServers` block already spawns processes from a cloned repository today — but it widens an existing one, and it should be the first thing added after this pass. `discovery.py` ships with the single choke point where the gate goes, and every plugin carries its `scope`. |
| **Shell-mode command tools** (`shell: true`, `$QC_PARAM_*`) | Argv covers the tools people actually write first (`uv run pytest --lf`, `git status`, `npm run build`). Shell mode is the half that most needs the trust gate to be safe, so cutting them together is coherent rather than arbitrary. |
| **`kind: mcp` files and the Adopt action** | `mcpServers` in `settings.json` already works and pasting a Claude config is the reason that shape exists. The file form buys a title, a description and a secret warning: polish. |
| **`kind: preset` files** | §1.1 — a preset has no free-text payload, which was the entire argument for markdown files. |
| **Negative tool patterns** (`!mcp__docs__x`) | §1.9 — serves one picker affordance that only matters with a large MCP server attached. Two lines whenever it is needed. |
| **Per-agent MCP binding** (`{"plugin":"mcp.docs","to":"agent:explore"}`) | Depends on nothing structural — it desugars to a `mcp__docs__*` grant in a composition, which the resolver already supports — but it has no user until MCP servers are in play and the picker is finished. It is expressible by hand in a composition on day one. |
| **`@session` and `agents:<a>,<b>` selectors** | §1.3 — collapsed into `plugins.<id>.enabled` and into two bindings respectively. |
| **`GET /api/bindings`, `GET /api/bundles`, `GET /api/kernel/problems`** | §3.3 — each is a transpose or a subset of a payload another route already returns. |
| **Import / export / import-from-URL** | The file *is* the export; a directory of files is a bundle; people know how to copy files and how to zip a directory. Import-from-URL is refused permanently, not deferred: an authored tool is arbitrary command execution, and pasting content you have read is a different act from fetching content you have not. |
| **Per-authored-plugin drift hashes** | §1.5 — one session `digest` answers the same question for a fraction of the bookkeeping. |
| **`used_by`, cross-links, global search** | Phase 7. Valuable, but they are navigation over facts the earlier phases produce, and none of them is on the path from "I do not understand this agent" to "I changed it". |

The cut removes roughly half the surface the three documents describe and none
of the sentence the pass is judged against.

---

## Addendum: session-scoped switching

Requested after the plan was written: switching composition inside a running
project session, plus other quick session-scoped controls.

### Switching is allowed, at a turn boundary only

The frozen-composition invariant exists for two reasons, and neither one is
"the composition may never change":

1. The model has been told what tools it has. Changing them underneath a
   conversation mid-turn produces calls to tools that no longer exist.
2. The prompt cache breakpoint sits on the system message, which must stay
   byte-stable *within* a cached span.

Both survive a switch taken between turns. So the rule is:

- **Refused while the agent is busy.** Not queued, not applied on the next
  idle — refused, with the reason. A switch that lands invisibly three
  seconds later is worse than one that does not happen.
- **On switch**: re-resolve the composition, re-render the system prompt from
  the new bodies, write a `composition` meta record, and emit a transcript
  marker so the trajectory shows exactly where the agent's capabilities
  changed. Reading a session later without that marker would be misleading —
  the same conversation genuinely had two different agents in it.
- **The cache breakpoint moves once**, and the next turn pays a full
  uncached input. That is the honest cost of the feature; it is paid at a
  moment the user chose, not silently.
- **Resume replays the record**, so reopening a session restores the
  composition it ended with, not the one it started with.

### What is session-scoped and switchable

Three controls belong on the composer, next to the existing mode and model
pills, because they are the three things a user changes mid-conversation:

| Control     | Scope   | Live? | Notes                                   |
|-------------|---------|-------|-----------------------------------------|
| Mode        | session | yes   | already exists; now clamped to ceiling  |
| Model       | session | yes   | already exists; must stay in the set    |
| Composition | session | turn boundary | new                             |

Everything else (authoring a plugin, editing an agent, changing a preset's
definition) stays in the configuration view and takes effect on the next
session. A setting whose blast radius is every future session does not belong
on a pill next to the send button.

### One-click derivation

The switcher's last entry is "Customise this…", which duplicates the active
composition into a project-scoped one and opens it in the workbench. This is
the on-ramp: most people discover they want a custom composition at the
moment an existing one is nearly right.
