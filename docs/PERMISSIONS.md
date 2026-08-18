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
| `auto-edit` | ✅ | ✅ auto (project root only) | prompt (plus a small allowlist of file-op commands: `mkdir`, `touch`, `mv`, `cp`, `rm` inside project) | trusted editing flow |
| `dontask` | ✅ | rule-matched only, else **auto-deny** | rule-matched only, else auto-deny | headless `-p` default: never blocks on a prompt |
| `yolo` | ✅ | ✅ | ✅ | bypass; explicit opt-in |

- **Switching:** `Shift+Tab` cycles `plan → ask → auto-edit` (+ `yolo` only if the session started with `--yolo` or settings enable it — same containment idea as Claude Code's bypassPermissions). `dontask` never appears in the cycle; it's a startup flag / headless default. Mode is per-conversation; status-bar segment is color-coded (plan blue, ask neutral, auto-edit yellow, yolo red).
- **Yolo guardrails:** confirmation screen on entry (persisted acceptance), red status bar, and a hard circuit breaker: catastrophic patterns (`rm -rf /`, `rm -rf ~`, and the same via `$()`/backtick substitution, `git push --force` to default branch, recursive deletes outside the project) prompt **even in yolo**.
- **Protected paths always prompt** regardless of mode or allow rules: `.git/`, `.quickcode/`, `~/.quickcode/`, and anything outside the project root. Checked *before* allow-rule evaluation so no rule can accidentally unprotect them. The prompt for `.quickcode/` writes offers "allow self-config edits for this session".
- **Subagent capping:** a child agent's mode is `min(parent mode, its spawn-time cap)` — a yolo orchestrator does not imply yolo workers. Detail in docs/AGENTS.md.

## Rules

Stored as `allow` / `ask` / `deny` arrays. Sources merge; evaluation order is fixed: **deny → ask → allow → mode default**. First match wins — a broad deny beats a narrow allow by design.

```jsonc
// .quickcode/settings.json
{
  "permissions": {
    "allow": ["bash(uv run pytest*)", "bash(git status)", "edit(src/**)"],
    "ask":   ["bash(git push*)"],
    "deny":  ["read(.env)", "read(**/*.pem)", "bash(curl * | *sh)"]
  },
  // The starting mode is a plugin setting, and it is the one the Settings UI
  // writes. In a project file it is subject to the trust gate below.
  "plugins": {
    "runtime.permissions": { "settings": { "default_mode": "ask" } }
  }
}
```

Syntax:

- `bash(npm run build)` exact · `bash(npm *)` wildcard (spans spaces) · `bash(ls *)` with space = word boundary (won't match `lsof`)
- `edit(src/**)`, `read(.env)` (bare filename matches at any depth), `read(//C/Users/...)` absolute vs `read(/src/**)` settings-file-relative — gitignore-spec matching
- `agent(researcher)` — gate which subagent types may spawn
- Bare tool name (`write`) matches all uses; as a **deny** it removes the tool from the model's tool list entirely

**Settings scopes & precedence** (rule-kind beats scope, then): CLI flags (session-only) → `./.quickcode/settings.local.json` (gitignored) → `./.quickcode/settings.json` (shared, checked in) → `~/.quickcode/config.json` (user).

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
  → shell-parse into subcommands (split on && || ; | |& & and newlines)
  → per subcommand:
      strip harmless wrappers (timeout, time, nice, nohup) and env-var prefixes*
      → builtin read-only? (ls cat pwd head tail wc which stat diff cd,
        read-only git forms, rg) → auto-allow, unless the subcommand
        carries an env-var prefix
      → deny rules → ask rules → allow rules → mode default
  → final decision = most restrictive across subcommands
```

- *Env-prefix stripping is for **deny** matching: `FOO=x rm -rf y` still hits a `rm` deny. It does **not** buy the read-only auto-allow, and it does not match an allow rule written against the bare command — `PATH=. ls` is not `ls`, and approving `git status` is not approving `LD_PRELOAD=./x.so git status`. A rule that spells the assignment out still matches.
- Any assignment disqualifies, not a list of dangerous names: such a list would have to be complete, and `PATH`/`LD_PRELOAD` are only the obvious entries next to `BASH_ENV`, `IFS`, `PYTHONSTARTUP`, `NODE_OPTIONS` — and `RIPGREP_CONFIG_PATH`, which points `rg` (a read-only builtin) at a config file that can set `--pre`, which runs a program. The set grows with every program installed on the machine. The cost of the conservative reading is one prompt for `FOO=1 ls`.
- Exec-style wrappers that smuggle commands (`watch`, `xargs -I`, `find -exec`, `setsid`) are never stripped → always prompt unless the full string matches a rule.
- Approving a compound command with "always allow" persists **one rule per subcommand**, not one rule for the whole line.
- Windows: same pipeline for PowerShell with alias canonicalization (`gci|ls|dir → Get-ChildItem`) when the shell is PowerShell.

## The prompt (UI in docs/UI.md)

Three options, keyboard `y / a / n`:

1. **Allow once**
2. **Always allow `<generated rule>`** — the modal shows the exact rule text before it's written to `settings.local.json`
3. **Deny & redirect** — free-text box; the text is returned to the model as the tool result (`is_error`), so denial is steering, not a dead end

While any agent is blocked on a prompt, its tab/pane row glows orange (never an invisible modal in an unfocused conversation).

## Plan mode

- Entry: `Shift+Tab`, `/plan`, or `--mode plan`. System prompt gains a `<plan_mode>` section: *investigate, don't mutate; produce a plan; call the `plan` tool when ready*.
- Enforcement is **structural, not prompt-based**: in plan mode the mutating tools are withheld from the request's tool list (the model can't call what isn't offered), and the bash pipeline only permits builtin read-only commands.
- It lives in `PlanModeHook` (`core/hooks.py`), not in the loop. The hook hides every tool declaring `mutates` unless it also declares `shell` — a shell tool is only partly mutating and the engine gates it per subcommand — and it intercepts the `plan` call to run the review. Because the rule is written against the declaration rather than against two tool names, a plugin's mutating tool is withheld in plan mode too.
- Exit: the model calls `plan(markdown)` → **PlanReviewModal**:
  1. **Approve & auto-edit** — plan accepted, mode drops to `auto-edit` for execution
  2. **Approve, manual** — mode drops to `ask`
  3. **Keep planning** — feedback text returns to the model, stays in plan mode
  - `Ctrl+G` opens the plan in `$EDITOR`; saved edits replace the plan text before approval.
- On approval the plan is pinned: injected as a `<approved_plan>` system-reminder on subsequent turns and shown as a collapsible card in the sidebar; task board seeded from its steps (docs/AGENTS.md).

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

`-p` runs with `dontask`: rule-matched actions run, everything else auto-denies with a structured reason in the result — the run never hangs on an invisible prompt. `--mode yolo -p` exists for sandboxed CI use, same circuit breakers.
