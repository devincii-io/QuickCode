# Hooks

A hook is a shell command QuickCode runs at a fixed point in a conversation:
before a tool call, after one, when you send a message, when a turn ends, when
a session starts. It gets a JSON description of what is happening on stdin and
answers with its exit code, optionally with JSON on stdout. A hook can refuse a
tool call, demand a permission prompt, send the model a note, refuse a message
or add context to it — and it can never make the agent *less* careful than the
permission engine already is.

The configuration shape, the payload fields and the output keys are Claude
Code's, so a hook written for one runs on the other. The differences are listed
at the end.

Hooks are built on the loop-hook seam in `quickcode/core/hooks.py` — the same
one plan mode uses — and live in `quickcode/hooks/`.

## Configuring

Hooks go under `hooks` in a settings file — by hand, or from the **Hooks** page
in Configuration (`#/config/hooks`, see [The Hooks page](#the-hooks-page)),
which writes the same files:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "bash",
        "hooks": [
          { "type": "command", "command": "python ~/.quickcode/guard.py", "timeout": 10 }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "write|edit",
        "hooks": [{ "type": "command", "command": "npx prettier --write . >/dev/null" }]
      }
    ],
    "SessionStart": [
      { "hooks": [{ "type": "command", "command": "git status --short" }] }
    ]
  }
}
```

| File | Scope | Runs |
|---|---|---|
| `~/.quickcode/settings.json` | user | always |
| `<project>/.quickcode/settings.json` | project | only while the project is trusted |
| `<project>/.quickcode/settings.local.json` | project | only while the project is trusted |

Both scopes run: user hooks first, then the project's. Within one scope, the
same command declared twice for the same event and matcher runs once.

- `matcher` — which tools a `PreToolUse` or `PostToolUse` hook sees. Empty or
  `*` means every tool. Otherwise `|` separates alternatives, and each is an
  exact tool name or a glob: `bash`, `write|edit`, `mcp__*`,
  `mcp__docs__*|web_fetch`. Matching ignores case, so Claude Code's `Bash`
  still selects `bash`. These are globs, not regular expressions:
  `mcp__docs__.*` matches nothing. On the other events a matcher is ignored.
- `type` — `command` is the only type.
- `command` — run by the shell the `bash` tool uses: `/bin/bash -lc` on
  Linux and macOS, Git Bash (or PowerShell without it) on Windows.
- `timeout` — seconds, default 30, at most 600.

A malformed entry is skipped on its own and reported under Settings →
Problems with the JSON path of the entry; the rest of the block still runs.

The configuration is read when a session's first turn starts and held for the
rest of the session. Editing the `hooks` block — in the file or on the Hooks
page — reaches new sessions; editing a script a hook runs takes effect on its
next run.

## Events

| Event | When it runs | Matcher | What it can do |
|---|---|---|---|
| `PreToolUse` | after the permission engine decides a call, before it runs | tool name | refuse the call, or demand a permission prompt |
| `PostToolUse` | after a tool ran and returned a result | tool name | send the model a note alongside the result |
| `UserPromptSubmit` | when you send a message, before the model sees it | — | refuse the message, or add context to it |
| `Stop` | when a turn finishes on its own | — | run (for example a notifier); it cannot block |
| `SessionStart` | once per session, as its first turn starts | — | add context for the model; it cannot block |

`Stop` does not run when a turn is interrupted or fails. `SessionStart` runs at
the first turn rather than when a window opens, because opening a project opens
a conversation and a hook should not run for a session nobody used.

Subagents run the `PreToolUse` and `PostToolUse` hooks on their own tool calls,
so a guard cannot be stepped around by delegating. A delegation is not a user
message and a subagent finishing is not the end of a turn, so the other three
events apply to the conversation you are typing into only.

## Input

Every hook receives one JSON object on stdin:

```json
{
  "session_id": "3f2c9d1e0a4b",
  "transcript_path": "/work/app/.quickcode/sessions/3f2c9d1e0a4b.jsonl",
  "cwd": "/work/app",
  "hook_event_name": "PreToolUse",
  "permission_mode": "auto-edit",
  "agent_name": "main",
  "tool_name": "bash",
  "tool_input": { "command": "rm -rf build", "description": "clean" },
  "tool_use_id": "call_01"
}
```

Per event, on top of the common fields:

- `PreToolUse`: `tool_name`, `tool_input`, `tool_use_id`.
- `PostToolUse`: the same, plus `tool_response` — `{"content": …, "is_error": …}`.
- `UserPromptSubmit`: `prompt`.
- `Stop`: `last_assistant_message`, and `stop_hook_active` (always `false`).
- `SessionStart`: `source` — `startup` for a new session, `resume` for one
  reopened.

`agent_name` is `main` for the conversation itself and the subagent's id
(`explore-1`) for a subagent's call.

The payload is UTF-8. The hook runs in the project root with the environment
QuickCode was started with, plus `QUICKCODE_PROJECT_DIR`, and with
`PYTHONIOENCODING=utf-8` unless you set it yourself, so a Python hook on
Windows reads the payload correctly. QuickCode's own API keys — the model
provider's (`QUICKCODE_OPENROUTER_API_KEY`, `QUICKCODE_ANTHROPIC_API_KEY`) and
the web-search providers' — are removed, as they are from every process
QuickCode starts (`quickcode/subproc.py`): no hook needs them, and a hook that
logs its environment should not be how they leak.

A subagent working in its own git worktree (`isolation: "worktree"`) runs its
hooks there instead: `cwd` and `QUICKCODE_PROJECT_DIR` name the checkout it is
editing under `.quickcode/worktrees/`, and a file a hook writes there that git
does not ignore is committed with the subagent's work.

## Output

| Exit code | Meaning |
|---|---|
| `0` | fine; stdout may carry a JSON answer |
| `2` | block; stderr is the reason |
| anything else | the hook failed; you are shown stderr, and the call or message goes ahead |

**A hook that fails, fails open.** A crash, a missing program or a timeout is
reported and does not block — the same rule Claude Code has. A guard that
must hold should decide explicitly and exit 2.

What exit 2 does depends on the event: for `PreToolUse` the call is refused
and the model reads the reason as the tool's result; for `PostToolUse` the
reason is sent to the model as feedback (the tool has already run); for
`UserPromptSubmit` the message is not sent and you are shown the reason;
`Stop` and `SessionStart` cannot block, so you are only told.

On exit 0, stdout may be a JSON object. These keys are read:

```json
{
  "decision": "block",
  "reason": "src/generated is rebuilt by make; edit the template instead",
  "systemMessage": "shown to you, never to the model",
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "the reason the model reads",
    "additionalContext": "text for the model"
  }
}
```

- `hookSpecificOutput.permissionDecision` (`PreToolUse` only): `deny` refuses
  the call, `ask` demands a prompt, `allow` is recorded and changes nothing —
  see the next section.
- `decision`: `block` (or `deny`) blocks, as exit 2 would, with `reason` as the
  reason; `ask` is read for `PreToolUse` only; `approve` / `allow` are recorded.
- `additionalContext` (in `hookSpecificOutput`, or at the top level): for
  `UserPromptSubmit` and `SessionStart` it reaches the model as a
  system reminder on your message; for `PostToolUse` it is appended to the
  tool's result.
- `systemMessage`: a line in the transcript for you.

For `UserPromptSubmit` and `SessionStart`, stdout that is not JSON is taken as
context as it is, so `git status --short` works as a `SessionStart` hook. For
the other events plain stdout is ignored. If a login shell prints something
before the JSON, the last line of stdout is tried on its own. Anything a hook
sends the model is capped at 10,000 characters.

## Hooks and permissions

**`PreToolUse` runs after the permission engine and can only tighten its
answer.** The engine decides first — mode, deny/ask/allow rules, protected
paths, circuit breakers (`docs/PERMISSIONS.md#modes`) — and the hooks see a
call only if the engine did not deny it outright. A hook can then:

