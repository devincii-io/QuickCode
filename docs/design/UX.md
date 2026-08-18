# Configuration UX — the interface over the plugin kernel

This is the interface design for QuickCode's configuration surface. Two sibling
designs own the model underneath it: **AUTHORING** (what is creatable, the file
formats, duplicate-to-customise) and **BINDING** (how plugins attach to the
orchestrator and to named agents, resolution order, and the
`GET /api/agents/{id}/resolved` introspection endpoint that returns every
resolved value with its provenance). This document assumes both exist and
designs only what a human sees and touches. Where it needs a field or an
endpoint that does not exist yet, it names it exactly, in §3 and §9.

---

## 1. What is actually wrong

The complaints are specific and each one traces to a line of code, so the fixes
can be too.

**"Pretty involved."** `manifest.py` produces exactly 37 internal plugins — 8
runtime internals, 13 prompt sections, 13 tools, 2 agents, 1 provider. All 37
land in one `renderPluginsPage` list (`js/settings/plugins.js:80`) filtered by a
four-control toolbar. Grouping by `spec.group` gives seven headings ("Agent
loop", "Safety", "Prompt", "Files", "Shell", "Tasks", "Agents"), which is a
grouping by *implementation neighbourhood*, not by anything a user came to do.
There is no first screen that says what to do first.

**"No explaination."** `SettingSpec` carries `title`, `help` and `risk`. `help`
says what a setting *is* ("A round is one model response plus the tools it asked
for"). Nothing says what it *affects*. `risk` is populated only for the confirm
tier and is phrased as a warning, so a free-tier knob gets no consequence text
at all and a locked one gets nothing but `LOCKED_NOTE`, a single constant string
reused for all 14 locked settings (`js/settings/fields.js:14`).

**"No destinciton between anything."** `cardHtml` (`plugins.js:40`) renders one
markup shape for every kind. `prompt.tone` (a block of English prose that ends
up in the model's context), `tool.bash` (a callable with a JSON schema),
`runtime.permissions` (the rule set that decides whether a write is allowed) and
an MCP server (an external subprocess) are the same grey rounded rectangle with
the same caret, the same "N settings · M locked" subtitle, and kind expressed as
a 10px chip. `settings.css:67-72` even gives `k-agent` and `k-mcp_server` the
same colour.

**"No context based views of a agent."** `explore`'s configuration is scattered:
its five settings are on the Agents & Presets page (`presets.js:60`), its
instructions are behind a **View** button that opens a read-only sheet
(`ui.js:154`), its tool grant is a `metadata.tools` array rendered as three
`<code>` tags with no way to edit them, its actual composed prompt is nowhere in
the UI at all (`prompts/subagent.py:46` composes it at spawn time), and the
permission ceiling that governs it sits in one page while the depth cap that
governs whether it can spawn at all sits in another (`runtime.subagents`).

---

## 2. Two structural decisions

### 2.1 Configuration becomes a view, not a modal

Today `openSettings()` (`modals.js:569`) puts everything inside a 1120px dialog
with a 172px rail and a 78vh scroll box, and the affordances that must appear
over it — the confirm dialog, the raw inspector — open as a hand-rolled sheet
stack inside `#modal-root` because `modal()` clears its own root
(`js/settings/ui.js:1-8`). That stack is already the symptom.

Make it the third top-level view: `#view-config`, alongside `#view-home` and
`#view-workspace`. The reasons, in order of weight:

1. **The workbench is three columns.** An agent's editor and the live preview of
   the bytes it will receive have to be visible at once — that side-by-side *is*
   the answer to "fully understandable". At 1120px minus a rail there is no room
   for a 460px preview pane, and putting the preview behind a tab destroys the
   thing it is for.
2. **Everything here deserves a URL.** A trajectory event should link to the
   agent that produced it (`#/config/agents/explore`); a permission denial
   should link to the policy that denied it; a tool card in chat should link to
   its schema. A modal cannot be linked to.
3. **The creation flows are multi-step with live preview.** A guided form for a
   shell tool inside a dialog inside a dialog is not a design, it is a
   concession.
4. **The escape hatches want space.** "Edit as file", the raw JSON view, and a
   composed 6,000-character prompt are all full-width content.

The session keeps running while the view is open. `showHome()` currently calls
`disconnect()` (`main.js:27`); `showConfig()` must not — leaving the chat to
change a setting and losing the socket is exactly the friction that stops people
changing settings. The workspace stays mounted and hidden, as it already is when
Home shows.

One thing stays a modal: **Quick settings**, opened from the composer, holding
only the three install-level things people change mid-conversation without
wanting to leave — provider endpoint, API key, theme. It is a shortcut into the
Install section of the view, and says so with a "Open full configuration →"
link.

### 2.2 The agent is the primary object; a composition is an agent identity

The navigation leads with agents because that is the noun a user has an
intention about. "I want it to stop rewriting my whole file" is a sentence about
an agent, and it should resolve in one place.

The second decision follows from looking at what a preset actually is. A
`Preset` (`kernel/preset.py:34`) holds `tools`, `agents`, `prompt_overrides`,
`settings` and `default_mode`. An `AgentDef` (`subagents/definitions.py:23`)
holds `tools`, `model`, `models`, `mode_cap`, `max_turns` and `prompt_body`.
These are the same object seen twice: *the configuration of one agent*. A preset
is the orchestrator's configuration under a switchable name; a definition is a
subagent's under its own name.

So there is **one workbench component with two entry points**. Opening the
composition "Explore" and opening the subagent `explore` render the same six
sections against different backing stores. The identity header differs (a
composition says "active for new sessions"; a subagent says "spawnable by the
orchestrator") and the Delegation section is only meaningful for something that
can spawn. Everything else — instructions, tools, models, limits, preview — is
literally the same code. This is the single largest simplification available and
it is why compositions sit as a peer of agents in the navigation rather than
buried in a settings page.

---

## 3. Information architecture

```
┌ QuickCode ▸ Configuration ──────────────────── [⌕ search everything]  [Done ✕] ┐
│                                                                                │
│  AGENTS                    │                                                   │
│   ▸ Orchestrator           │                                                   │
│   ▸ explore                │            (the selected page renders here)       │
│   ▸ general                │                                                   │
│   + New agent              │                                                   │
│                            │                                                   │
│  COMPOSITIONS              │                                                   │
│   ▸ Standard      ✓ active │                                                   │
│   ▸ Minimal                │                                                   │
│   ▸ Explore                │                                                   │
│   + New composition        │                                                   │
│                            │                                                   │
│  PARTS                     │                                                   │
│   ⌗ Tools              13  │                                                   │
│   ¶ Prompt             13  │                                                   │
│   » Models & providers  1  │                                                   │
│   :: MCP servers        0  │                                                   │
│   § Policies & limits   5  │                                                   │
│                            │                                                   │
│  MACHINE ROOM           4  │                                                   │
│  INSTALL                   │                                                   │
└────────────────────────────┴───────────────────────────────────────────────────┘
```

The split is by the question you arrived with, not by the kind hierarchy:

| You came to ask | Section |
|---|---|
| "make this agent behave differently" | **Agents** |
| "which agent do new sessions start as" | **Compositions** |
| "what can it do at all / add a capability" | **Parts** |
| "why does it do that / what is fixed and why" | **Machine room** |
| "where do the models come from" | **Install** |

**Parts** is the plugin inventory, split by kind rather than by `spec.group`,
because kind is the only grouping that predicts what a page looks like. Each
Parts page is a browser over one kind with the body layout that kind deserves
(§5), and each row opens a detail page. Tools, Prompt and MCP each get a
"+ New…" affordance; Models and Policies do not, because nothing in this build
authors a provider or a permission engine, and the page says so plainly rather
than showing a dead button.

**Machine room** holds the four plugins that are entirely locked —
`runtime.tool_protocol`, `runtime.session_log`, `hook.plan_mode`, and the
sanitizer half of `runtime.subagents`. They are pulled out of Parts on purpose:
mixed into a list of editable things they read as broken, and given their own
room with a lede that says "this is the contract everything else depends on;
here it is in full" they read as documentation, which is what they are.

The **flat list is retired as a page and becomes a search result surface.** The
search box in the header queries every plugin, setting, tool, prompt section and
composition by id, title, summary and body text, and lands the user on the
detail page with the match highlighted. A flat list of 37 (or 137, once MCP
servers arrive) is a *lookup* instrument, and lookup belongs behind a search
box, not in the navigation where it competes with the pages that teach.

The rail is the same rail on every page, so the answer to "where am I" is always
one glance, and every page has a URL: `#/config/agents/explore`,
`#/config/parts/tools/bash`, `#/config/machine-room/runtime.tool_protocol`.

---

## 4. The explanation layer

### 4.1 The six questions

Every plugin and every setting answers the same six questions in the same order,
everywhere it appears. Consistency is the whole point: once you have read one
explanation block you can read all of them without looking.

```
WHAT        one line, plain language, no jargon and no restating the title
AFFECTS     where it takes effect: prompt · tool list · loop · storage · ui ·
            permissions · models
WHO         orchestrator only / named agents / every agent / the install
IF CHANGED  what actually becomes different (all tiers)
WHY FIXED   only when locked: the reason it is not a knob
INSTEAD     only when locked: the recourse
```

### 4.2 Fields to add

These go in `kernel/spec.py`. The sibling designs implement them; here are the
exact names and contracts.

```python
Effect = Literal["prompt", "tool_list", "loop", "storage",
                 "ui", "permissions", "models"]
Audience = Literal["orchestrator", "named_agents", "all_agents", "install"]

@dataclass(frozen=True)
class Recourse:
    """What a user CAN do when the thing in front of them is fixed."""
    action: Literal["duplicate", "author", "settings", "docs", "none"]
    label: str          # "Duplicate this agent to get an editable copy"
    target: str = ""    # plugin id to duplicate, or kind to seed New…
```

Added to `PluginSpec`:

| field | type | contract |
|---|---|---|
| `summary` | `str` | ≤ 90 chars, one sentence, no ids, no "this plugin". Distinct from `description`, which stays as the longer paragraph. |
| `affects` | `tuple[Effect, ...]` | Every surface this touches. Empty is a bug, not a default. |
| `audience` | `Audience` | Who is affected. |
| `consequence` | `str` | What becomes different if this is disabled or changed. Neutral tone at every tier — this is not `risk`. |
| `locked_because` | `str` | Required when `tier_hint == "locked"` or any setting is locked. Names the invariant, e.g. "the trajectory replays by sequence number, so the record shape is fixed". |
| `recourse` | `Recourse \| None` | Required whenever `locked_because` is set. |
| `docs_anchor` | `str` | `"docs/PERMISSIONS.md#modes"` — the long form. |

Added to `SettingSpec`:

| field | type | contract |
|---|---|---|
| `affects` | `tuple[Effect, ...]` | Narrower than the plugin's. `max_rounds` affects `loop` only. |
| `effect_detail` | `str` | The mechanical sentence: "Caps the `timeout_ms` maximum the model sees in bash's schema." |
| `locked_because` | `str` | Per-setting; falls back to the plugin's. |
| `recourse` | `Recourse \| None` | Per-setting; falls back to the plugin's. |
| `example` | `str` | A good non-default value, used as the placeholder. |

**`risk` is not duplicated and not replaced.** The rule the UI applies:

- tier `free` → render `consequence` in the neutral note slot.
- tier `confirm` → render `consequence` in the neutral slot **and** `risk` in
  the amber slot. They are different sentences: `consequence` is "compaction
  runs earlier and keeps less detail", `risk` is "set too high and compaction
  runs too late to fit". The existing `risk` strings in `manifest.py` are
  already written this way and need no rewriting.
- tier `locked` → render `consequence` in the neutral slot, `locked_because`
  in the fixed-by-design block, `recourse` as the action line.

### 4.3 How locked reads

Locked must read as *documentation with an exit*, never as a greyed-out box.
Three rules, all enforceable in CSS:

1. **Neutral, never red.** `settings.css:62` already says this and gets it right
   — keep `--boost` background and `--line-strong` border. Nothing about locked
   uses `--error` or `--warning`.
2. **The value is fully legible and selectable.** Today a locked control is a
   `disabled` input with dashed borders (`settings.css:222`). Replace the input
   entirely: render the value as a `.raw` block for text, as a bold literal for
   scalars. A locked value is a fact to read, not a control to fail at.
3. **There is always a next line.** The `recourse` line is a real button, not
   prose. For an agent: "Duplicate `explore` to get an editable copy" → opens
   the New-agent flow pre-filled. For a prompt section: "Author a new section
   that runs after this one". For the tool protocol: "Read the full contract →"
   opening the raw view, plus "docs/TOOLS.md". Never a dead end.

The block, verbatim:

```
┌ Fixed by design ─────────────────────────────────────────────────────────┐
│ strict_schemas          true                                             │
│                                                                          │
│ Tool arguments are validated against the declared schema and unknown     │
│ fields are rejected.                                                     │
│                                                                          │
│ Why fixed — the tool_call_id round trip is what makes a session          │
│ replayable and auditable. A model that could invent argument fields      │
│ would produce calls the trajectory cannot reconstruct.                   │
│                                                                          │
│ [ Read the full protocol ]  [ docs/TOOLS.md ]                            │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 5. Visual distinction by kind

### 5.1 The rule that avoids a colour collision

Tier already owns colour: free is `--success`, confirm is `--warning`, locked is
neutral (`settings.css:56-66`). If kind also owned colour, an amber tool card
would read as a pending confirmation. So:

> **Kind owns the hue. Tier owns the badge and the stripe texture.**

Each card gets a 3px left stripe in its kind hue. Tier modulates the *texture*
of that stripe, not its colour: free is solid, confirm is solid with a 3px notch
at the top, locked is a 45° hatch drawn with `repeating-linear-gradient`. Tier
also keeps its existing text badge. A user reading at a glance sees hue first
(what sort of thing is this) and texture second (can I touch it), which is the
right priority.

### 5.2 Hues

Nine kinds, added to `app.css` next to the existing `--chip-*` block. Nothing is
hardcoded — every value is an existing token or a `color-mix` of one, so a theme
swap still works:

```css
--kind-tool:     var(--chip-tool);       /* amber   — a thing the model calls  */
--kind-prompt:   var(--chip-system);     /* violet  — words in the context     */
--kind-provider: var(--chip-user);       /* blue    — where tokens come from   */
--kind-agent:    var(--chip-agent);      /* cyan    — someone who acts         */
--kind-mcp:      var(--accent);          /* orange  — an external process      */
--kind-hook:     var(--chip-context);    /* teal    — the loop's lifecycle     */
--kind-policy:   color-mix(in srgb, var(--chip-review) 82%, var(--fg));
--kind-storage:  color-mix(in srgb, var(--chip-user) 45%, var(--fg-dim));
--kind-panel:    var(--fg-dim);          /* neutral — it is only UI            */
```

`--kind-mcp` deliberately equals `--accent`, which `settings.css:73` already
uses for `src-config` chips — every MCP server *is* source `config`, so this is
agreement, not a collision. The current shared cyan between agent and MCP is
removed.

### 5.3 Sigils

A two-character monospace tile, not an icon font and not an emoji. `app.css:133`
already records that `▸` collapses to a dot in several fallback mono stacks, so
every sigil below is ASCII or Latin-1 and cannot fail to render. Two-character
tiles also align in a list, which suits the dense aesthetic.

| kind | sigil | reading |
|---|---|---|
| `tool` | `fn` | a callable |
| `prompt_section` | `¶ ` | a paragraph of the prompt |
| `provider` | `» ` | where tokens flow from |
| `agent` | `@ ` | someone who acts |
| `mcp_server` | `::` | an external namespace |
| `policy` | `§ ` | a rule |
| `hook` | `()` | a lifecycle callback |
| `panel` | `[]` | a UI surface |
| `storage` | `db` | bytes at rest |

### 5.4 What the card body shows, per kind

The body is not one template. Each kind shows the fact you would have opened it
to find:

- **tool** — the signature, then the flags:
  `read(file_path, offset?, limit?) → numbered lines` · `R read-only` ·
  held by `orchestrator, general`.
- **prompt_section** — its order number, the first two lines in mono with a
  fade-out, and its position: `#20 · 1,204–1,588 in the composed prompt · 384
  chars`.
- **provider** — the endpoint host, model count, and whether it is active:
  `openai-compat · openrouter.ai · 312 models · active`.
- **agent** — model, tool count, ceiling, turns:
  `worker · 3 tools · ceiling ask · ≤30 turns`.
- **mcp_server** — the command it runs and what it contributed:
  `npx -y @modelcontextprotocol/server-git` · `connected` ·
  `24 tools →` (a link into the Tools page filtered to that server).
- **policy** — the effective values as a compact strip:
  `default_mode=ask · protect_outside_root=true`.
- **hook** — which loop phases it fires on: `before_request · after_tool`.
- **panel** — which tab it is, and whether it is shown.
- **storage** — the path pattern and current size:
  `.quickcode/sessions/*.jsonl · 41 files · 18 MB`.

---

## 6. The agent workbench

The centrepiece. One page per agent, everything about that agent, and a live
preview of the exact bytes it will receive.

### 6.1 Layout

```
┌ AGENTS ────┬ @ explore ──────────────────────────────┬ PREVIEW ───────────────┐
│ Orchestr.  │ Read-only investigation: search the     │ ( System prompt ) Tools│
│ ▸ explore  │ codebase/docs and report findings.      │ ────────────────────── │
│   general  │ built-in · spawnable · 3 tools · worker │  1,418 chars · 4 blocks│
│ + New      │                                         │                        │
│            │ ┌ Identity ───────────────────── free ┐ │ <identity>             │
│ ─────────  │ │ name      explore        (fixed)    │ │ You are a QuickCode    │
│ ON THIS    │ │ summary   Read-only investigation…  │ │ subagent named         │
│ PAGE       │ │ colour    ● cyan                    │ │ "explore", powered by  │
│ Identity   │ └─────────────────────────────────────┘ │ the model "worker".    │
│ Instructi. │ ┌ Instructions ───────────────── free ┐ │ You were spawned by    │
│ Tools    3 │ │ You are a read-only investigation   │ │ another agent to       │
│ Models     │ │ subagent. Your job is to search,    │ │ handle one             │
│ Limits     │ │ read, and analyze — never to modify │ │ self-contained task…   │
│ Delegation │ │ anything. You have read, glob, and  │ │ </identity>            │
│            │ │ grep only.                          │ │                        │
│ ─────────  │ │ …                             ✎ Edit│ │ <role>                 │
│ [Duplicate]│ │ lands at <role> · 512 chars         │ │ You are a read-only ◀──┤
│ [Open file]│ └─────────────────────────────────────┘ │ investigation subagent.│
│            │ ┌ Tools ────────────────── 3 of 37 ───┐ │ …                      │
│            │ │ read  glob  grep       [ Change… ]  │ │ </role>                │
│            │ │ 3 read-only · 0 mutating · 0 shell  │ │                        │
│            │ └─────────────────────────────────────┘ │ <environment>          │
│            │ ┌ Models ──────────────────────── free┐ │   <cwd>C:\…\QuickCode  │
│            │ │ default   worker  ▾                 │ │   <platform>win32      │
│            │ │ allowed   (any the provider offers) │ │ </environment>         │
│            │ │ caller may choose      [on]         │ │                        │
│            │ └─────────────────────────────────────┘ │ ⚠ project instructions │
│            │ ┌ Limits & permissions ───── confirm ─┐ │   omitted — this agent │
│            │ │ ceiling   ask       ▾               │ │   sets                 │
│            │ │ max turns 30                        │ │   skip_project_        │
│            │ │ depth     inherited: 2  → Policies  │ │   instructions         │
│            │ └─────────────────────────────────────┘ │                        │
│            │ ┌ Delegation ─────────────────────────┐ │ [⧉ Copy] [Raw]         │
│            │ │ cannot spawn — subagents are        │ │                        │
│            │ │ granted the delegation pair by      │ │                        │
│            │ │ depth, never by allowlist. Why →    │ │                        │
│            │ └─────────────────────────────────────┘ │                        │
└────────────┴─────────────────────────────────────────┴────────────────────────┘
```

Three columns at ≥1280px: rail 208px, editor `1fr` (min 520px), preview 460px
sticky. Below 1280px the preview collapses into a segmented tab above the
editor; below 900px the rail collapses to a select. The preview is never
removed, only relocated — it is the feature.

### 6.2 The sections

**Identity.** Name (immutable for built-ins, editable for authored ones and
explained as such), summary, colour. Small on purpose. It also carries the
provenance strip: `built-in · quickcode/subagents/definitions.py:72` for
`explore`, `.quickcode/agents/reviewer.md` for an authored one, with **Open
file** where a file exists.

**Instructions.** The `prompt_body`, edited inline in a mono textarea. Below it,
the sentence that makes it comprehensible: **"lands at `<role>` · 512 chars"** —
because `render_subagent_prompt` (`prompts/subagent.py:53`) wraps the body in
`<role>` between the identity block and the environment block, and until you
know that, editing the body is editing something whose position you cannot see.
Typing here updates the preview live, with the changed range highlighted.

**Tools.** A read-only summary plus **Change…**, which opens the picker (§7).
The summary line is the sentence a human needs and nobody currently writes:
`3 read-only · 0 mutating · 0 shell`. For an agent inheriting everything
(`tools: null`, which is `general`) it reads `everything the session's
composition allows — currently 13` with the count live.

**Models.** `model` (default), `models` (allowed set), `model_selectable`. The
allowed set is a pattern list with a resolved count next to it:
`anthropic/*  →  matches 14 of 312`. Off `model_selectable` renders as
"pinned to `worker` — an override is refused, not ignored", the phrasing already
in `manifest.py:397`.

**Limits & permissions.** `mode_cap` and `max_turns`, plus the inherited limits
that actually govern this agent but live elsewhere — `runtime.subagents.max_depth`
and `max_agents`. Showing them here as read-only rows with a link
(`depth  inherited: 2  → Policies`) is the fix for a real confusion: those two
caps decide whether this agent ever runs, and nothing on its page said so.
`mode_cap` keeps its confirm tier and its existing `risk` text — "This is the
most this agent may ever do, whatever mode the session is in. Subagents can
never ask you for permission."

**Delegation.** For the orchestrator: which agent definitions it may spawn, as a
pattern list against the live definitions, with a resolved preview. For a
subagent at the depth floor: the honest explanation that the delegation pair is
granted by depth and never by allowlist (`tools/registry.py:117`), with a link
to the depth setting. This section exists on every agent precisely so the answer
"it cannot, and here is why" has somewhere to be.

### 6.3 The preview pane

Two tabs, both showing bytes rather than descriptions.

**System prompt** — the exact composed string this agent will receive, from the
same code path the runner uses. Section boundaries are drawn as a thin left
margin with the section id on hover, using the offsets the resolve endpoint
returns, exactly as `js/settings/prompt.js:18` already slices the orchestrator's
prompt by code point. Editing the Instructions field re-renders it with the
changed range marked. A footer shows `1,418 chars · 4 blocks` and offers
**⧉ Copy** and **Raw**.

The pane also states what is *absent* and why. For `explore` it shows the
project-instructions block struck through with "omitted — this agent sets
`skip_project_instructions`", because an empty region is invisible and a
surprised user has no way to discover the flag caused it.

**Tools** — the exact JSON array of tool schemas, syntax-highlighted with the
existing `highlightJson` (`ui.js:113`), collapsed one schema per row and
expandable. The header states `3 tools · 1,902 bytes of schema`, which is the
number that matters when someone wonders why a cheap agent is not cheap.

Unsaved edits put both tabs into a diff mode: a **saved / draft** toggle plus a
"2 changes not saved" strip with **Save** and **Discard**. The draft preview
needs a backend call (§9): `POST /api/agents/{id}/preview` with the draft body,
returning the same shape as the resolve endpoint. Composing the preview in
JavaScript would recreate `prompts/subagent.py` in a second language and it
would drift — the whole point of the pane is that it is not a reconstruction.

---

## 7. The tool picker

### 7.1 What it must not do

The backing model is patterns, not a set. `AgentDef.tools` is a list resolved at
spawn time by `tools/registry.py:78`, supporting names, the `task` alias and
globs like `mcp__*`. A checkbox grid that writes explicit names would silently
convert `mcp__*` into a frozen snapshot, and the next MCP server the user adds
would not reach the agent. So the picker **edits patterns and shows their
effect**, and never rewrites one into the other behind the user's back.

### 7.2 Layout

```
┌ Tools for @explore ──────────────────────────────────────────────── [✕] ─┐
│ Grant patterns                                                           │
│  [ read ×] [ glob ×] [ grep ×]  [+ pattern…]     matched 3 of 37 tools   │
│  ○ inherit everything the composition allows (tools: null)               │
│                                                                          │
│ ⌕ filter…                            [ all ] [ granted ] [ read-only ]   │
│                                                                          │
│ FILES                                                            4 tools │
│  ✓ fn read    read(file_path, offset?, limit?)            R   ← "read"   │
│  ✓ fn glob    glob(pattern, path?)                        R   ← "glob"   │
│  ✓ fn grep    grep(pattern, path?, glob?, output_mode…)   R   ← "grep"   │
│  ○ fn write   write(file_path, content)                   W              │
│  ○ fn edit    edit(file_path, old_string, new_string…)    W              │
│ SHELL                                                            1 tool  │
│  ○ fn bash    bash(command, description?, timeout_ms?)    W  shell       │
│ TASKS                                                            4 tools │
│  ○ ⌄ task_*   4 tools — create, update, list, get         R/W            │
│ SUBAGENTS                                                        2 tools │
│  ⊘ fn agent          granted by depth, never by pattern  →why            │
│  ⊘ fn send_message   granted by depth, never by pattern  →why            │
│ MCP · company-kb                                                24 tools │
│  ○ ⌄ mcp__company-kb__*     24 tools, 22 read-only        R/W  [grant all]│
│                                                                          │
│ ─────────────────────────────────────────────────────────────────────── │
│ 3 tools · 3 read-only · 0 that change files · 0 that run shell commands  │
│                                              [ Cancel ]  [ Apply ]       │
└──────────────────────────────────────────────────────────────────────────┘
```

### 7.3 The mechanics

**Four row states, not two.** `✓` granted by an explicit pattern, `≈` granted by
a glob (with the glob named in the "←" column), `○` not granted, `⊘` not
grantable. The fourth state is the one that stops the picker lying: `agent` and
`send_message` are stripped by `build_registry` regardless of the allowlist
(`tools/registry.py:118`), so offering a checkbox for them would offer a
promise the runtime breaks.

**The "←" provenance column** answers "what came from where" for every granted
row: the literal pattern, the glob, or `inherited` when `tools: null`. With MCP
present this is the difference between a comprehensible list and a mystery.

**Clicking rows edits patterns.** Clicking `○` adds the literal name. Clicking
`✓` removes that pattern. Clicking `≈` (glob-granted) offers **exclude this
one**, which writes `!mcp__company-kb__kb_add_note`. That needs a two-line
addition to `select()` — negative patterns applied after positives — named as a
dependency in §9. Without it the only honest alternative is expanding the glob,
which reintroduces the freezing problem, so the exclusion is worth the two
lines.

**Bulk arrives collapsed.** Any family over 6 tools (MCP servers, `task_*`)
renders as one summary row with a count and a disclosure. `[grant all]` writes
the glob, not 24 names. Expanded, each child row is individually grantable and
excludable.

**R/W is a fixed column, never a colour.** `R` for `is_read_only`, `W` for
mutating, plus a `shell` marker for `PermissionSpec(shell=True)`. Read-only
tools skip the permission prompt and run in parallel (`manifest.py:356`), which
makes this the single most consequential fact about a tool, so it gets a column
of its own rather than a badge that competes with tier and kind.

**The footer sentence is the deliverable.** "3 tools · 3 read-only · 0 that
change files · 0 that run shell commands" is what a person actually wants to
know about a grant, and it updates as they click. For the orchestrator on
Standard it reads "13 tools · 7 read-only · 3 that change files · 1 that runs
shell commands", which is a genuinely useful thing to have discovered.

---

## 8. Plugin detail at the three tiers

### 8.1 free — `prompt.tone`

```
┌ ¶ Tone and style ──────────────────────── prompt_section · free ──── #20 ┐
│ How replies read: length, preamble, narration.                           │
│                                                                          │
│ AFFECTS   [prompt]                                                       │
│ WHO       the orchestrator · not sent to subagents                       │
│ IF CHANGED  Every reply in every new session changes shape. Sessions      │
│           already running keep the text they started with — the prompt   │
│           is byte-stable within a session.                               │
│                                                                          │
│ ┌ Section text ──────────────────────────────────────────── body ──────┐ │
│ │ <tone_and_style>                                                     │ │
│ │ Your output renders as markdown in the chat pane. Be concise and     │ │
│ │ direct.                                                              │ │
│ │                                                                      │ │
│ │ - Answer simple questions in 1-4 lines. No preamble ("Great          │ │
│ │   question!"), no postamble ("Let me know if..."), no restating…     │ │
│ └──────────────────────────────────────────────────────────────────────┘ │
│                              [ Save ] [ Revert ]  [ Restore the default ] │
│                                                                          │
│ WHERE IT LANDS   characters 1,204–1,588 of the composed prompt           │
│                  [ Show it in the prompt → ]                             │
└──────────────────────────────────────────────────────────────────────────┘
```

**Restore the default** is new and belongs at every free and confirm tier: the
registry already keeps declared defaults separate from persisted overrides
(`registry.py:89`), so "put it back" is a one-line write and its absence is the
reason people are afraid to touch a text box.

### 8.2 confirm — `runtime.subagents.max_depth`

```
┌ () Subagents ───────────────────────────────── hook · confirm ───────────┐
│ Limits on how far the agent may fan work out.                            │
│                                                                          │
│ AFFECTS   [loop] [tool list]                                             │
│ WHO       every agent, at every depth                                    │
│                                                                          │
│ ┌ Maximum nesting depth ──────────────────────────────── max_depth ────┐ │
│ │  [ 2 ]  0 … 4                                                        │ │
│ │                                                                      │ │
│ │  Effect  At depth 0 the orchestrator gets `agent` and `send_message`;│ │
│ │          at the floor they are withheld entirely, so an agent that   │ │
│ │          cannot spawn never sees the tools.                          │ │
│ │  ⚠ Risk  Deeper trees multiply cost fast and make a run hard to      │ │
│ │          follow.                                                     │ │
│ │                                       [ Save ] [ Revert ] [ Default ]│ │
│ └──────────────────────────────────────────────────────────────────────┘ │
│ ┌ Maximum agents per session ───────────────────────── max_agents ─────┐ │
│ │  [ 50 ]  1 … 500      ⚠ This is the backstop against a runaway       │ │
│ │                          fan-out loop.                               │ │
│ └──────────────────────────────────────────────────────────────────────┘ │
│ ┌ Fixed by design ─────────────────────────────────────────────────────┐ │
│ │ Neutralize control tags in subagent output          sanitize_reports │ │
│ │ true                                                                 │ │
│ │ Why fixed — a subagent's text was produced by a model that may have  │ │
│ │ read files an attacker controls. Control tags are neutralized before │ │
│ │ that text re-enters the parent's context.                            │ │
│ │                          [ Read the sanitizer contract ]             │ │
│ └──────────────────────────────────────────────────────────────────────┘ │
│                                                                          │
│ USED BY   Orchestrator (spawns explore, general) · every subagent        │
└──────────────────────────────────────────────────────────────────────────┘
```

The confirm dialog itself needs no redesign — `confirmRisk` (`ui.js:86`) already
puts the server's own 409 detail in front of the user instead of a bare "are you
sure?", which is the correct behaviour and stays.

### 8.3 locked — `runtime.tool_protocol`

```
┌ § Tool call protocol ─────────────────── policy · locked ── machine room ┐
│ How tools are declared to the model and how calls come back.             │
│                                                                          │
│ AFFECTS   [tool list] [loop] [storage]                                   │
│ WHO       every agent, always                                            │
│ WHY FIXED This handshake is what makes a session replayable and          │
│           auditable. Change it and the trajectory can no longer          │
│           reconstruct what happened.                                     │
│                                                                          │
│ ┌ Fixed by design ─────────────────────────────────────────────────────┐ │
│ │ Strict argument schemas                              strict_schemas  │ │
│ │ true                                                                 │ │
│ │ Tool arguments are validated against the declared schema; unknown    │ │
│ │ fields are rejected.                                                 │ │
│ └──────────────────────────────────────────────────────────────────────┘ │
│ ┌ Run read-only tools in parallel ───────────────────────── confirm ───┐ │
│ │  [on]   Reads, globs and greps in one round run concurrently.        │ │
│ │  ⚠ Turning this off makes every session slower. Turning it on for    │ │
│ │    tools that are not genuinely read-only can interleave writes      │ │
│ │    unpredictably.                                                    │ │
│ └──────────────────────────────────────────────────────────────────────┘ │
│                                                                          │
│ THE CONTRACT, IN FULL                                    [⧉ Copy] [Raw]  │
│ ┌──────────────────────────────────────────────────────────────────────┐ │
│ │ QuickCode declares every enabled tool to the model as a JSON Schema  │ │
│ │ derived from the tool's pydantic Input model, with                   │ │
│ │ additionalProperties set to false so the model cannot invent fields: │ │
│ │   { "name": "read", "description": "...", "parameters": {…} }        │ │
│ │ …                                                                    │ │
│ └──────────────────────────────────────────────────────────────────────┘ │
│                                                                          │
│ INSTEAD   You cannot change this handshake, but you can change what      │
│           flows through it: [ New shell tool ] [ Tool grants per agent ] │
└──────────────────────────────────────────────────────────────────────────┘
```

Note the mixed-tier case handled honestly: this plugin has one locked setting
and one confirm setting, and each renders in its own frame rather than the whole
card taking the strictest badge and looking uneditable. The card badge still
shows `locked` (that is `PluginSpec.tier()`'s contract) but the body does not
lie about the knob you can actually turn.

---

## 9. Creation flows

### 9.1 What is creatable

Five kinds, each with a New… entry in the section where it belongs:

| kind | writes | entry point |
|---|---|---|
| agent | `.quickcode/agents/<name>.md` | Agents ▸ + New agent |
| shell tool | `.quickcode/tools/<name>.json` | Parts ▸ Tools ▸ + New tool |
| prompt section | `.quickcode/prompt/<id>.md` | Parts ▸ Prompt ▸ + New section |
| MCP server | `.quickcode/settings.json` → `mcpServers` | Parts ▸ MCP ▸ + Add server |
| composition | `.quickcode/settings.json` → `presets` | Compositions ▸ + New |

Exact paths and formats are the AUTHORING sibling's call; the interface only
requires that each flow can show the user the path it will write before it
writes it.

The kinds that are *not* creatable — provider, policy, hook, panel, storage —
get a sentence on their page rather than a missing button: "Providers come from
Python entry points (`quickcode.providers`); there is nothing to author here.
See docs/ARCHITECTURE.md." A missing affordance with no explanation is the same
failure as a locked setting with no explanation.

### 9.2 The common shape

Every New… flow is one page, never a wizard with steps:

- a **form** on the left with inline validation (a name that already exists says
  so as you type, and offers to open the existing one),
- a **live preview** on the right showing the artefact this will produce — for
  an agent, the composed prompt; for a tool, the JSON schema the model will see;
  for a section, its position in the prompt,
- a footer with the file path it will write, **Create**, and **Edit as file**.

**Edit as file** is the escape hatch for the fluent: it swaps the form for a
mono editor over the actual file text with the same live preview beside it, and
it is reversible while the content still parses. Nobody is forced through a form
they have outgrown, and nobody is handed a JSON textarea as their first
experience.

### 9.3 The shell-tool form

The hardest and most valuable one, because it is how a user extends what the
agent can do without writing Python.

```
┌ New shell tool ──────────────────────────────┬ SCHEMA THE MODEL WILL SEE ─┐
│ Name        [ run_tests            ]         │ {                          │
│             lowercase, digits, _ — this is   │  "name": "run_tests",      │
│             the name the model calls         │  "description": "Runs the  │
│                                              │    project's pytest suite  │
│ Description [ Runs the project's pytest    ] │    and returns the summary │
│             [ suite and returns the summary] │    line plus any failures.│
│             [ line plus any failures.      ] │    Use after changing code│
│             The model chooses tools by this. │    …",                     │
│             Say when to use it, not just     │  "parameters": {           │
│             what it does.                    │    "type": "object",       │
│                                              │    "properties": {         │
│ Parameters                       [+ add]     │      "path": {             │
│ ┌──────┬────────┬─────┬────────────────────┐ │        "type": "string",   │
│ │ name │ type   │ req │ description        │ │        "description":      │
│ ├──────┼────────┼─────┼────────────────────┤ │          "Test file or dir │
│ │ path │ string │  ☐  │ Test file or dir…  │ │           to run…"},       │
│ │ mark │ string │  ☐  │ -k expression…     │ │      "mark": {…}           │
│ └──────┴────────┴─────┴────────────────────┘ │    },                      │
│                                              │    "required": [],         │
│ Command template                             │    "additionalProperties": │
│ ┌──────────────────────────────────────────┐ │       false                │
│ │ uv run pytest -q ${path} ${mark:+-k       │ │  }                        │
│ │ "$mark"}                                 │ │ }                          │
│ └──────────────────────────────────────────┘ │                            │
│ ${path} ${mark} resolve to arguments,        │ ── DRY RUN ─────────────── │
│ shell-quoted. Unknown ${names} are an error. │ path="tests/test_loop.py"  │
│                                              │ mark=""                    │
│ Runs in    [ project root        ▾ ]         │ →                          │
│ Timeout    [ 120000 ] ms                     │ uv run pytest -q \         │
│                                              │   'tests/test_loop.py'     │
│ ☐ This tool is read-only                     │                            │
│   Off is right here. A read-only tool skips  │ [ Run it once to check ]   │
│   the permission prompt and runs in parallel │                            │
│   with other reads — declaring that falsely  │                            │
│   is how two writes interleave.              │                            │
│                                              │                            │
│ Writes .quickcode/tools/run_tests.json       │                            │
│              [ Edit as file ]  [ Create ]    │                            │
└──────────────────────────────────────────────┴────────────────────────────┘
```

Four details that make this work rather than merely exist:

1. **The description field is coached.** A tool's description is how the model
   decides to call it; a user's instinct is to write a label. The helper text
   says "say when to use it, not just what it does" and the preview shows the
   description inside the schema so its role is visible.
2. **The dry run is real.** Filling sample argument values renders the exact
   shell-quoted command, and **Run it once to check** executes it through the
   normal permission path and shows the output. A tool that has never been run
   is a tool nobody trusts.
3. **The read-only checkbox is defended, not just offered.** The consequence
   text is right there, because getting this wrong breaks the parallelism
   invariant in `manifest.py:356` in a way that is very hard to debug later.
4. **The schema preview is the schema.** Rendered by the same serializer the
   backend uses (the create endpoint returns the rendered schema for any draft),
   never a JavaScript approximation.

### 9.4 Empty states

The Tools page with nothing authored:

```
┌ Your tools ──────────────────────────────────────────────────────────────┐
│                                                                          │
│   You have not written a tool yet.                                       │
│                                                                          │
│   A tool is anything the model can call. QuickCode ships 13 of them;      │
│   yours sit alongside and are granted to agents the same way.             │
│                                                                          │
│   The quickest way in is to copy one that already works:                  │
│                                                                          │
│   ┌ fn bash ─────────────────────────────────────────────────────────┐   │
│   │ Runs a shell command and returns stdout/stderr.                  │   │
│   │ Copy it and pin the command down — `uv run pytest -q ${path}`    │   │
│   │ instead of an open shell — and you have a tool the model can     │   │
│   │ only use one way.                        [ Duplicate as a tool ] │   │
│   └──────────────────────────────────────────────────────────────────┘   │
│   ┌ fn grep ─────────────────────────────────────────────────────────┐   │
│   │ Searches file contents for a regular expression.                 │   │
│   │ Copy it to make a search that is already scoped to your          │   │
│   │ codebase's conventions.                  [ Duplicate as a tool ] │   │
│   └──────────────────────────────────────────────────────────────────┘   │
│                                                                          │
│                            [ Start from scratch ]                        │
└──────────────────────────────────────────────────────────────────────────┘
```

The rule for every empty state: **name one real thing that already exists,
explain in one sentence what you would change about it, and offer to duplicate
it.** "Nothing here yet, click + to add" teaches nothing. The Agents empty state
offers `explore` ("copy it and give it write access for a narrow refactor
agent") and `general` ("copy it and cap it at 10 turns for cheap bounded jobs").
The Compositions empty state offers Minimal ("copy it and add `grep`").

---

## 10. Progressive disclosure

Four levels. The rule between them: **each level is one click from the one
above, and no level hides anything a lower level shows.** Depth adds detail; it
never adds truth that was withheld.

**Level 0 — the card.** Sigil, title, `summary`, `affects` chips, tier badge,
and the kind-specific body line from §5.4. No ids, no setting counts, no
`description`. This is the level a first-time user reads, and it should be
readable end to end without a single piece of QuickCode vocabulary.

**Level 1 — the detail page.** The six-question explanation block, the settings
form, and the "used by" section. One click from the card. This is where the ids
appear, and they appear in mono at 10.5px where they belong — a `plugin.id` is a
handle for a person who already knows what they are looking at, not an
introduction.

**Level 2 — the resolved view.** For an agent, the workbench preview pane; for a
prompt section, the composed prompt scrolled to its byte range with the range
highlighted; for a tool, the JSON schema; for an MCP server, the tools it
contributed. This is generated truth: derived from the live objects, never
authored.

**Level 3 — the raw view and the file.** The existing `PluginView`
(`spec.py:111`, rendered by `ui.js:154`) is the deepest level and stays exactly
as it is — format chip, title, copy, on-disk path, syntax-highlighted body. It
is reachable from every level-1 and level-2 surface via **Raw**, and where
`view.path` is set it is accompanied by **Open file**, which opens the actual
file in the user's editor. `PluginView` is already the right primitive; the only
change is that it is now the bottom of a ladder rather than the only rung.

The ladder is uniform: **every** configurable thing in QuickCode has all four
levels, including the locked ones. That is what makes "locked" tolerable — a
locked plugin still has a card, a detail page, a resolved view and a raw view,
and only the form in the middle is replaced by a Fixed-by-design block.

---

## 11. Impact and traceability

"Where does this show up?" gets answered in a **USED BY** block on every detail
page, fed by the provenance data the BINDING design exposes.

**A prompt section → its bytes.** `prompt.tone` says
`characters 1,204–1,588 of the composed prompt (384 chars)` with
**Show it in the prompt →**, which opens the Prompt page scrolled to that offset
with the range highlighted. It also states its absences: `not in explore's
prompt` — subagent prompts are composed from a different template entirely
(`prompts/subagent.py:18`), and a user who does not know that will edit the tone
section and wonder why their subagents ignore it.

**A tool → the agents holding it.** `tool.bash` says
`held by Orchestrator (Standard, Minimal) · general` and
`not held by explore`, each entry a link into that agent's tool picker scrolled
to the `bash` row. Turning a tool off from its own page shows which agents lose
it before the write, not after.

**An MCP server → the tools it contributed.** `24 tools →` opens the Tools page
filtered to that server. In the other direction, every MCP tool's detail page
names its server and offers **Open the server →**. A user who has just added a
server and cannot find its tools currently has no path at all.

**A provider → what runs on it.** `serves 3 agents · 2 distinct models`.

**A setting → where its value came from.** The provenance chain, rendered as a
five-slot strip under any setting whose value is not the declared default:

```
default 30  →  install —  →  project —  →  composition —  →  ●agent 12
```

The winning layer is filled and highlighted, the rest are dimmed with their
value on hover, and each filled slot links to the file that set it. This is the
single most valuable thing the BINDING work exposes and it must appear
everywhere a resolved value is shown — in the workbench, in the plugin detail,
and in the resolved preview. "Why is this 12 when the default is 30" is a
question the current UI cannot answer at all.

---

## 12. Implementation sketch

Ordered. Each step ends with something that runs, per `docs/PLAN-PLUGIN-UI-OVERHAUL.md`
Part D.

**1 — the view shell.** `index.html` gains
`<section id="view-config" class="view">` and a `css/config.css` link.
`main.js` gains `showConfig()` / a third view state and hash routing
(`#/config/...`); `showConfig()` must not call `disconnect()`.
`js/config/view.js` mounts the rail and routes. `modals.js` `openSettings()`
becomes a redirect into the view, and the Quick-settings modal keeps only
endpoint / key / theme. *Nothing else changes; the old pages render inside the
new shell unstyled until step 3.*

**2 — the shared vocabulary.** `js/config/kinds.js` exports the sigil, hue token
and body renderer per kind. `js/config/explain.js` renders the six-question
block from the new spec fields, degrading gracefully while the backend still
omits them (a missing `summary` falls back to `description`). `css/config.css`
adds the `--kind-*` tokens, the stripe/texture rules, and the sigil tile.

**3 — Parts.** `js/config/parts.js` replaces `plugins.js` as the per-kind
browser using `kinds.js` bodies. `js/config/detail.js` is the level-1 page,
reusing `fields.js` unchanged for the form and extending it with the
Fixed-by-design block, the neutral consequence slot and **Restore the default**.
`js/config/machineroom.js` is the same detail page over the four locked plugins
with its own lede. `prompt.js` becomes the Parts ▸ Prompt browser and exports
its `slicer()` for reuse. `settings.css` loses its `.plug-card` / `.plug-row`
rules and keeps the form, sheet and raw rules.

**4 — the workbench, read-only.** `js/config/agent.js` renders the six sections
and `js/config/preview.js` renders the preview pane, both from
`GET /api/agents/{id}/resolved`. Nothing is editable yet. `presets.js` folds in:
`js/config/compositions.js` is the list, and opening one opens `agent.js` in
composition mode.

**5 — the workbench, editable.** Wire Identity / Instructions / Models / Limits
through the existing `updatePlugin` PUT and `confirmRisk`. Add draft state and
`POST /api/agents/{id}/preview` for the live preview. Add the provenance strip.

**6 — the tool picker.** `js/config/toolpicker.js`, opened from the workbench
Tools section as a sheet (`ui.js` `sheet()` is reused as is — it is already the
right primitive and works over a view as well as a modal).

**7 — creation.** `js/config/create/{agent,tool,prompt,mcp,composition}.js`
against the AUTHORING endpoints, plus the shared form/preview/escape-hatch
scaffold in `js/config/create/scaffold.js`. Empty states land with their kind's
page.

**8 — traceability.** The USED BY block on every detail page, cross-links from
the trajectory and chat into `#/config/...`, and the global search box.

### Backend dependencies, named

Fields on `kernel/spec.py`, emitted by `registry.plugin_json`:

- `PluginSpec.summary`, `.affects`, `.audience`, `.consequence`,
  `.locked_because`, `.recourse`, `.docs_anchor`
- `SettingSpec.affects`, `.effect_detail`, `.locked_because`, `.recourse`,
  `.example`
- the `Effect` and `Audience` literals and the `Recourse` dataclass
- `plugin_json` gains `used_by: {agents: [...], compositions: [...]}` — cheaper
  than a separate endpoint and needed on every detail render

Endpoints (the first is the BINDING sibling's; the second is this design's only
new one):

- `GET /api/agents` → every agent identity (orchestrator + definitions) with
  source, tool count, model and ceiling, for the rail
- `GET /api/agents/{id}/resolved` → `{system_prompt, sections[{id,title,start,
  end,tier,source}], tools[{name,schema,read_only,shell,granted_by,source}],
  model{default,allowed,selectable}, limits{max_turns,mode_cap,max_depth,
  max_agents}, delegation{can_spawn[],reason}, provenance{field:[{layer,value,
  path}]}}`
- **`POST /api/agents/{id}/preview`** → the same shape composed from an unsaved
  draft body, so the preview pane is never a JavaScript reconstruction of
  `prompts/subagent.py`
- the AUTHORING endpoints: `POST /api/authored/{kind}`,
  `POST /api/authored/{kind}/{id}/duplicate`,
  `GET|PUT /api/authored/{kind}/{id}/file`, and a schema-render call for the
  shell-tool draft preview

One runtime change:

- `tools/registry.py:select()` gains negative patterns — a pattern starting `!`
  removes matches after the positives are applied. Two lines, and without it the
  tool picker cannot offer "exclude this one from `mcp__*`" without freezing the
  glob into a snapshot.

### What is explicitly not changed

`ui.js` in full — `sheet()`, `confirmRisk()`, `highlightJson()`,
`viewBodyHtml()`, `openPluginView()`, `splitError()`, `flash()` are all correct
and get reused verbatim. The tier semantics and the 403/409 protocol are
correct. `PluginView` is correct. The confirm dialog putting the server's own
words in front of the user instead of "are you sure?" is correct. This design
adds an interface over those; it does not relitigate them.
