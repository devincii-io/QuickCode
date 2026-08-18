# Authoring — how a human creates a plugin

Companion to `../PLAN-PLUGIN-UI-OVERHAUL.md`. That document made the internals
enumerable: 37 plugins, one registry, three tiers. It stopped one step short of
the point. Today every plugin is declared in `kernel/manifest.py`, which means
the only way to add a capability is to edit QuickCode's source. The kernel can
describe what exists; it cannot yet be extended by the person using it.

This document designs the authoring model: what a human can create, in what
file, with what keys, how it is validated, and how it becomes a plugin that is
indistinguishable from an internal one in the registry, the tool list, the
permission gate and the trajectory.

The organising rule: **the tier system protects QuickCode's internals from you;
it does not protect your own files from you.** An authored plugin is yours. It
has no locked settings, nothing is `required`, and you can delete it. What you
cannot do is reach into `prompt.tool_use_policy` and rewrite the contract the
loop depends on — but you *can* stand your own section next to it, and you can
duplicate `agent.explore` into a file you own down to the byte.

---

## 1. What is authorable without Python

Five kinds. Each is one file, and each is data the runtime already knows how to
consume — nothing here invents a new execution path.

| kind | becomes | consumed by |
|---|---|---|
| `tool` | a `Tool` built from a command template | `tools/registry.py` pool |
| `agent` | an `AgentDef` | `subagents/definitions.py` |
| `prompt` | a `PromptSection` | `prompts/sections.py:compose` |
| `mcp` | an entry in the MCP server config | `plugins/mcp.py:connect_servers` |
| `preset` | a `Preset` | `kernel/preset.py:resolve` |

### 1.1 `tool` — a command tool

The single most valuable creatable thing, because it turns "I know the command"
into "the agent knows the command", with a name, a description and typed
parameters the model can actually reason about. A command tool is a **narrowed
`bash`**: fewer degrees of freedom, a description written for this project, and
an argv the permission prompt can show in full before it runs.

The whole design turns on one decision: **the default execution mode is argv,
not a shell string.** The template is a JSON array; each element is one argv
token; the process is spawned with `create_subprocess_exec`, no shell involved.
A parameter value containing `; rm -rf /` or `$(curl evil)` is inert bytes,
because nothing ever parses it. This is not defence in depth against the user —
it is defence against the model, which fills the parameters and is the one
component here we cannot audit.

Substitution rules, exactly:

1. `{param}` is replaced **inside** an argv element. `"--path={path}"` yields
   one element, whatever the value contains. Values are never re-split on
   whitespace, never re-quoted, never re-parsed.
2. An element that is **exactly** `{param}` where the parameter is `list`-typed
   expands to one element per item. This is the only expansion, and it is
   explicit at both ends: the element must be nothing but the placeholder, and
   the parameter must be declared `list`.
3. An element that is exactly `{param}` and whose value is absent or empty is
   **dropped**. An element mixing literal text with an empty placeholder is
   kept with an empty substitution — if you want a flag to disappear, give it
   its own element.
4. A `bool` parameter may only appear as a whole element. True keeps the
   element(s) it names, false drops them.
5. `{{` and `}}` are literal braces. An unknown `{name}` is a validation error,
   not a silent empty string.

Shell mode exists, because `npm test 2>&1 | tail -40` is a real thing people
want. It is opt-in with `shell: true`, it is `confirm`-tier to enable, and it
comes with the rule that makes it safe: **in shell mode `{param}` substitution
is forbidden.** Parameters arrive as environment variables `QC_PARAM_<NAME>`
and the template references `"$QC_PARAM_PATH"`. Values still never touch the
shell parser. A `{...}` in a shell-mode template is the validation error
`param_substitution_in_shell`.

Typed parameters: `string`, `text`, `int`, `float`, `bool`, `enum` (+`choices`),
`path`, `list` (+`item_type`). Constraints — `pattern`, `minimum`, `maximum`,
`max_length` — exist so a nonsense value fails early with a message the model
can act on. They are **not** the safety mechanism; argv exec is. Say this in the
help text, or people will write regexes they think are a sandbox.

`path` parameters get one behaviour the others do not: the value is resolved
against the session cwd and refused if it escapes the project root, using the
same rule as `core/permissions.py:_protected`. That refusal is in the tool, not
the gate, and it is not configurable — a command tool must not become the way
to read `~/.ssh`.

Output mapping is declared, not guessed: `text` (combined stdout/stderr, the
default), `json` (stdout parsed; malformed JSON is an error result naming the
parse failure), `lines` (stdout, blanks stripped, capped), `file` (stdout
written to a temp file, the result is the path plus a head). Plus
`success_exit_codes`, `on_nonzero: error|content`, `timeout_ms`,
`max_output_chars`. Truncation uses `tools/base.py:truncate`, so a command tool
truncates with the same marker every other tool does.

### 1.2 `agent` — a subagent definition