- turn an allow or an ask into a refusal (`deny`, `block`, or exit 2);
- turn an allow into a prompt (`ask`) — in `dontask` mode, which never waits
  for a person, that prompt becomes a refusal instead. The prompt says a hook
  raised it and shows the hook's reason (`permissionDecisionReason`, or
  `reason`), and **Always allow** has nothing to save: the rules already allow
  the call, and the hook will ask again next time;
- say `allow`, which is written to the log and does nothing else.

A hook can never turn a prompt or a refusal into an allow. That is the whole
design: a protected path (`.env`, `.git`, anything outside the project), a
circuit breaker (`rm -rf /`, a forced push) and a deny rule stay exactly as
strict as they are, whatever a hook prints. Hooks run in every mode, `yolo`
included — which is what makes a `PreToolUse` guard useful there.

Two calls never reach `PreToolUse`: one the engine denied, and a `plan` call,
which plan review answers before any gate runs.

## Trust

A project's hooks are a repository asking QuickCode to run programs, so they
go through the trust gate (`quickcode/security/trust.py`) with the project's
MCP servers and command tools:

- In a project you have not trusted, its hooks do not run. They are reported —
  in the trust banner with each command, as a problem in Settings, and once
  per session in the transcript — and your own user hooks still run.
- The grant is bound to a hash of the project's configuration, and the
  `hooks` blocks are part of it. A `git pull` that adds or edits a hook makes
  the project ask again before any of it runs.
