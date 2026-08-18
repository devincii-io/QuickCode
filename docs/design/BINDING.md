# Binding: one model for attaching plugins to agents

Status: design. Nothing here is implemented yet. This document owns the
*attachment* problem only — how a plugin becomes part of a particular agent.
Discovery (what exists), mutability tiers, and the settings UI are already
specced in `PLAN-PLUGIN-UI-OVERHAUL.md` and `ARCHITECTURE.md#the-plugin-kernel`
and are assumed here.

## 1. What is broken

A `Preset` attaches things to exactly one agent — the session's main one — and
does it by glob:

```python
registry = ToolRegistry(preset_module.select_tools(preset, pool))      # tools
allowed_agents=list(preset.agents)                                      # spawns
overrides={**preset.prompt_overrides, **prompt_overrides(self.cwd)}     # prompt
```

A subagent's tools come from somewhere else entirely: `AgentDef.tools`, parsed
out of frontmatter in `.quickcode/agents/*.md`, resolved in
`build_registry(defn.tools, pool=deps.tool_pool)`. Its prompt is not composed
from sections at all — `prompts/subagent.py` is a single format string, so no
prompt section can ever apply to a subagent. Its model policy lives in a third
place (`AgentDef.models`, `_resolve_model`), its permission ceiling in a fourth
(`mode_cap`, `cap_mode`).

The consequences are concrete:

- There is no way to say "the `explore` agent also gets the docs MCP server's
  tools" without hand-writing `mcp__docs__*` into that one markdown file, and no
  way to see afterwards that you did.
- There is no way to say "this prompt section applies to subagents but not to
  the orchestrator". `prompt_overrides` reaches the main agent only.
- There is no way to ask "what does agent X actually end up with", because the
  answer is assembled at spawn time from four modules and never written down.
- **There is a live privilege-escalation bug.** `SubagentDeps.child()` copies
  `tool_pool` unchanged, and `build_registry(None, pool=...)` means "inherit the
  whole pool". So an `explore` agent (`tools: [read, glob, grep]`) that spawns a
  `general` agent (`tools: None`) hands the grandchild `write`, `edit` and
  `bash`. The narrowing that `mode_cap` performs for permissions has no
  counterpart for tools.

Every one of these is the same missing concept: there is no first-class
statement "this plugin is attached to that agent", and no resolver that turns
such statements into one answer per agent.

## 2. The unified binding model

### 2.1 Composition

Every agent — orchestrator included — has a **composition**: the complete set of
things attached to it.

```python
@dataclass(frozen=True)
class Composition:
    tools: tuple[str, ...] | None = None        # patterns/bundles; None = inherit
    spawns: tuple[str, ...] | None = None       # agent ids it may spawn
    sections: tuple[str, ...] | None = None     # prompt section ids, None = default set
    section_bodies: dict[str, str] = ...        # section id -> replacement body
    models: tuple[str, ...] = ()                # allowed set; empty = any
    model: str = ""                             # the default pick within that set
    model_selectable: bool = True
    ceiling: Mode = Mode.ask                    # permission ceiling (today: mode_cap)
    settings: dict[str, dict[str, Any]] = ...   # plugin id -> {key: value}
    base: str = ""                              # named base composition to start from
```

A **binding** is one statement that contributes to one or more compositions:

```python
@dataclass(frozen=True)
class Binding:
    plugin: str          # "tool.bash", "@files", "mcp.docs", "prompt.tone", "agent.explore"
    to: str              # a selector, see §3
    effect: str = "grant"  # grant | revoke | set | narrow
    value: Any = None    # effect-dependent (a section body, a mode, a setting dict)
```

Bindings are the wire and settings-file form; compositions are what a resolver
produces. Both exist because they answer different questions. A binding answers
"where did this come from" — it is authored, diffable, and attributable. A
composition answers "what does this agent have" — it is derived, and never
stored except as a session's frozen snapshot.

A **preset** stops being a special shape and becomes exactly two things: the
orchestrator's composition, plus the set of compositions available to spawn.

```json
{
  "presets": {
    "delegator": {
      "title": "Delegator",
      "orchestrator": { "tools": ["@search", "@tasks", "agent", "send_message"], "...": "..." },
      "agents": { "explore": {}, "implement": { "ceiling": "auto-edit" } },
      "bindings": [ { "plugin": "mcp.docs", "to": "@subagents" } ]
    }
  }
}
```

### 2.2 Is the orchestrator literally an `AgentDef`?

**Yes for its composition, no for its runtime.** `AgentDef` gains a `role`
discriminator and a reserved id:

```python
role: Literal["orchestrator", "subagent"] = "subagent"
# id "@orchestrator" is reserved; the loader refuses it for role="subagent".
```

The orchestrator's definition is sourced from a preset's `orchestrator` block,
or from `.quickcode/agents/@orchestrator.md` when the frontmatter declares
`role: orchestrator`. It registers under the reserved id `@orchestrator`. The
leading `@` is the same sigil bundles use (§6) and is why no ordinary agent name
can collide with it.

Collapsing composition into one type is the whole point: one resolver, one
introspection endpoint, one UI screen, one place where "what does this agent
get" is decided. The alternative — a parallel `OrchestratorSpec` — reproduces
the divergence this document exists to remove.

But four things genuinely differ, and `role` gates all four:

| Aspect | `role="orchestrator"` | `role="subagent"` |
|---|---|---|
| `plan` tool | eligible (`PlanModeHook` needs an interactive reviewer) | never — `build_registry` already strips it |
| permission callback | the WebSocket round-trip (`Conversation.permission_cb`) | `_deny_cb`; a headless child cannot prompt |
| `ceiling` semantics | the *starting* mode, live-adjustable up to the ceiling | a hard cap applied by `cap_mode` at spawn |
| `max_turns` | not applicable — a conversation has no turn budget | the child's budget, enforced per delegation |

Consequences worth stating plainly:

- **`max_turns` and `skip_project_instructions` become role-conditional fields.**
  Setting `max_turns` on `@orchestrator` is a no-op, and the UI must grey it out
  rather than pretend it does something.
- **The orchestrator gains an author-able prompt body**, which it does not have
  today. `role`-scoped sections (§3) replace the current split between
  `prompts/sections.py` and `prompts/subagent.py`.
- **Depth 0 becomes a real position in the tree.** `SubagentDeps.depth == 0` is
  the orchestrator, so the narrowing rule in §5 applies uniformly from the root
  instead of starting one level down.

## 3. Binding targets and scope

A selector names *which* compositions a binding contributes to.

| Selector | Means |
|---|---|
| `@orchestrator` | the session's main agent only |
| `agent:<id>` | one named agent definition |
| `agents:<id>,<id>` | a set of named agents |
| `@subagents` | every agent except the orchestrator |
| `@all` | every agent including the orchestrator |
| `@session` | not an agent at all — the session as a whole |

`@session` is deliberately not "all agents". It is the layer where processes,
transports and persistence live: things that exist once per session and are not
owned by any agent. Conflating the two is what makes MCP confusing today.

What each plugin kind means per scope:

| Kind | `@orchestrator` / `agent:` / `@subagents` / `@all` | `@session` |
|---|---|---|
| `tool` | membership in that agent's granted tool list | **pool admission only** — the tool exists and may be granted; grants nothing |
| `bundle` | expands to its pattern list, then as `tool` | pool admission for everything it names |
| `prompt_section` | composed into that agent's system prompt, at the section's `order` | **meaningless — refused.** A prompt belongs to an agent; there is no session-level prompt |
| `mcp_server` | grants `mcp__<name>__*` to that agent | starts/stops the server process. Always implied by any scoped MCP binding |
| `policy` | narrows that agent's `ceiling` | the session's default mode (today `Preset.default_mode`) |
| `agent` | membership in that agent's `spawns` list | **meaningless — refused.** "Which agents exist" is discovery; "who may spawn them" is per-agent |
| `hook` | installed in that agent's loop | **meaningless — refused.** Hooks run in a loop and every loop belongs to an agent. `hook.plan_mode` binds to `@orchestrator` |
| `provider` | **refused in v1** | the session's model transport |
| `panel`, `storage` | **meaningless — refused.** Both are session-wide surfaces | the only valid scope |

Two entries deserve their reasoning.

**A tool binding at `@session` grants nothing.** Pool membership and grant are
different facts, and the current code conflates them: the session's
`ToolRegistry` is both the orchestrator's grant *and* the pool subagents select
from (`tool_pool=list(registry.tools.values())`). That is why an MCP tool can
only reach a subagent by first being given to the orchestrator. Splitting them
means "the docs server is available in this project" and "the explore agent may
call it" are two independent statements, which is the feature that is missing.

**An MCP binding is sugar, plus a lifecycle.** `{"plugin": "mcp.docs", "to":
"agent:explore"}` desugars to a tool binding of `mcp__docs__*` scoped to
`explore`, plus an implicit `@session` binding that keeps the process running.
The lifecycle half can never be scoped: a per-agent process would have to start
mid-session, and §9 explains why that is not on the table.