Already authorable today (`.quickcode/agents/*.md`) and already loaded by
`load_defs`. What it lacks is a schema anyone can see, validation, and a place
in the registry that says "yours". This design keeps the existing file shape,
adds `kind: agent`, and moves the canonical location into the plugins directory
while continuing to read the old one.

### 1.3 `prompt` — a prompt section

Pure text plus ordering plus when it applies. Authored sections are always
`free` tier and always additive: they take a slot in the composition next to
the internal ones. `applies_to` decides audience (`main`, `subagents`,
`agent:<name>`) and `when` decides condition (`always`, `plan`, `orchestration`,
`headless`). Ties on `order` break by id, deterministically — the composed
prompt is a cache breakpoint and must be byte-identical for identical inputs.

### 1.4 `mcp` — an external MCP server

Already config-driven. Formalising it as a file buys three things the
`mcpServers` blob cannot: a title and description separate from the command, a
per-server enable toggle that lives with the definition, and a validator that
warns when a literal secret is sitting in a file about to be committed.
`mcpServers` in `settings.json` keeps working — pasting a Claude config is the
whole reason that shape exists — and gets an **Adopt** action that rewrites it
as an authored file.

### 1.5 `preset` — a composition

Which tools, which agents, which prompt overrides, which default mode. Already
data (`kernel/preset.py`), already duplicable by design (`base:`). Moving it to
a file makes it reviewable in a diff, which matters more for a preset than for
anything else here, because a preset is what silently decides an agent's reach.

### 1.6 What needs Python, and why

| kind | why not authorable |
|---|---|
| `provider` | a wire protocol adapter — streaming, tool-call framing, usage accounting. There is no data shape for this that is not just Python with extra steps. |
| `hook` | `before_request` / `before_tool` / `after_tool` run inside the loop and can rewrite what the model sees. A declarative hook language would be a programming language. |
| `policy` | the permission engine's decisions are `deny → ask → allow`; a policy plugin decides *how* to decide. Rules — the data part — are already authorable in `settings.json`. |
| `storage`, `panel` | the session log format is locked by contract; a panel is frontend code. |
| a tool with real logic | if it needs to hold state, retry, parse incrementally or talk to a library, it is a `Tool` subclass. Pretending otherwise produces a template language nobody can debug. |

The Python path is the existing entry-point mechanism, extended.
`quickcode.tools` and `quickcode.providers` stay exactly as they are — they are
the simplest thing that works for the two most common cases. A third group
covers everything else:

```toml
[project.entry-points."quickcode.plugins"]
myext = "mypkg.quickcode_ext:plugins"
```

The callable takes no arguments and returns `PluginSpec | list[PluginSpec]`.
Contract:

- **Id namespace.** Every returned spec's id must be `<kind_prefix>.<epname>__<slug>`
  — `tool.myext__deploy`, `prompt.myext__house_style`. The kind prefix is kept
  because `kernel/state.py:prompt_overrides` scans ids by prefix and building a
  full registry on every session open to answer that question would be silly.
  The `<epname>__` segment is the namespace. A spec that violates the pattern is
  logged and dropped; it is not renamed, because a plugin whose id changed under
  it would lose its saved settings on the next release.
- **Source is stamped, never declared.** The loader sets `source="entrypoint"`
  after loading, exactly as `plugins/loader.py:29` already does for tools.
  Provenance is the one thing a plugin cannot be trusted to say about itself.
- **Runtime object via a factory.** `PluginSpec` gains
  `factory: Callable[[], Any] | None`. The subsystem that consumes the kind
  calls it — the loop calls a `hook` spec's factory, the tool registry calls a
  `tool` spec's factory. `factory` is never called during discovery, for the
  same reason `view` is lazy: building the registry must not import, spawn or
  read anything.
- **Failure is per-entry-point.** One raising entry point is logged and skipped;
  the others load. This is already the rule in `loader.py` and it stays.

A third-party `PluginSpec` then joins `PluginRegistry` through the same
`register_all` call every internal one uses, appears in the same list, obeys the
same tier enforcement, and is shown with source `Installed` in the UI.

---

## 2. File format and location

### 2.1 One file per plugin, markdown with frontmatter

Rejected: a `plugins` object in `settings.json`. That file is already four
things (permissions, mcpServers, presets, plugin state); adding authored
definitions makes it five, makes every edit a read-modify-write of a shared
document, makes a diff unreadable, and makes a syntax error take out the other
four concerns at once. The kernel already learned this lesson once — that is why
`state.py` merges per field rather than per plugin.

Rejected: JSON files per plugin. Every authorable kind has a large free-text
payload at its heart — a tool's description, an agent's system prompt, a prompt
section's body. JSON's answer to a 40-line prose block is `"\n"` escapes, and
the moment the primary content is unreadable in the file, people stop editing
the file.