- A project cannot switch off one of *your* hooks. `plugins.<hook id>.enabled:
  false` in a project file is honoured for a user hook only while the project
  is trusted, and it is part of the trust hash like an allow rule: a hook may
  be your guard, and a cloned repository turning it off would be widening by
  subtraction.

## Timeouts and interruption

Each hook has its own timeout. At the deadline the hook's whole process tree
is killed — a hook runs in its own process group on Linux and macOS, and
through `taskkill /T` on Windows — so a script that started a server does not
leave it behind. Stop (Esc) reaches a running hook the same way: the tree is
killed and the turn ends, rather than waiting out the timeout.

Every hook matching one event runs at the same time, and their answers combine:
any refusal refuses, otherwise any ask asks; notes and context are joined in
configuration order.

## The session log

Every run is recorded as a `hook_run` event — a new record type, added beside
the existing ones:

```json
{
  "type": "hook_run",
  "event": "PreToolUse",
  "outcome": "block",
  "hook_id": "hook.cmd.user.pre_tool_use.4e1f0a9c2b",
  "scope": "user",
  "tool": "bash",
  "call_id": "call_01",
  "decision": "block",
  "exit_code": 2,
  "ms": 84,
  "reason": "no rm -rf outside build/",
  "notice": ""
}
```

`outcome` is one of `ok`, `block`, `ask`, `error`, `timeout`, `interrupted`,
or `refused` for the record that says an untrusted project's hooks did not
run. `notice` is the sentence shown in the transcript, empty when there is
nothing to say. The command line and the payload are deliberately not
recorded: the hook is named by its id, the tool input is already in the log
once, and a command line may carry a token. `reason` is the hook's own output,
capped at 1,000 characters.

## Settings

Each configured hook is listed in Settings under **Hooks** as a plugin of kind
`hook`, with its event, matcher, command and file, and an enable switch. The id
is `hook.cmd.<scope>.<event>.<hash>`, where the hash covers the event, the
matcher and the command — so reordering the block does not move a switch onto
a different command, and editing a command gives it a new id. A project hook
that is not running because the project is untrusted has no card, only the
problem entry, because a switch would suggest it could run.

The switch is the card's; adding, changing and removing a hook is the Hooks
page's. Changing a hook's event, matcher or command gives it a new id, so a
switch set on the old one does not follow it. The switch of one of *your* hooks
is saved beside the hook, in `~/.quickcode/settings.json`, so it holds in every
project, trusted or not; flipping it also clears a project file's own switch
for that hook, which would otherwise outrank yours. A project hook's switch is
saved in the project's `.quickcode/settings.json`.

## The Hooks page

Configuration ▸ **Hooks** (`#/config/hooks`, `js/config/hooks.js`) lists every
hook the settings files declare, grouped by event, with the file it is in and
what happens to it:

| Status | Meaning |
|---|---|
| active | runs in new sessions |
| switched off | declared, and disabled by its Settings card |
| refused | a project hook in an untrusted project: saved, never run |

**Adding, changing and removing** edits the `hooks` block of the file the hook
lives in — `~/.quickcode/settings.json` for yours; the project's
`.quickcode/settings.json` (shared with whoever clones it) or
`.quickcode/settings.local.json` (this machine only) for the project's
(`quickcode/hooks/store.py`). The file is read, the one block is changed, and
the whole file is written beside the old one and renamed over it, so every
other key survives and a crash leaves the old file or the new one. A file that
does not parse as JSON is left alone and the save is refused: writing over it
would replace everything in it.