**`provider` is refused per-agent in v1** and the refusal is loud, not silent. A
second provider means a second client, a second credential, and a second model
catalog. Per-agent *model* choice already exists via `models`/`model` and covers
the actual use case (cheap workers, expensive orchestrator). When a real
multi-provider need appears, it belongs in a design of its own.

## 4. Resolution order and conflicts

There are seven layers. They do not all combine the same way, and pretending
they do is the source of most precedence bugs.

Fields split into two classes:

- **Capability fields** — `tools`, `spawns`, `models`, `ceiling`, MCP grants.
  Combined by **intersection**. Layer order is irrelevant, because intersection
  is commutative and associative. This *is* the narrowing invariant (§5), and it
  deletes a whole category of "which layer wins" questions.
- **Value fields** — `model`, `max_turns`, `section_bodies`, `settings`,
  `model_selectable`, `color`. Combined by **last writer wins** down the ordered
  layer list.

| # | Layer | Source | Capability fields | Value fields |
|---|---|---|---|---|
| 0 | internal defaults | dataclass defaults, `manifest.py` | seed set (everything the install has) | seed |
| 1 | user settings | `~/.quickcode/settings.json` | ∩ | overwrite |
| 2 | project settings | `<cwd>/.quickcode/settings.json` | ∩ | overwrite |
| 3 | preset | `presets.<id>` (either settings file) | ∩ | overwrite |
| 4 | agent definition | builtin `AgentDef` or `.quickcode/agents/*.md` | ∩ | overwrite |
| 5 | per-session override | recorded in session meta at open | ∩ | overwrite |
| 6 | per-call override | `agent` tool's `model=` argument | — (may not widen) | overwrite, then re-checked |
| — | parent composition | the spawning agent's resolved set | ∩ (applied last, always) | — |

The parent intersection is listed outside the numbering because it is not a
configuration layer. It applies after everything else, unconditionally, and no
layer can opt out of it.

`model` is the one hybrid: `models` is a capability (intersected), `model` is a
value (last-wins) that **must be a member of the intersected `models` set**. A
layer-6 override that is not a member is refused, which is exactly what
`_resolve_model` does today; this generalises that behaviour to every layer.

### 4.1 Resolution is total; spawning is fallible

`resolve_composition()` **never raises.** It returns a `Resolved` carrying the
answer plus a `conflicts: list[Conflict]`, each classified `blocking` or
`advisory`. A session must always open — refusing to show a conversation because
a preset went stale is worse than losing the composition, which is the rule
`preset.resolve()` already follows.

Spawning is where refusals bite. `spawn_subagent` raises `ValueError` on any
`blocking` conflict, before minting an agent id — the existing "a refused model
policy should not burn an agent slot" ordering. The `agent` tool turns that into
an error `ToolResult`, so the model is told no and can adapt.

### 4.2 What must be loud

`_resolve_model` already establishes the principle: *the spawning model asked
for something specific and deserves to be told no.* Extend it.

| Conflict | Class | Why |
|---|---|---|
| model override with `model_selectable=false` | blocking | a cheap agent must not be talked onto an expensive model |
| model outside the resolved `models` set | blocking | already enforced; now also across layers |
| literal tool grant the parent does not hold (§5.2) | blocking | this is an escalation attempt, not a wish |
| `agent:X` in `spawns` that the parent may not spawn | blocking | depth laundering: A spawns B to spawn C that A was denied |
| binding to an unknown agent id | advisory | a preset shared between machines must still run |
| glob or bundle matching nothing in the pool | advisory | existing `select()` semantics; a smaller agent, not a crash |
| tool named that this install does not have at all | advisory | an install fact, not a policy decision (§5.2) |
| prompt section id that does not exist | advisory | dropped; the prompt is still coherent |
| `ceiling` above the parent's effective mode | advisory | capped by `cap_mode`; the definition asked, policy answered |
| bundle cycle (`@a` → `@b` → `@a`) | advisory | resolves empty, and the settings editor shows it before you save |
| `@session` scope on a per-agent-only kind | blocking **at save time** | the binding is nonsense and must not be written |

Advisory conflicts still surface: they ride in `Resolved.conflicts`, are rendered
in the introspection view (§8), and — for the ones a child can act on — are
injected into that child's first turn as a `<system-reminder>` listing what it
asked for and did not get. An agent whose prompt says "you have read, glob and
grep" must not silently run with two of them.

## 5. The narrowing-only invariant