Chosen: **markdown with `---` frontmatter, one file per plugin.** The user
already writes this shape for agents. The body is the natural home for the free
text. It diffs, it greps, it lives in git, and an external editor opens it
without ceremony. Structured payloads that genuinely are data — a tool's
parameter table, its argv, an MCP env map — live in **fenced blocks tagged in
the info string**, inside the body:

````markdown
```json params
[{"name": "path", "type": "string", "default": ""}]
```

```json argv
["uv", "run", "pytest", "--last-failed", "-q", "{path}"]
```
````

This keeps the parser small: scalars and inline lists in the frontmatter, JSON
where JSON is honest, prose where prose is honest, and **no YAML dependency**.
The existing `_split_frontmatter` (`subagents/definitions.py:158`) is a 15-line
`key: value` reader and it has been enough; it gets generalised, not replaced by
a parser that can express things this format has no use for.

### 2.2 Where the files live

```
~/.quickcode/plugins/*.md          user scope   — every project
<cwd>/.quickcode/plugins/*.md      project scope — this repo, committed
<cwd>/.quickcode/plugins/.trash/   deleted files, not scanned
```

Flat, not `plugins/tool/`, `plugins/agent/`. The kind is in the frontmatter; a
directory that also encodes it is a second truth that can disagree with the
first, and then there has to be a rule about which wins that nobody will
remember. Flat also means renaming a kind is an edit, not a move.

`.quickcode/agents/*.md` keeps loading, treated as `kind: agent`. It is the
shape documented in `docs/AGENTS.md` and there is no reason to break it.

### 2.3 Layering

Discovery order, later shadowing earlier **by plugin id**, not by filename:

1. `~/.quickcode/settings.json` (`mcpServers`, `presets` — legacy blobs)
2. `~/.quickcode/plugins/*.md` + `~/.quickcode/agents/*.md`
3. `<cwd>/.quickcode/settings.json` (legacy blobs)
4. `<cwd>/.quickcode/plugins/*.md` + `<cwd>/.quickcode/agents/*.md`

Two rules fall out. A file beats a blob at the same scope, because the file is
the more explicit artifact and the one the UI can edit. A project beats the
user, matching `state.py`, `preset.py`, `mcp.py` and `definitions.py` — every
existing layering in the codebase.

Shadowing is resolved **before** registration. `PluginRegistry.register` keeps
the first spec for an id and warns (`registry.py:45`); discovery must therefore
hand it one winner per id, not two and a hope.

Enable/disable state stays where it already is: `plugins.<id>.enabled` in
`settings.json`, via `kernel/state.py`. The frontmatter is the *definition*;
whether it is switched on is *state*. Mixing them means toggling a plugin dirties
a file you may have committed. `enabled_by_default` in frontmatter is authoring
intent and is read only when there is no saved state.

### 2.4 Frontmatter keys per kind

Common to all kinds:

| key | type | notes |
|---|---|---|
| `kind` | enum | `tool` \| `agent` \| `prompt` \| `mcp` \| `preset`. Required. |
| `name` | slug | `^[a-z][a-z0-9_-]{0,31}$`. Defaults to the filename stem. |
| `title` | string | Display name. Defaults to `name`. |
| `description` | string | Required for `tool` and `agent` — the model reads it. |
| `group` | string | Card grouping in Settings. Defaults per kind. |
| `enabled_by_default` | bool | Default `true`. |
| `derived_from` | plugin id | Breadcrumb written by Duplicate. Inert. |

**`kind: tool`**

| key | type | notes |
|---|---|---|
| `label` | string | Transcript row template, e.g. `pytest --lf {path}`. Substituted for display only. |
| `shell` | bool | Default `false`. `true` switches to shell-string mode + `QC_PARAM_*`. |
| `cwd` | string | `project` (default), `file_dir`, or a path relative to the project root. |
| `timeout_ms` | int | Default 120000, max 600000 — the same envelope as `bash`. |
| `output` | enum | `text` (default) \| `json` \| `lines` \| `file`. |
| `max_output_chars` | int | Default 30000. |
| `success_exit_codes` | list | Default `[0]`. |
| `on_nonzero` | enum | `error` (default) \| `content`. `content` is for tools where a non-zero exit *is* the answer (a linter, a test run). |
| `read_only` | bool | Default `false`. Your assertion, `confirm`-tier to set: it removes the permission prompt and allows parallel execution. |
| `permission_target` | string | Name of the parameter a rule like `pytest-failed(tests/**)` matches on. |
| `env_from` | list | Ambient env var names passed through. |