The form checks what the loader would otherwise drop or misread: the event
must be one of the five, the command must be non-empty (and contain no NUL),
the timeout must be more than 0 and at most 600 seconds, and a matcher must be
tool names and globs separated by `|` — no empty alternative, no unbalanced
`[`, nothing that reads like a regular expression (`.*`, `^`, `$`, `(`…), and
none at all on an event that is not about a tool. Saving a hook that the same
scope already declares is refused, because it would run once either way. When
one command is written twice in a file, changing or removing it changes or
removes every copy, so the old command cannot keep running from the other.

**Trust.** A write to a project file keeps the project's trust and grants
none, as every other settings write the app makes does (`trust.keep_trust`):

- in a trusted project, a hook you add or change here keeps the project
  trusted — you wrote it — and runs in new sessions;
- in a project that is not trusted, the hook is saved and listed as refused,
  and it runs only once you trust the project;
- an edit made *outside* the app since the grant (a pull that added a hook)
  has already untrusted the project, and saving here does not re-grant it over
  that edit.

**Test run.** *Test* runs one saved hook once, now, with a sample payload for
its event (`quickcode/hooks/trial.py`). It uses the runner a session uses —
the same shell, environment, project directory and the hook's own timeout —
and reads the answer with the same protocol, then shows the exit code, stdout
and stderr (each capped at 16,000 characters), how long it took, the payload
it was sent, and what that answer would do in a session ("the call would be
refused", "a failing hook fails open, so the call would go ahead"). For
`PreToolUse` and `PostToolUse` you choose the tool name and its arguments; for
`UserPromptSubmit`, the message. If a session would not have run the hook for
that sample — its matcher does not select that tool, or you asked for another
event — the result says so.

It never touches a session: no conversation is opened, no `hook_run` record is
written, no model is called. The payload's `session_id` is `test-run` and its
`transcript_path` is empty, so a hook can tell. The command itself does run —
a `Stop` hook that sends a notification sends one.

A project hook in an untrusted project is not test-run: a test run is a run,
and the refusal is the one the loop makes.

The routes behind the page (`quickcode/server/hooks_api.py`), each also under
`/api/projects/{pid}/…`:

| Route | Does |
|---|---|
| `GET /api/hooks` | every declared hook with its status, the project's trust, the hook problems |
| `POST /api/hooks` | add: `{event, matcher, command, timeout, scope, file}` |
| `PUT /api/hooks/{id}` | change the hook with that id: the same body, `file` naming its file |
| `DELETE /api/hooks/{id}?file=` | remove it |
| `POST /api/hooks/{id}/test` | test-run it: `{event?, tool_name?, tool_input?, prompt?, file?}` |

## Examples

A guard that refuses recursive deletes outside `build/`:

```python
#!/usr/bin/env python3
import json, re, sys

call = json.load(sys.stdin)
command = call.get("tool_input", {}).get("command", "")
if re.search(r"\brm\s+-\w*r", command) and "build/" not in command:
    print("Recursive deletes are limited to build/ in this repo.", file=sys.stderr)
    sys.exit(2)
```

```json
{
  "hooks": {
    "PreToolUse": [
      { "matcher": "bash", "hooks": [{ "type": "command", "command": "python .quickcode/hooks/guard.py" }] }
    ],
    "Stop": [
      { "hooks": [{ "type": "command", "command": "notify-send QuickCode 'turn finished'", "timeout": 5 }] }
    ]
  }
}
```

## Differences from Claude Code

- A hook's `allow` never skips a prompt; see *Hooks and permissions*.
- `Stop` and `SessionStart` cannot block, and `continue` / `stopReason` are
  not read. A `Stop` hook cannot keep the agent working.
- Only `command` hooks; no `Notification`, `SubagentStop`, `PreCompact` or
  `SessionEnd` events yet.
- Matchers are globs with `|` alternatives, not regular expressions.
- Hook configuration is read at a session's first turn and held; there is no
  review step for edits made mid-session, because none reach it.