> A child's composition is the intersection of what it asks for and what its
> parent holds. No layer, no definition and no argument can widen it.

Formally, for capability field *f*: `child.f = ask(f) ∩ parent.f`, where
`ask(f)` is the result of layers 0–6.

Applied per field:

- **Tools — yes.** This is the missing half of `mode_cap` and the fix for the
  grandchild bug in §1. `SubagentDeps` stops carrying `tool_pool` and carries the
  parent's `Resolved` instead; `deps.child()` passes the *child's* `Resolved`
  down, so narrowing compounds with depth instead of resetting.
- **Models — yes, as a set.** `child.models = ask(models) ∩ parent.models`, with
  the empty parent set meaning "any" and therefore intersecting to `ask`. A child
  may not run on a model its parent was forbidden. The *default pick* is a value
  field and may differ freely inside the narrowed set — that is the whole point
  of cheap workers under an expensive orchestrator.
- **Spawnable agents — yes, and this is also the depth rule.** `child.spawns =
  ask(spawns) ∩ parent.spawns`, on top of the existing `MAX_DEPTH` floor. Without
  the intersection, depth becomes launderable: an agent denied `implement` spawns
  a `general` that spawns `implement`.
- **MCP servers — yes, via the tool intersection.** Because an MCP binding
  desugars to `mcp__<name>__*`, it narrows for free with tools. The *process*
  half is `@session` and is not a capability, so it is not intersected — a child
  cannot start a server, only call one that is already running.
- **Permission ceiling — already yes.** `cap_mode(parent, cap)` is exactly this
  rule for one field. It stays; the general rule is stated to include it rather
  than to replace it.
- **Prompt sections — no, and deliberately.** Sections are not a capability;
  adding one cannot grant anything, and children legitimately need sections
  their parent lacks (a subagent's report-format instructions are meaningless to
  the orchestrator). Section bodies are value fields all the way down.

### 5.2 An agent asks for a tool its parent does not have

Distinguish **patterns** from **literals**, and then distinguish *why* the tool
is missing.

| Case | Answer | Why |
|---|---|---|
| pattern (`mcp__*`, `@files`) matches nothing granted | spawn without it, advisory | a pattern is a wish; the current `select()` semantics are right |
| literal tool exists in the pool, parent was not granted it | **refuse the spawn**, blocking | the operator deliberately withheld it from this branch; granting it here defeats the composition |
| literal tool does not exist in this install at all | spawn without it, advisory + child reminder | absence is an install fact, not a policy decision |

The rule in one line: **refuse when the answer is "no"; proceed when the answer
is "not here".** A preset must keep working on a machine where an MCP server
failed to start, and must not keep working when it is being used to route around
a restriction.

How the user is told, in all three cases:

1. A `binding_refused` / `binding_narrowed` event on the agent's bus, carrying
   `{agent, plugin, requested, effective, reason, layer}`. It is loggable, so it
   survives replay and appears in the trajectory as its own row.
2. The `agent` tool returns the refusal text as an error `ToolResult`, so the
   spawning model reads it and can retry with a different agent type.
3. `GET /api/agents/{id}/resolved` lists it in `conflicts`, so the settings UI
   shows it before anyone ever runs the thing.

## 6. Inheritance, bases and capability bundles

Nobody will write a full tool list per agent. Three mechanisms, in the order the
resolver applies them.

**`tools: null` means inherit the parent's granted list** — not the pool. This is
a one-word change to today's meaning (`build_registry(None, pool=...)` inherits
the pool) and it is the change that fixes the grandchild bug. For the
orchestrator, whose parent is the session, "inherit" means the session pool, so
existing behaviour is preserved exactly where it was correct.

**`base: "<id>"` names another composition to start from.** `Preset.from_dict`
already does this for presets; the same field on a `Composition` lets an agent
say "like `explore`, plus these two tools". Bases resolve depth-first with cycle
detection, then are treated as layer 4 input — they never escape the parent
intersection.

**Bundles are a real registry concept, not a UI grouping.** A bundle is a named
pattern list referenced with a `@` sigil anywhere a tool pattern is accepted:

```json
{"bundles": {"files": ["read", "write", "edit"], "search": ["glob", "grep", "read"]}}
```

Built-ins: `@files`, `@search`, `@shell`, `@tasks`, `@delegation`, `@mcp`,
`@all`. Bundles may reference bundles; cycles resolve empty with an advisory
conflict. Expansion happens *before* selection — a bundle flattens to patterns,
and then one `select()` call runs against the pool, so there is still exactly
one matching code path.