Body blocks: ` ```json params ` (required, may be `[]`), ` ```json argv `
(required unless `shell`), ` ```sh command ` (shell mode only),
` ```text stdin ` (optional), ` ```json env ` (optional, literal values).
Prose outside the blocks is the tool's long description, appended to
`description` in the schema the model sees.

**`kind: agent`** — the keys `docs/AGENTS.md` already documents (`tools`,
`model`, `models`, `model_selectable`, `mode_cap`, `max_turns`, `color`,
`skip_project_instructions`), plus `kind`. Body is the system prompt.

**`kind: prompt`**

| key | type | notes |
|---|---|---|
| `order` | int | Position in the composition. Internal sections sit at 10..130. |
| `after` | plugin id | Alternative to `order`: resolved to `that.order + 1`. |
| `applies_to` | list | `main`, `subagents`, `agent:<name>`. Default `[main]`. |
| `when` | enum | `always` (default) \| `plan` \| `orchestration` \| `headless`. |

Body is the section text, verbatim, stripped. Wrapping it in an XML-ish tag the
way the internal sections do is recommended and not enforced.

**`kind: mcp`** — `command` (string, required), `args` (list), `env_from`
(list). Body block ` ```json env ` for literal values, warned about in project
scope.

**`kind: preset`** — `base`, `tools` (list of names/aliases/globs), `agents`
(list), `default_mode`. Body block ` ```json settings ` for
`{plugin_id: {key: value}}` overrides; prose body is the description.

---

## 3. Identity and namespacing

An authored plugin's id is `<kind_prefix>.<name>`: `tool.pytest-failed`,
`agent.reviewer`, `prompt.house_style`, `mcp.docs`, `preset.review-only`. Kind
prefixes are `tool.`, `agent.`, `prompt.`, `mcp.`, `preset.` — the same prefixes
the manifest already uses, because `prompt_overrides` and the frontend both key
off them.

A `tool`'s `name` is also its **wire name**, the string the model calls. It must
match `^[a-zA-Z0-9_-]{1,64}$` (the OpenAI function-name constraint) and must not
be one of the live tool names or start with `mcp__`.

Reserved, and refused:

- the exact id of any internal plugin (`tool.bash`, `prompt.tone`,
  `agent.explore`, `runtime.*`, `hook.*`, `provider.*`);
- the wire names `read`, `write`, `edit`, `glob`, `grep`, `bash`, `plan`,
  `agent`, `send_message`, `task_*`;
- the prefixes `mcp__`, `runtime.`, `hook.`, `provider.`;
- an id already claimed by an entry-point plugin.

**Collision with an internal plugin is refused, not shadowed.** Three reasons.
`.quickcode/` is committed, so shadowing means cloning a repository can silently
replace the shell tool with something that looks like the shell tool — a supply
chain hole with a friendly UI. Prompt sections already have a sanctioned
override path (the `body` setting, `manifest.py:251`), so shadowing would be a
second mechanism for one job. And the failure is invisible: nothing in the
trajectory would say "this `read` is not the `read` you think it is". The
refusal is the error code `id_reserved`, and the problems surface offers the
one-click fix, which is Duplicate.

Collision between two *authored* plugins at different scopes is the layering
rule and is fine: project shadows user, and the UI says so on the card
("shadows your user-level `reviewer`"). Collision at the *same* scope is
impossible by construction — the id derives from `name`, which defaults to the
filename stem, and two files in one directory cannot share a stem unless `name`
was set explicitly. That case is the error `id_duplicate` and both files are
skipped, because guessing which one was meant is worse than loading neither.

---

## 4. Duplicate-to-customise

This is how "fully customisable" and "the locked tier is real" coexist. You
cannot edit `agent.explore`. You can press Duplicate and get a file you own, in
which every line of that agent — including the parts that were locked — is
plain editable text.

**What Duplicate does.** It materialises the internal plugin's live definition
into an authored file at the chosen scope, with `derived_from: <original id>`
in the frontmatter. Nothing else links the two. No inheritance, no delta, no
"upstream changed, do you want to merge". `derived_from` is a breadcrumb for
the UI ("copied from `agent.explore`") and for you six months later; the
runtime ignores it. Live inheritance would recreate exactly the coupling that
makes the locked tier necessary in the first place, and it would mean an
upgrade to QuickCode could change the behaviour of a file you thought you owned.

**Naming.** `<original-name>-copy`, then `-copy-2`, and so on until free. Title
becomes `<original title> (copy)`. Both are editable immediately; the point of
the flow is that it lands you in the editor, not that it picks a clever name.

**Tier and `required` interaction.** Duplicating *reads*, and reading is never
restricted — "locked means you cannot edit this, never that you cannot see it"
is already the kernel's stated principle (`spec.py:17`), and every plugin
already exposes a `PluginView`. So Duplicate is available at every tier,
including `locked`, and including `required=True`. The copy is born with
`source="authored"`, `required=False`, `tier="free"` on every setting. The
original is untouched and stays enabled.

**What is duplicable, per kind:**

| kind | duplicable | what a copy contains |
|---|---|---|
| internal `agent` (`explore`, `general`) | **yes** — the flagship case | full frontmatter from `AgentDef`, `prompt_body` as the markdown body |
| `preset` (built-in) | **yes** | resolved tools/agents/mode; `base` cleared, since a copy that still inherits is not a copy |
| `mcp` (from a blob) | **yes** (this is Adopt) | command, args, env keys; literal secrets replaced by `env_from` entries |
| authored anything | **yes** | byte copy with a new `name`/id |
| `prompt` section, tier `free`/`confirm` | yes, but wrong | you want **Edit body**, which the existing `body` setting already gives you. The UI offers Edit first and Duplicate second. |
| `prompt` section, tier `locked` (`prompt.tool_use_policy`, `prompt.environment`) | **yes, as a sibling** | a new section with `after: <original>`. The original still renders. You are adding a voice, not replacing one. |
| internal `tool` (`read`, `bash`, `task_*`) | **no** | a Python tool's behaviour is not expressible as an argv template. A "copy" would be a file that claims to be `read` and is not. Duplicate is disabled with that sentence as the reason, and the card offers **New command tool** instead. |
| `provider` | **no** | needs Python (§1.6). The card links to the entry-point contract. |
| `policy`, `hook`, `storage`, `panel` | **no** | nothing consumes a second one. A duplicate would be inert, and an inert plugin sitting in the list looking enabled is worse than no button. |

Duplicating `tool.bash` deserves its own note, because it is the one people will
try: a command tool *is* a narrowed `bash`, so the honest answer is New command
tool with `shell: true`, pre-filled with the shell selection logic `bash` uses.
The button says that.

---

## 5. Validation and failure

### 5.1 When it runs

Twice, with the same code and different authority.

**On load — authoritative.** Every registry build (`bootstrap.build_registry`)
parses and validates every authored file. A file with an error-severity problem
is **skipped**: it produces no spec, no tool, no section. It never raises. This
is exactly the rule `preset.py:133` already follows and the reason
`bootstrap._safe` exists — one malformed file must not take down Settings, must
not stop the app starting, and must not hide the other plugins.

**On save — advisory.** `PUT .../source` runs the same validator, writes the
file regardless, and returns the problems in the response. Refusing to save
half-finished work would be hostile in an editor, and pointless besides: the
file can be edited in vim, and the filesystem is the source of truth. What the
save endpoint guarantees is that you are told, immediately, in the editor,
before you go looking for a tool that never loaded.

### 5.2 The problem shape

```json
{
  "plugin_id": "tool.deploy-preview",
  "kind": "tool",
  "scope": "project",
  "path": "C:/proj/.quickcode/plugins/deploy-preview.md",
  "line": 24,
  "field": "argv",
  "code": "unknown_placeholder",
  "severity": "error",
  "message": "argv references {taget}, which is not a declared parameter. Declared: target, env.",
  "fix": "Rename it to {target}, or add a parameter named taget."
}
```

`code` is stable and machine-readable; `message` names what is wrong in the
user's own vocabulary; `fix` is the sentence that turns a rejection into a next
action, and is what the current app is missing everywhere. `line` is best-effort
— frontmatter keys and fenced blocks both have known positions.

Severities: `error` skips the plugin; `warning` loads it and shows a badge (a
literal secret in a project-scope MCP file, a `read_only: true` on a tool whose
argv contains `git push`, a preset naming a tool that does not exist in this
install — that last one is deliberately a warning, matching `select()`'s rule
that an allowlist mentioning an absent tool yields a smaller agent, not a crash).

A representative error vocabulary: `missing_key`, `bad_kind`, `bad_slug`,
`id_reserved`, `id_duplicate`, `missing_block`, `bad_json`, `unknown_param_type`,
`unknown_placeholder`, `placeholder_split_risk`, `param_substitution_in_shell`,
`list_placeholder_not_alone`, `bad_enum_choice`, `timeout_out_of_range`,
`path_escapes_project`, `needs_trust`, `secret_in_project_file`,
`unknown_agent_ref`, `order_conflict`.

### 5.3 Where the user sees it

- `GET /api/kernel` grows a `problems` array; `GET /api/kernel/problems` serves
  it alone for polling.
- Settings → Plugins shows a **Problems (n)** card pinned above the list, red,
  one row per problem: title, message, fix, and an **Open file** button that
  uses the `path` (the same affordance `PluginView.path` was added for).
- The Plugins tab label carries the count badge, so a broken plugin is visible
  from anywhere in Settings rather than only once you go looking.
- Opening a project whose *project-scope* plugins have errors emits one toast.
  User-scope problems do not toast on every project open — you would see the
  same toast forever.
- `quickcode doctor` (`doctor.py` already exists) gains a plugins section, which
  is the headless answer and the one that works over SSH.

A skipped plugin is listed in the problems card but **not** in the plugin list.
A half-loaded plugin sitting in the list with a warning triangle invites the
question "is it running?", and the answer must be unambiguous: it is not.

### 5.4 Trust — the security decision

`.quickcode/plugins/` is committed. Cloning a repository and opening it in
QuickCode would otherwise hand that repository the ability to define tools the
agent may run, prompt sections that instruct the agent, and MCP servers that
spawn processes. That is remote code execution with a nice card UI.

**Project-scope authored plugins are inert until the project is trusted.**
One decision per project, recorded in `~/.quickcode/trust.json` keyed by
resolved project path, storing a hash over the sorted contents of the project's
plugin files. Until then every project-authored plugin loads as disabled with
the problem `needs_trust`, and the Problems card leads with a single prompt
naming what is in there ("3 tools, 1 MCP server, 2 prompt sections — review
them"). The hash changing — a `git pull` that edits a tool — re-prompts, naming
the files that changed.

User-scope plugins are trusted implicitly. You wrote them; there is nobody else
to defend against, and prompting for your own files trains the reflex that makes
the project prompt worthless.

---

## 6. Lifecycle

**Create.** From a **template file with comments in it**, not an empty form.
Templates ship at `quickcode/kernel/authoring/templates/<kind>.md` and are what
the New flow writes to disk before opening the editor. A commented example
teaches the format in one read; a form with eleven inputs and no context is
where the current app loses people.

**Edit.** Two surfaces over one file: a generated form for the frontmatter
(driven by the same `SettingSpec` machinery the plugin cards already use) and a
raw source editor for the whole document. The raw editor is not a power-user
escape hatch — it is the primary one, because the body is the interesting part
of every kind here.

**Disable without deleting.** The existing toggle, writing
`plugins.<id>.enabled` to `settings.json` (`state.save_entry`). Works for
authored ids today with no changes, because nothing in `state.py` cares whether
an id is internal.

**Delete.** Move to `.quickcode/plugins/.trash/<name>-<unix_ts>.md`. The trash
directory is not scanned. Undo is a file move; there is no need for anything
cleverer, and there is a strong need to not silently destroy a prompt someone
spent an hour on.

**Export.** The file is the export. `GET .../export` returns its bytes with a
content disposition. There is no bundle format — a set of plugins is a directory
and people already know how to zip a directory.

**Import.** `POST .../import` takes file content plus a target scope, validates,
refuses reserved ids, and on an authored-id collision returns a `409` with the
suggested rename rather than overwriting. Import from a URL is deliberately not
offered: an authored tool is arbitrary command execution, and pasting content
you have read is a different act from fetching content you have not.

### 6.1 What takes effect when

`Preset` is frozen to a session at start, on purpose (`manager.py:557`) — the
conversation was told what tools it has, and changing that underneath it makes
the transcript a lie and invalidates the prompt cache breakpoint. That
constraint propagates to exactly the things the model was already told about.

| kind | takes effect | why |
|---|---|---|
| `tool` (new or edited) | **next session** | the tool list and its schemas were declared in the first request. The resolved definitions are snapshotted at session open. |
| `prompt` (new or edited) | **next session** | the system message is the cache breakpoint and must be byte-stable within a session (`prompts/sections.py:9`). |
| `agent` (edited) | **next spawn** | definitions are read at spawn time and a subagent gets a fresh context. Editing `reviewer`'s prompt, model or `max_turns` affects the next `agent` call. |
| `agent` (new) | **next session** | the `agent` tool's `agent_type` description enumerates the available agents; a new one is not in the schema the running parent was given. |
| `mcp` | **project reopen** | servers are spawned once per project (`projects.py:_create`). A `POST /api/projects/{pid}/mcp/reload` stops and respawns them; running sessions keep their frozen tool lists regardless. |
| `preset` | **next session** | by definition. |
| enable/disable | same rule as the kind it applies to | the toggle writes state; state is read where the definition is read. |

Drift on resume: session meta records a content hash per authored plugin the
session used. On resume, a differing hash writes a `plugin_drift` meta record
and shows a badge on the session ("`pytest-failed` changed since this session
started"). It does not block, does not rewrite, and does not re-freeze — the old
conversation keeps the tools it was told about, and the badge explains why the
tool behaves like the older definition.

---

## 7. Worked examples

### 7.1 A command tool

`<project>/.quickcode/plugins/pytest-failed.md`

````markdown
---
kind: tool
name: pytest-failed
title: Re-run failed tests
description: Re-runs only the tests that failed in the last pytest run, quietly.
group: Testing
label: pytest --last-failed {path}
cwd: project
timeout_ms: 300000
output: text
max_output_chars: 30000
success_exit_codes: [0, 5]
on_nonzero: content
read_only: false
permission_target: path
---

Re-runs the tests that failed on the previous pytest invocation, using pytest's
`--last-failed` cache. Exit code 5 means "no failed tests cached", which is a
normal answer and not an error. Pass `path` to narrow the run to one file or
directory; leave it empty to re-run every cached failure.

```json params
[
  {"name": "path", "type": "path", "required": false, "default": "",
   "description": "Optional test file or directory to restrict the run to."},
  {"name": "maxfail", "type": "int", "required": false, "default": 0,
   "minimum": 0, "maximum": 50,
   "description": "Stop after this many failures. 0 means no limit."}
]
```

```json argv
["uv", "run", "pytest", "--last-failed", "-q", "{path}", "--maxfail={maxfail}"]
```
````

`path` empty drops that element (rule 3). `maxfail` is `0` by default, which
substitutes into `--maxfail=0`; if that is not what pytest wants, split it into
its own element and let rule 3 do the work — this is exactly the sort of thing
the template's comments should say out loud.

### 7.2 A custom agent

`<project>/.quickcode/plugins/reviewer.md`

```markdown
---
kind: agent
name: reviewer
title: Reviewer
description: Reviews a diff or a named set of files for correctness bugs and
  convention drift. Read-only. Spawn one per area; do not give it the whole repo.
