# Permissions & Plan Mode

Modeled on Claude Code's system (researched against current docs), simplified where cloning adds no value. Two principles:

1. **Parse, don't prefix-match.** String-prefix matching on bash commands is trivially bypassed by `&& rm -rf`. We decompose commands and evaluate each subcommand.
2. **Deny beats allow, everywhere.** A deny rule from *any* settings scope beats an allow rule from any other scope. Precedence is about rule kind first, file origin second.

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
  "defaultMode": "ask"
}
```

Syntax:

- `bash(npm run build)` exact · `bash(npm *)` wildcard (spans spaces) · `bash(ls *)` with space = word boundary (won't match `lsof`)
- `edit(src/**)`, `read(.env)` (bare filename matches at any depth), `read(//C/Users/...)` absolute vs `read(/src/**)` settings-file-relative — gitignore-spec matching
- `agent(researcher)` — gate which subagent types may spawn
- Bare tool name (`write`) matches all uses; as a **deny** it removes the tool from the model's tool list entirely

**Settings scopes & precedence** (rule-kind beats scope, then): CLI flags (session-only) → `./.quickcode/settings.local.json` (gitignored) → `./.quickcode/settings.json` (shared, checked in) → `~/.quickcode/config.json` (user). A project file cannot grant itself `yolo` as `defaultMode` — that's honored from user config or flags only.

## Bash evaluation pipeline

```
command string
  → shell-parse into subcommands (split on && || ; | |& & and newlines)
  → per subcommand:
      strip harmless wrappers (timeout, time, nice, nohup, env-var prefixes*)
      → builtin read-only? (ls cat pwd head tail wc which stat diff cd,
        read-only git forms, rg) → auto-allow
      → deny rules → ask rules → allow rules → mode default
  → final decision = most restrictive across subcommands
```

- *Env-prefix stripping applies for **allow** matching only — `FOO=x rm -rf y` still hits a `rm` deny.
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
- Exit: the model calls `plan(markdown)` → **PlanReviewModal**:
  1. **Approve & auto-edit** — plan accepted, mode drops to `auto-edit` for execution
  2. **Approve, manual** — mode drops to `ask`
  3. **Keep planning** — feedback text returns to the model, stays in plan mode
  - `Ctrl+G` opens the plan in `$EDITOR`; saved edits replace the plan text before approval.
- On approval the plan is pinned: injected as a `<approved_plan>` system-reminder on subsequent turns and shown as a collapsible card in the sidebar; task board seeded from its steps (docs/AGENTS.md).

## Hooks (extension point, post-MVP)

A `pre_tool_use` hook: user-configured script receiving `{tool_name, tool_input, mode, cwd}` on stdin, answering `{"decision": "allow"|"deny"|"ask"|"defer", "reason": "..."}`. Decisions are subordinate to deny/ask rules (a hook cannot override a deny). Exit code 2 = hard block with stderr as the reason shown to the model. This gives Devin-style automation (lint gates, org policy) without forking the core.

## Headless mode

`-p` runs with `dontask`: rule-matched actions run, everything else auto-denies with a structured reason in the result — the run never hangs on an invisible prompt. `--mode yolo -p` exists for sandboxed CI use, same circuit breakers.