They are a registry concept for two reasons, both of which are the reason the
kernel exists at all. First, a UI-only grouping stores five tool names in
`settings.json`; when a sixth file tool ships, every consumer silently keeps the
old five, and the settings file has drifted from what the app can do. A named
reference updates every consumer at once. Second, the introspection endpoint must
be able to say "`write` is here because of `@files`, which came from the preset"
— provenance through a UI grouping is not representable, because the UI grouping
does not exist by the time the value is resolved.

Bundles register as `PluginSpec(kind="bundle")`, which adds one member to the
`Kind` literal in `spec.py`. They therefore appear in the plugin list, carry a
tier (built-ins `confirm`, user bundles `free`), and expose a view rendering
their expansion against the live pool — so "what is in `@files` on this machine
right now" is answerable without running anything.

## 7. The orchestrator as a configurable thing

Today the main agent's identity is implicit: `name="main"`, tools from a preset
glob, prompt from `render_system_prompt(orchestration=bool(preset.agents))`, mode
from a three-way fallback. Under this model it is an ordinary composition under
the reserved id `@orchestrator`, and everything about it is authorable:

- **Its own instructions** — `section_bodies` on `@orchestrator`, which is what
  `Preset.prompt_overrides` becomes, plus the ability to add sections scoped to
  it alone.
- **Its own tool set** — `tools`, including the ability to *not* have tools its
  children have.
- **Its delegation policy** — `spawns` (which agents), `MAX_DEPTH` via the
  `runtime.subagents` plugin (how deep), and `parallel_spawns` (whether the loop
  may `gather` several `agent` calls in one round; today always yes, because the
  tool declares `is_read_only=True`).
- **What it may not do itself** — the interesting one, and the reason this is
  worth building.

### Worked example: an orchestrator that may not edit

```json
{
  "presets": {
    "delegator": {
      "title": "Delegator",
      "description": "Plans and delegates. Cannot touch files itself.",
      "orchestrator": {
        "tools": ["@search", "@tasks", "agent", "send_message", "plan"],
        "spawns": ["explore", "implement"],
        "models": ["orchestrator"],
        "ceiling": "auto-edit",
        "section_bodies": {
          "prompt.autonomy": "You do not edit files or run commands yourself. You investigate with read/glob/grep, write a plan to the task board, and delegate every mutation to an `implement` subagent with a bounded, non-overlapping file scope. If a change is too small to delegate, say so and ask the user to make it."
        }
      },
      "agents": {
        "explore": {},
        "implement": {
          "base": "general",
          "tools": ["@files", "@shell"],
          "models": ["worker"],
          "ceiling": "auto-edit",
          "max_turns": 40
        }
      },
      "bindings": [
        {"plugin": "mcp.docs", "to": "@subagents"},
        {"plugin": "prompt.verification", "to": "agent:implement"}
      ]
    }
  }
}
```

What this composition asserts, and where each assertion is enforced:

- The orchestrator has no `write`, `edit` or `bash` — enforced structurally by
  the tool list, not by prompt instruction. A tool the model cannot see is a tool
  it cannot try, the same reasoning `PlanModeHook` already uses.
- `implement` gets `@files` and `@shell` **and the parent does not have them**.
  This is legal: the parent intersection applies to what the *parent holds*, and
  the parent here is the orchestrator, whose grant is `@search`… — so under a
  strict reading `implement` would be narrowed to nothing. That is wrong, and it
  is the one place the rule needs an explicit exception.

**The exception, stated precisely:** the orchestrator's *grant* and the
orchestrator's *pool* are different sets (§3). The parent set that children are
intersected against is the **pool**, not the grant, at depth 0 only. Below depth
0, a child's pool *is* its grant, so narrowing compounds normally. Rationale: the
orchestrator's restriction is a statement about what the orchestrator does with
its own hands, not a statement about the session's capability envelope — that
envelope is `@session`. Without this, "delegate everything" would be
indistinguishable from "the session can't edit files", and the second is what
`explore` preset already expresses.

The consequence must be documented in the UI: **restricting the orchestrator's
tools does not restrict the session.** To restrict the session, revoke at
`@session`, which removes the tool from the pool and therefore from every agent
at every depth.

- `mcp.docs` reaches `explore` and `implement` but not the orchestrator — the
  binding the current model cannot express at all.
- `prompt.verification` is composed into `implement`'s prompt only. The
  orchestrator does not run tests; telling it to verify would be noise.

## 8. Introspection

This is the part that makes the model understandable. Every resolved value
carries provenance: which layer set it, which rule matched, and what the value
was before that layer.

