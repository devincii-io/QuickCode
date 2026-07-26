# Tool Surface

Six core tools (`read`, `write`, `edit`, `glob`, `grep`, `bash`) plus the agentic set (`agent`, `send_message`, `task_*`, `plan`, `ask_user` — table at the bottom, specced in docs/AGENTS.md). Small on purpose: too many tools degrade selection accuracy, and bash covers the long tail. Promotion rule (when does something deserve to be a dedicated tool instead of bash?): when the harness needs to **gate, render, parallelize, or enforce invariants** on it.

Every tool implements:

```python
class Tool[In: BaseModel]:
    name: str
    description: str            # prompt copy — see style rules in PROMPTS.md §3
    Input: type[In]             # Pydantic model → strict JSON Schema on the wire
    is_read_only: bool          # True → parallel-safe + auto-allowed
    async def run(self, input: In, ctx: ToolCtx) -> ToolResult: ...
    def render_call(self, input: In) -> Widget | str: ...    # "⏺ Read src/index.py"
    def render_result(self, r: ToolResult) -> Widget | str:  # diff view, match list, ...
        ...
```

`ToolResult` carries `content` (string for the model), `is_error`, and optional UI metadata. Truncation always happens **inside the tool**, with an explicit marker the model can act on:

```
<truncated shown="2000" total="6349" hint="re-run with offset=2000"/>
```

---

## read `[read-only]`

> Reads a file from the local filesystem. Call this before editing any file, and when you need to see actual code rather than search matches. Returns numbered lines (`123→code`). Reads up to 2000 lines by default; for larger files pass offset/limit. Prefer this over `bash cat`.

```json
{ "file_path": "string (absolute)", "offset": "number?", "limit": "number?" }
```

- Lines longer than 2000 chars are cut with a marker.
- Records `{path, mtime}` in the session's read-registry — the `edit` staleness check depends on it.
- Re-reading a file supersedes the old copy in history (read-dedup, see ARCHITECTURE).

## write

> Creates a new file, or fully replaces one that was already read this session. For any partial change to an existing file use `edit` instead — it is cheaper and reviewable as a diff.

```json
{ "file_path": "string (absolute)", "content": "string" }
```

- Overwriting a file that was never `read` → error (forces the model to look before it leaps).
- Renders as a diff against the previous content when overwriting.

## edit

> Performs an exact string replacement in a file. Call this for all modifications to existing files. `old_string` must match the file exactly (including whitespace) and be unique in the file — extend it with surrounding lines until it is. Use `replace_all` to rename a symbol everywhere.

```json
{
  "file_path": "string (absolute)",
  "old_string": "string",
  "new_string": "string",
  "replace_all": "boolean? (default false)"
}
```

- Errors (all returned as `is_error` with a actionable message): file not read this session · file changed on disk since read · 0 matches · >1 match without `replace_all`.
- Renders as a colored unified diff; the tool result to the model is a short confirmation + patched region snippet, not the whole file.

## glob `[read-only]`

> Fast file-pattern matching. Call this to find files by name or path (`src/**/*.ts`, `**/config.*`). Returns paths sorted by modification time, newest first. Prefer this over `bash find` or `ls -R`.

```json
{ "pattern": "string", "path": "string? (default cwd)" }
```

- Respects `.gitignore`; caps at 200 results with truncation marker.

## grep `[read-only]`

> Content search built on ripgrep with full regex support. Call this to find where something is defined, used, or mentioned. Filter with `glob` (e.g. `*.ts`). Prefer this over `bash grep` — it is faster and its results are paginated.

```json
{
  "pattern": "string (rust regex)",
  "path": "string?",
  "glob": "string?",
  "output_mode": "\"content\" | \"files_with_matches\" | \"count\" (default files_with_matches)",
  "context": "number? (lines around match, content mode)",
  "ignore_case": "boolean?",
  "head_limit": "number? (default 100)"
}
```

## bash

> **Design target:** `run_in_background` and persistent background-task output
> are not implemented in `0.1.0`; the tool returns an explicit error when that
> flag is requested. Current status is tracked in [ROADMAP.md](ROADMAP.md).

> Executes a command in ${shellName} on ${platform} and returns combined stdout+stderr. Use for builds, tests, git, package managers, and anything without a dedicated tool. Do NOT use for reading files or searching (use read/grep/glob). State persists via tracked cwd; quote paths containing spaces.

```json
{
  "command": "string",
  "description": "string (5-10 words shown to the user, e.g. \"Run test suite\")",
  "timeout_ms": "number? (default 120000, max 600000)",
  "run_in_background": "boolean?"
}
```

- Runs in a real PTY (`pty/session.py`, ConPTY on Windows — QuickTerm's reader/watcher/writer thread pattern, see ARCHITECTURE §PTY). Tracked cwd; persistent shell session per conversation.
- Output cap 30k chars to the model (head+tail kept, middle truncated with marker); the UI pane keeps the full scrollback ring. Background tasks stream to the ring, readable via a follow-up call and surfaced as a toast on exit.
- **Security:** commands are untrusted model output. The permission layer prompts unless the command matches a persisted allow-rule; commands with `;`, `&&`, `|`, `$()`, backticks never prefix-match a rule — full-string match or prompt. Process-tree kill on Esc/timeout.

---

## Agentic tools (specced in docs/AGENTS.md and docs/PERMISSIONS.md)

| Tool | Purpose |
|---|---|
| `agent` | Spawn a subagent (own pane, own model, capped permissions); background by default. |
| `send_message` | Message/resume a subagent or teammate by name/id. |
| `task_create` / `task_update` / `task_list` / `task_get` | The task board — solo checklist *and* teammate coordination backbone (dependencies, file-locked claiming). No separate todo tool. |
| `plan` | Present a plan for approval and exit plan mode (docs/PERMISSIONS.md §Plan mode). |
| `ask_user` | Structured question with options → rendered as a modal. |

## Later candidates (explicitly deferred)

| Tool | Why deferred |
|---|---|
| `web_fetch` / `web_search` | Useful but pulls in provider-specific behavior; add once core loop is solid. |
| `notebook_edit` | Niche. |

## Wire format note

On the OpenAI-compatible wire, tools go as `{type: "function", function: {name, description, parameters}}` with `strict: true` where the endpoint supports it; results return as `role: "tool"` messages keyed by `tool_call_id` — the tool registry owns this translation, tools themselves never see wire formats.