group: Agents
tools: [read, glob, grep, bash]
model: worker
models: [worker, orchestrator]
model_selectable: true
mode_cap: ask
max_turns: 20
color: magenta
derived_from: agent.explore
---

You are a code reviewer. You read, you do not write. Your entire output is the
final message; nobody sees your intermediate steps.

Review only what the delegation names. For each finding, give:

- the file and line as `path:line`,
- what is wrong, in one sentence,
- the smallest change that fixes it.

Rank findings: correctness bugs first, then things that will break under a
condition the code does not handle, then convention drift. Do not report style
opinions the project's own files contradict — read a neighbouring file before
you claim something is unconventional.

If you find nothing, say so in one line. A review that invents findings to look
thorough is worse than a short one.
```

Note `tools: [read, glob, grep, bash]`: the allowlist resolves against the
**live** pool at spawn time (`tools/registry.py:78`), so once `pytest-failed`
exists, adding it to this list grants it — no code knows or cares that it is
authored.

### 7.3 A prompt section

`~/.quickcode/plugins/house-style.md`

```markdown
---
kind: prompt
name: house_style
title: House style
description: Project-independent conventions I want in every session.
group: Prompt
after: prompt.conventions
applies_to: [main, subagents]
when: always
enabled_by_default: true
---

<house_style>
- Python first. Modular: no single-file business logic, no 400-line function
  that "will be split later".