Routes (paired with a `/api/projects/{pid}/…` mirror, matching every existing
route in `server/app.py`):

```
GET  /api/agents                         list of composable agents incl. @orchestrator
GET  /api/agents/{id}/resolved           the read model below
GET  /api/bindings                       the flat binding table (the "what is attached where" matrix)
GET  /api/bundles                        bundles with their live expansion
```

Query parameters on `/resolved`, all optional:

- `?preset=<id>` — resolve against a preset other than the active one. This is
  what a preset editor previews against.
- `?parent=<agent-id>` — resolve *as a child of* that agent. Without it, the
  parent intersection is skipped and the response is the agent's standalone
  composition. With it, `narrowed_by` fields appear. This is how the UI shows
  "explore under the delegator orchestrator" versus "explore in the abstract".
- `?conv=<conv_id>` — resolve against a live conversation's **frozen** snapshot
  instead of current settings. The response sets `"frozen": true` and the UI must
  label it, otherwise the screen lies about running sessions (§9).

### Response shape

```json
{
  "id": "explore",
  "role": "subagent",
  "title": "explore",
  "frozen": false,
  "resolved_against": {"preset": "delegator", "parent": "@orchestrator", "conv": null},

  "tools": [
    {
      "name": "read",
      "granted": true,
      "provenance": {
        "layer": "agent_definition",
        "source": "builtin:explore",
        "path": null,
        "rule": "read",
        "via_bundle": null
      }
    },
    {
      "name": "mcp__docs__search",
      "granted": true,
      "provenance": {
        "layer": "preset",
        "source": "preset:delegator",
        "path": ".quickcode/settings.json#/presets/delegator/bindings/0",
        "rule": "mcp__docs__*",
        "via_bundle": null
      }
    },
    {
      "name": "write",
      "granted": false,
      "provenance": {
        "layer": "agent_definition",
        "source": "builtin:explore",
        "rule": null,
        "reason": "not selected by this agent's tool patterns"
      }
    }
  ],

  "prompt": {
    "text": "<identity>\nYou are a QuickCode subagent…",
    "sections": [
      {
        "id": "prompt.identity",
        "title": "Identity",
        "order": 10,
        "start": 0,
        "end": 214,
        "tier": "confirm",
        "provenance": {"layer": "internal_default", "source": "prompts/sections.py", "overridden": false}
      },
      {
        "id": "prompt.tone",
        "title": "Tone and style",
        "order": 20,
        "start": 216,
        "end": 703,
        "tier": "free",
        "provenance": {
          "layer": "project_settings",
          "source": "plugin:prompt.tone",
          "path": ".quickcode/settings.json#/plugins/prompt.tone/settings/body",
          "overridden": true,
          "previous_layer": "internal_default"
        }
      }
    ]
  },

  "model": {
    "default": "anthropic/claude-sonnet-4",
    "allowed": ["worker"],
    "selectable": true,
    "provenance": {
      "default": {"layer": "agent_definition", "source": "builtin:explore", "rule": "worker",
                  "resolved_via": "profile.resolve('worker')"},
      "allowed": {"layer": "preset", "source": "preset:delegator", "rule": ["worker"],
                  "narrowed_by": null}
    }
  },

  "permissions": {
    "ceiling": "ask",
    "effective_if_spawned_now": "ask",
    "provenance": {
      "ceiling": {"layer": "agent_definition", "source": "builtin:explore", "rule": "ask"},
      "effective": {"layer": "parent", "source": "@orchestrator",
                    "rule": "cap_mode(parent=auto-edit, cap=ask)"}
    }
  },

  "spawns": [
    {"id": "explore", "allowed": false,
     "provenance": {"layer": "runtime", "rule": "depth 2 >= MAX_DEPTH"}}
  ],

  "settings": {
    "runtime.agent_loop.max_rounds": {
      "value": 50,
      "provenance": {"layer": "internal_default", "source": "manifest.py"}
    }
  },

  "conflicts": [
    {
      "class": "advisory",
      "kind": "tool_not_installed",
      "plugin": "mcp.docs",
      "requested": "mcp__docs__*",
      "message": "MCP server 'docs' is configured but not connected in this session.",
      "layer": "preset",
      "source": "preset:delegator"
    }
  ],

  "digest": "sha256:6f1c…"
}
```

Rules the shape enforces:

- **Every resolved value has a `provenance` object.** Not "most"; every one. A
  value without provenance is a value nobody can explain, and explaining is the
  entire purpose.
- **Denied things are listed, not omitted.** `"granted": false` with a reason is
  the answer to "why doesn't my agent have `write`", which is the question people
  actually ask. An omitted key answers nothing.
- **`layer` is one of the seven names in §4's table**, plus `parent` and
  `runtime` for the two non-configuration sources. This is a closed vocabulary
  the UI can colour-code.
- **`path` is a JSON Pointer into a real file** whenever one exists, so the
  existing "open the underlying configuration file" affordance works from here.
- **`digest`** is the stable hash of the resolved composition. It is what a
  session records, what `?conv=` compares against, and what tells the UI that a
  running session no longer matches its preset.

`GET /api/bindings` returns the same facts transposed — one row per binding, with
the agents it reaches — because "what does this MCP server attach to" and "what
does this agent have" are the two directions people navigate, and only one of
them is answerable from a per-agent view.

## 9. Migration

Nothing on disk breaks, and no session already recorded changes behaviour.

**Presets.** `Preset.from_dict` gains a compatibility branch: a body with no
`orchestrator` key is read as the legacy shape and lifted.

| Legacy field | Becomes |
|---|---|
| `tools` | `orchestrator.tools` — verbatim; `select()` semantics are unchanged |
| `agents` | `orchestrator.spawns` |
| `prompt_overrides` | `orchestrator.section_bodies` — scoped to `@orchestrator`, *not* `@all` |
| `settings` | bindings with `to: "@session"`, `effect: "set"` |
| `default_mode` | `orchestrator.ceiling` and the session's starting mode |
| `base` | `orchestrator.base` |

`prompt_overrides → @orchestrator` is the one place migration must resist
improving things. Today those overrides reach only the main agent; lifting them
to `@all` would silently change every existing preset's subagent prompts. The
new capability is opt-in.

**Agent definitions.** Frontmatter keys map one-to-one: `tools → composition.tools`,
`mode_cap → composition.ceiling`, `models`/`model`/`model_selectable` unchanged,
`max_turns`/`color`/`skip_project_instructions` unchanged. New optional keys:
`role`, `base`, `spawns`, `sections`. A file with none of them parses exactly as
it does today.

**Sessions on disk.** They record `preset=<id>` in a `meta` record and nothing
else. Two cases on resume:

- *No `composition` meta record* (every session written before this change) —
  fall back to `preset_module.resolve(cwd, recorded_id)` and re-resolve, which is
  precisely the current behaviour, including the fallback to `standard` when the
  preset is gone. Nothing changes for these sessions.