- Comments only where the code cannot say it. Never write a comment addressed
  to a reviewer.
- Introduce a dependency only when it earns its place; say which problem it
  solves when you add one.
- Business-facing text is German. Code, commits and developer docs are English.
- Decisions about money, legal exposure, vendors or publishing are mine. Flag
  them; do not make them.
</house_style>
```

`after: prompt.conventions` resolves to order 41, which puts it directly after
the internal conventions block and before task management. `applies_to`
including `subagents` means a spawned `reviewer` gets it too — which is the
answer to "how do I attach this to my agents".

---

## 8. Implementation sketch

In order. Each step ends with something that runs.

1. **`quickcode/kernel/authoring/format.py`** — `parse_document(text) -> Document`
   with `meta: dict[str, str]`, `body: str`, `blocks: dict[str, Block]` where a
   block is `(tag, lang, text, line)`. Generalises
   `subagents/definitions.py:_split_frontmatter`; that function becomes a thin
   caller so the agent loader and the plugin loader cannot drift apart.
2. **`authoring/schema.py`** — the per-kind key tables from §2.4, plus
   `validate(doc, *, scope, reserved) -> list[Problem]` and the `Problem`
   dataclass. Pure; no filesystem, no imports of the runtime. This is where the
   tests go.
3. **`authoring/model.py`** — `AuthoredPlugin` and the converters:
   `to_agent_def()`, `to_prompt_section()`, `to_preset()`, `to_mcp_config()`,
   `to_tool()`, `to_spec()`.
4. **`tools/command.py`** — `CommandTool(Tool)`. Builds a pydantic `Input`
   model with `pydantic.create_model` from the parameter table (so
   `Tool.schema()` needs no changes and the strict-schema contract holds
   unaltered), implements the §1.1 substitution rules, execs via
   `asyncio.create_subprocess_exec` (argv mode) or the existing `_build_argv`
   shell selection (`tools/bash.py:210`) in shell mode, maps output per
   `output:`, truncates with `tools/base.py:truncate`, and sets
   `permission = PermissionSpec(mutates=not read_only, target_field=<permission_target>,
   path_target=<that param is a path>)`. It puts the fully resolved argv in
   `ToolResult.ui_meta` and in `render_call`, so the approval modal and the
   trajectory show the exact command.
5. **`authoring/discovery.py`** — scan the four layers, resolve shadowing by id,
   apply the trust gate, return `(plugins, problems)`. Never raises.
6. **`authoring/store.py`** — slug allocation, create-from-template, save,
   delete-to-trash, duplicate (per the §4 table), export, import.
7. **`authoring/trust.py`** — `~/.quickcode/trust.json`, hash over the project's
   plugin files, `is_trusted` / `trust` / `revoke`.
8. **`kernel/spec.py`** — add `"authored"` to `Source`; add
   `factory: Callable[[], Any] | None = None` and `path: str = ""` to
   `PluginSpec`. Both default to the current behaviour, so nothing existing
   changes.
9. **`kernel/manifest.py`** — `authored_specs(plugins) -> list[PluginSpec]`,
   one branch per kind, each producing a spec whose settings are all `free` and
   whose `view` renders the file's source with its path. `agent_specs` stops
   inferring `builtin` from a name tuple and reads the flag it is handed.
10. **`kernel/registry.py`** — carry `problems: list[Problem]`; have `register`
    record an `id_duplicate` problem instead of only logging; expose them in
    `to_json`.
11. **`kernel/bootstrap.py`** — run discovery inside `_safe`, register authored
    specs after the internal ones (so a reserved-id collision loses, per §3),
    attach problems to the registry.
12. **`prompts/sections.py`** — `ordered(extra=None)` merges authored sections
    by `(order, id)`; `PromptContext` gains `audience: Literal["main","subagent"]`
    and `agent_name: str` so `applies_to` can filter. `compose()` is untouched —
    the join and the empty-section drop are the byte-stability guarantee and
    must not be reopened.
13. **`plugins/loader.py`** — `load_plugin_specs()` for the `quickcode.plugins`
    entry-point group, with the id-prefix enforcement and the `source` stamp
    from §1.6.
14. **`server/projects.py`** — build authored command tools alongside
    `plugin_tools` and `mcp_tools` into `extra`, gated on trust; merge authored
    MCP configs into `load_server_configs`.
15. **Routes** (names only; `app.py` is not touched by this document, and every
    one of these needs the `/api/projects/{pid}/...` twin the file already
    pairs for everything else):
    - `GET /api/kernel/problems`
    - `GET /api/kernel/authored` · `POST /api/kernel/authored`
    - `GET|PUT /api/kernel/authored/{id}/source`
    - `DELETE /api/kernel/authored/{id}`
    - `POST /api/kernel/authored/{id}/duplicate`
    - `POST /api/kernel/plugins/{id}/duplicate`
    - `POST /api/kernel/authored/validate`
    - `GET /api/kernel/authored/{id}/export` · `POST /api/kernel/authored/import`
    - `GET /api/kernel/plugins/{id}/usage`
    - `GET|POST /api/projects/{pid}/trust`
    - `POST /api/projects/{pid}/mcp/reload`

### 8.1 Invariants this touches

- **Byte-stable prompt.** Authored sections must be resolved once at session
  open and carried with the session. `GET /api/prompt` currently renders live
  and would, after this change, show a prompt that differs from a running
  session's. It needs an optional `conv_id` that renders from the session's
  frozen snapshot; without that the Prompt tab quietly becomes fiction.
- **Frozen preset.** A new authored tool must not appear in a running session's
  registry. `select_tools` runs once at open (`manager.py:571`) and must keep
  running once — the temptation to "just re-resolve on the next turn" is exactly
  what the freeze exists to prevent.
- **First registration wins.** `PluginRegistry.register` keeps the first spec
  for an id. Discovery must therefore hand the registry a resolved winner per
  id, and bootstrap must register internal specs first, so a reserved-id
  collision is refused rather than accidentally honoured.
- **Permission specs for authored tools.** `permissions.registry_specs()` is a
  process-level cache built from `default_registry()` and does not contain
  authored tools. The session path passes `ToolRegistry.permission_specs()`
  explicitly into the engine; that path must be verified to be the only one a
  command tool can reach, or an authored tool falls back to `DEFAULT_SPEC`.
  `DEFAULT_SPEC` is the cautious one (mutating, prompted), so the failure mode
  is annoying rather than dangerous — but a `read_only: true` tool that keeps
  prompting will be read as a bug.
- **Wire-name stability.** Renaming an authored tool mid-session produces a tool
  the running conversation has never heard of. The rename endpoint says so and
  offers to open a new session.
- **`.trash/` must not be scanned**, and `glob("*.md")` on the plugins directory
  will happily walk into it if the scan is ever made recursive. It must not be.

---

## 9. The surface this is for

The complaint behind this document was not only "I cannot create a plugin". It
was "there is no distinction between anything and no explanation". Three things
in the UI follow directly from the model above and should land with it.

**Three filters that match how people actually think.** Source: *Built in* /
*Yours* / *Installed*. Kind. Scope: *User* / *This project*. "Yours" is the
whole point — the 37 internal plugins and your four files should never look
alike in a list.

**Used by.** `GET /api/kernel/plugins/{id}/usage` returns the referrers: which
presets grant this tool, which agents list it, which sessions are currently
running on it. A plugin card that cannot tell you who uses it is a plugin card
you are afraid to disable.

**A context view per agent.** An agent's card resolves, live, against the
current pool: these are the tools it would actually get, this is its model
policy, this is its permission ceiling, these presets grant it, this is its
prompt with the authored sections that `applies_to` it folded in. That view is
the difference between "here is a config object" and "here is the agent you are
about to talk to".