- *A `composition` meta record exists* — resume from the recorded payload and do
  not re-resolve. New sessions write it at open, alongside the existing
  `preset=<id>` (which stays, for display and for "start a new session like this
  one"). This is strictly better than today: deleting a preset no longer degrades
  a resumed conversation into `standard`.

**Settings files.** New keys (`bundles`, `presets.*.orchestrator`,
`presets.*.bindings`) are additive. A file written by the new version and read by
an older build loses those keys and falls back to the legacy fields, which
`save_preset` continues to emit alongside the new shape for one release. Note the
asymmetry: legacy readers get a *degraded but valid* composition, never a broken
one.

## 10. Implementation sketch

Ordered so each step is independently shippable and testable.

1. **`kernel/composition.py`** — `Composition`, `Binding`, `Selector`,
   `Conflict`, `Resolved`. Pure data, no I/O, no imports from `tools/` or
   `subagents/`. `Resolved` owns `to_json()` and `digest()`.
2. **`kernel/bundles.py`** — built-in bundles, loader from settings, cycle-safe
   `expand(patterns) -> list[str]`. Add `"bundle"` to `Kind` in `spec.py` and a
   `bundle_specs()` builder in `manifest.py`.
3. **`kernel/resolve.py`** — `resolve_composition(agent_id, *, pool, preset,
   defs, cwd, parent: Resolved | None, overrides) -> Resolved`. Total; collects
   conflicts; records provenance for every value. This is the only module that
   knows the layer order, and it is where the §4 table lives as code.
4. **`preset.py`** — `Preset` keeps `id`/`title`/`description`/`builtin`/`base`
   and gains `orchestrator: Composition`, `agents: dict[str, Composition]`,
   `bindings: tuple[Binding, ...]`. Legacy fields become read-only properties
   derived from `orchestrator`, so `select_tools(preset, pool)` keeps working
   during the transition and can be deleted in step 8.
5. **`subagents/definitions.py`** — `AgentDef` gains `role` and `composition`;
   existing scalar fields become properties over `composition` so
   `manifest.agent_specs()` and the current UI keep reading them unchanged.
   `load_defs` reserves `@orchestrator` and refuses `role: orchestrator` on any
   other id.
6. **`prompts/`** — `render_subagent_prompt` is reimplemented over
   `sections.compose()` with a subagent-flavoured section set, so a subagent
   prompt gains per-section provenance and section bindings become possible.
   `PromptContext` gains `role` and `agent_id`; the existing subagent template
   becomes three sections (`subagent.identity`, `subagent.role`,
   `prompt.environment` reused). Byte-stability is preserved because composition
   is deterministic in `order`.
7. **`subagents/runner.py`** — the behavioural core:
   - `SubagentDeps` drops `tool_pool` and `allowed_agents`, gains
     `parent: Resolved` and `defs: dict[str, AgentDef]` (a frozen snapshot,
     see §11.5).
   - `spawn_subagent` calls `resolve_composition(agent_type, parent=deps.parent,
     …)`, raises on any `blocking` conflict *before* minting the id, and builds
     the registry from `resolved.tools`.
   - `deps.child()` passes the **child's** `Resolved` as the next `parent`. This
     one line is the grandchild-escalation fix.
   - Advisory conflicts become the child's first-turn `<system-reminder>` and a
     `binding_narrowed` bus event.
8. **`kernel/bootstrap.py`** — register bundle specs; enrich `agent_specs` with
   resolved metadata; delete `preset.select_tools` once nothing calls it.
9. **`server/` (described only — another agent owns these files).**
   `manager.py::open()` would, after building the tool pool: resolve
   `@orchestrator` against the preset and pool; build the `ToolRegistry` from
   `resolved.tools`; pass `resolved.section_bodies` into `render_system_prompt`;
   clamp the starting `Mode` to `resolved.ceiling`; construct `SubagentDeps` with
   `parent=resolved` and a frozen `defs` snapshot; and `store.append_meta(
   composition=resolved.to_json(), digest=resolved.digest())` for new sessions,
   preferring the recorded payload on resume. `app.py` would add the four routes
   in §8 and their `/api/projects/{pid}/…` mirrors, each a thin call into
   `kernel/resolve.py` plus `Resolved.to_json()`.

## 11. Where this collides with "a session's composition is frozen at start"

The invariant is stated in `preset.py`: *changing the preset mid-flight would
change the tools under a conversation that has already been told what it has.*
Seven collisions, each with a decision.

1. **Mode is live, the ceiling is frozen.** `Conversation.set_mode` currently
   checks only yolo acceptance. It must additionally refuse any mode above
   `resolved.ceiling`, with the same error-event shape it uses for yolo.
   Otherwise the delegator preset's `ceiling: auto-edit` is advisory decoration.
2. **`set_model` re-renders the system prompt mid-session.** It already does, and
   already violates byte-stability deliberately (switching models rebuilds the
   cache anyway). It must re-render from the **frozen** `section_bodies`, not
   from a fresh `prompt_overrides(cwd)` read — otherwise switching model silently
   applies prompt edits made since the session opened.
3. **MCP servers connect at launch, before any conversation exists.** A
   per-agent MCP binding therefore selects from already-connected servers; it can
   never start one. A binding naming a disconnected server is advisory ("not
   here", §5.2). Starting servers per session is a separate design.
4. **Settings edits are live; sessions are not.** `PUT /api/kernel/plugins/{id}`
   changes bundles and bindings immediately. `/api/agents/{id}/resolved` without
   `?conv=` therefore describes *new* sessions, and with `?conv=` describes a
   *running* one. The two can differ, the response carries `"frozen"` to say
   which it is, and the UI must render the difference rather than hide it — a
   settings screen that shows a running session values it does not have is worse
   than no screen.
5. **`load_defs(deps.cwd)` runs on every spawn.** Editing an agent `.md` today
   changes the children of a session already in flight, which contradicts the
   invariant the presets obey. Decision: snapshot `load_defs()` once at session
   open into `SubagentDeps.defs`. Consequence: editing an agent definition
   requires a new session to take effect, consistent with presets, and the UI
   must say so on the agent editor.
6. **Deleted presets currently degrade resumed sessions to `standard`.**
   Recording the resolved composition removes that failure mode. The fallback in
   `preset.resolve()` stays for the layer-3 path and for new sessions.
7. **Permission *rules* stay live while the *ceiling* is frozen.**
   `Rules.persist_allow` appends during a session, on purpose — "always allow
   this command" would be useless otherwise. The distinction to hold: rules
   decide *this call*, the ceiling decides *what is ever possible*. Only the
   second is composition, and only the second is frozen.
