# Architecture

## Stack

- **Runtime:** Python 3.12+, `uv` for env/packaging, `quickcode` console script (+ `qc` alias)
- **Server:** FastAPI + uvicorn on 127.0.0.1, WebSocket for the live event stream
- **UI:** vanilla ES modules, no bundler and no build step, served as static files
- **Window:** pywebview (WebView2 on Windows) — a native app window, not a browser tab
- **Wire client:** `openai` package, `AsyncOpenAI(base_url=...)` — one client class, many backends (OpenRouter default)
- **Schemas:** Pydantic models → strict JSON Schema for tools
- **Search:** ripgrep (`rg` on PATH; pure-Python fallback so nothing breaks without it)
- **PTY (bash tool):** `pywinpty` (ConPTY) on Windows, `pty` on POSIX — patterns lifted from QuickTerm (see below)

## Layer diagram

```
┌──────────────────────────────────────────────────────────────┐
│ Native window (pywebview) → frontend/ (ES modules)           │
│  chat · trajectory · agents/tasks/files/usage panels         │
│  settings: plugins, prompt, presets          (see docs/UI.md)│
└───────────▲──────────────────────────┬───────────────────────┘
            │ WebSocket events         │ input / approvals / steering
┌───────────┴──────────────────────────▼───────────────────────┐
│ Server (FastAPI)                                             │
│  ProjectHub → ConversationManager → Conversation             │
│  REST: bootstrap, sessions, models, kernel, presets, prompt  │
└───────────▲──────────────────────────┬───────────────────────┘
            │ AgentEvent (bus)         │
┌───────────┴──────────────────────────▼───────────────────────┐
│ Agent runtime                                                │
│  AgentInstance = loop.py + history + ledger + hooks          │
│   · main agent per conversation                              │
│   · subagents (spawned via the agent tool)  (see docs/AGENTS)│
│  permissions.py (modes, rules, tool-declared specs)          │
│  hooks.py (plan mode) · tasks.py · compact.py                │
└──────▲──────────────────────┬────────────────────────────────┘
       │ normalized stream    │ tool_use
┌──────┴────────┐   ┌─────────▼────────────────────────────────┐
│ Provider layer│   │ Tool system                              │
│ openai_compat │   │  registry · read/write/edit/glob/grep    │
│ (OpenRouter,  │   │  bash(PTY) · task_* · agent · plan       │
│  OpenAI,      │   │  PermissionSpec → gating, parallelism    │
│  Ollama, …)   │   │  + entry-point plugins, + MCP tools      │
└───────────────┘   └──────────────────────────────────────────┘
┌──────────────────────────────────────────────────────────────┐
│ Plugin kernel: what exists, and what may be changed          │
│  spec · registry · manifest · preset · state (settings.json) │
└──────────────────────────────────────────────────────────────┘
┌──────────────────────────────────────────────────────────────┐
│ Persistence: config.py · session store (JSONL) · task board  │
└──────────────────────────────────────────────────────────────┘
```

## Repo layout

```
pyproject.toml            # [project.scripts] quickcode = "quickcode.cli:main"
quickcode/
  cli.py                  # args, config, web app vs headless (-p) dispatch
  webapp.py               # uvicorn on a loopback port + the native window
  ui/window.py            # pywebview window, browser fallback, single instance
  frontend/               # index.html, css/, js/  (see docs/UI.md)
  core/
    agent.py              # AgentInstance: loop + history + ledger + event bus
    loop.py               # the agentic loop (single turn driver)
    hooks.py              # LoopHook: tool visibility, call interception
    events.py             # AgentEvent dataclasses (internal protocol)
    history.py            # messages, serialization, read-registry
    compact.py            # threshold + summarization turn
    permissions.py        # modes, rules, PermissionSpec, bash decomposition
    tasks.py              # task board
  kernel/                 # the plugin kernel (below)
    spec.py registry.py manifest.py bootstrap.py preset.py state.py
  server/
    app.py                # FastAPI routes + WebSocket attach
    manager.py            # ConversationManager / Conversation
    projects.py           # ProjectHub, project registry
    serialization.py auth.py gitinfo.py
  providers/
    base.py openai_compat.py
  tools/
    base.py registry.py
    read.py write.py edit.py glob.py grep.py bash.py
    agent.py send_message.py task.py plan.py
  plugins/
    loader.py             # quickcode.tools / quickcode.providers entry points
    mcp.py                # MCP client + tool adapter
  prompts/
    sections.py           # the system prompt, one section per block
    system.py compact.py subagent.py
  pty/session.py          # ConPTY/posix PTY session (QuickTerm patterns)
  config.py
  session/store.py        # JSONL transcripts + conversation registry
```

## The plugin kernel

Everything internal is a plugin: tools, prompt sections, providers, agents,
MCP servers, the loop hooks, the permission policy, the session log. The
kernel does not run them — the subsystems do — it records *what exists* and
*what may be changed*, and the Settings UI reads exactly that. The list the UI
shows is built from the live objects, so it cannot drift from what the agent
actually has.

Mutability is declared per setting in three tiers: `free` (change it),
`confirm` (changeable, but the caller must pass `confirmed=True` and the UI
must name the risk first) and `locked` (never changeable — the tool-call
protocol, the event-log format, the subagent report sanitizer). **Locked never
means hidden:** every plugin exposes a view of its raw definition at every
tier.

A **preset** is the plugin composition one session runs — its tools, its
subagents, its prompt, its default mode. A session records the preset it
started with and keeps it on resume: the conversation was already told what
tools it had, and changing them underneath it would be a lie.

## Async model

- Each **AgentInstance** runs as an asyncio task. No threads except the PTY reader/writer (below) and the server thread when the native window owns the main one.
- Agents emit `AgentEvent`s onto their own **event bus**; each attached WebSocket subscribes with a **bounded queue**. On overflow the client is dropped with a sentinel and reconnects, replaying from the log. (QuickTerm's pattern for fast producers + slow consumers — never unbounded buffering, never a frozen UI.)
- The frontend batches bursts with `requestAnimationFrame`; streaming text patches one live node rather than re-rendering the transcript.
- Permission and plan review round-trip over the WebSocket: the loop `await`s an `asyncio.Future` that a `permission_decision` / `plan_decision` message resolves — clean backpressure, no callback soup.
- **Cancellation:** interrupt cancels the agent's task → aborts the in-flight HTTP stream, kills the PTY process tree, marks the partial turn `[interrupted]` in history.

## The agent loop

Per-instance state machine: `idle → sending → streaming → executing_tools → (loop) → idle`.

```python
async def run_turn(agent, user_input):
    agent.history.push_user(user_input, reminders)
    max_rounds = agent.limits.max_rounds          # read once, per turn
    for round_no in range(max_rounds + 1):
        if round_no == max_rounds:
            agent.history.push_user("", [wrap_up_reminder])
        msg = await _stream_once(agent)           # streams, emits, assembles
        agent.history.push_assistant(msg)
        if not msg.tool_calls: return msg.text
        results = await _execute_tools(agent, msg.tool_calls)  # permission-gated
        agent.history.push_tool_results(results)  # all of them, in one push
```

Rules that matter:

- **The loop is bounded, not `while True`.** The counter *is* the guard: the budget is a `range`, and the extra iteration at `round_no == max_rounds` exists to deliver the wrap-up reminder and take one last answer.
- **All tool results for a round are pushed together** — splitting them across turns trains the model out of parallel calls. They go in as *consecutive* `role: "tool"` messages, one per `tool_call_id`, in call order, which is what the wire format requires; there is no single combined message.
- **Read-only tools run concurrently** (`asyncio.gather`); mutating tools sequentially in call order.
- **Failed tools still return a result** with `is_error: true` so the model can recover.
- **Loop guard:** `runtime.agent_loop.max_rounds` tool rounds per turn, then a system reminder to wrap up. 50 is the default (`RuntimeLimits.max_rounds` in `kernel/composition.py`, declared as a setting in `kernel/manifest.py`), not a constant — it is resolved per session by `kernel/resolve.runtime_limits` and frozen for the turn, so editing the setting mid-turn cannot move the budget under a turn already counting.
- **The loop knows no tool by name.** Which tools are offered is decided by hooks (`visible_tools`), a hook may answer a call itself (`intercept`, which is how plan review works), and how a call is gated comes from the tool's own `PermissionSpec`. Plan mode used to be an `if` in this file; it is now `PlanModeHook` in `core/hooks.py`.

## Provider layer

The core only speaks this; adapters translate wire formats:

```python
class Provider(Protocol):
    def stream_chat(self, req: ChatRequest, cancel: CancelScope) -> AgentStream: ...
    async def list_models(self) -> list[ModelInfo]: ...

AgentEvent = (
    TextDelta | ReasoningDelta
    | ToolCallStart | ToolCallDelta | ToolCallEnd
    | Usage        # input/output/cached tokens, cost → ledger + status bar
    | TurnDone     # finish_reason: stop | tool_calls | length | error
)
```

### `openai_compat` (default)

- `base_url` from the active profile; default `https://openrouter.ai/api/v1`, key from `OPENROUTER_API_KEY`. Any OpenAI-compatible endpoint works (OpenAI, Groq, Ollama at `localhost:11434/v1`, …).
- Streaming chat completions, OpenAI-style `tools`, buffered `tool_calls` argument deltas.
- Usage in-stream (OpenRouter `usage: {include: true}`) feeds the ledger; model list from `GET /models` filtered to tool-capable feeds the picker.
- `reasoning` param passthrough (OpenRouter normalizes effort across vendors); deltas surface as `ReasoningDelta`.
- Prompt caching: `cache_control` breakpoints on system tail + last history block — forwarded to Anthropic models by OpenRouter; OpenAI-family caches automatically; harmless elsewhere.
- **Per-agent model choice:** every AgentInstance carries its own model — expensive orchestrator, cheap workers (see docs/AGENTS.md).

### `anthropic` (later)

Native Messages API behind the same Protocol: adaptive thinking + effort, exact cache breakpoints, server-side compaction.

## Permission system

Full design in docs/PERMISSIONS.md; core model:

| Mode | Read-only | Edits | Bash/mutating | Notes |
|---|---|---|---|---|
| `plan` | ✅ auto | ❌ blocked | ❌ blocked | research only; exits via plan approval |
| `ask` (default) | ✅ auto | prompt | prompt | |
| `auto-edit` | ✅ auto | ✅ auto | prompt | edits only; no file-op command allowlist |
| `dontask` | ✅ auto | rule-matched, else auto-deny | rule-matched, else auto-deny | never prompts |
| `yolo` | ✅ auto | ✅ auto | ✅ auto | explicit opt-in, red status bar |

`Mode` has these five members and no others. A four-mode summary that omits
`dontask` used to sit here, which is how the one mode that silently *denies*
went undocumented in the architecture overview.

- Prompt choices: **allow once · always allow (persist rule) · deny with message** (deny text returns as the tool result so the model adapts).
- Rules persist in `./.quickcode/settings.local.json` — "always allow" writes there; `./.quickcode/settings.json` is the shared, checked-in half (`allow`/`deny`/`ask` arrays, `bash(npm test*)`-style patterns). Deny beats allow.
- A compound line is **split** on `;`, `&&`, `||`, `|` and `&`, and each subcommand is rule-matched on its own — that is the "parse, don't prefix-match" principle, and it means a rule whose pattern spans a splitter can never match. Substitution and redirection are the different case: a line containing `$(`, a backtick, `>` or `<` never matches an allow rule at all and never takes the read-only auto-allow — full-string deny rule or prompt.
- Mode cycling on a hotkey; per-conversation override; subagents/teammates inherit a *capped* mode (a yolo main agent does not imply yolo workers — see docs/AGENTS.md).
- Edits outside the project root always prompt.

## PTY subsystem (QuickTerm lessons, applied)

The `bash` tool runs commands in a real PTY (`pty/session.py`) instead of pipe-only subprocesses — interactive-ish tools, colors, and correct Ctrl+C semantics come free. Direct imports from QuickTerm's battle-tested design:

- **One ConPTY, three daemon threads:** reader (coalesces all immediately-available output into one callback, ≤128 KB), watcher (waits on the real process handle — winpty EOF lags ~8 s behind actual exit), writer (queue-drained; **PTY writes never run on the event loop** — a full stdin pipe blocks).
- **Bytes on the hot path**, decode once at the UI/model boundary (UTF-8 + surrogateescape, never `errors="replace"`).
- **Scrollback ring as a deque of chunks** (O(chunk) trim) for background-task buffers, not a flat bytearray.
- **Process-tree kill** on Esc/timeout (Windows: `taskkill /T` semantics via the ConPTY handle).
- Output to the model stays capped (30k chars, head+tail) with truncation markers; the *pane* can still show the full ring.

## Multi-project, multi-conversation, multi-agent runtime

- A **ProjectHub** holds one `ConversationManager` per open project; a project id is a stable hash of its resolved path, so it is the same id every run.
- A **Conversation** = one main AgentInstance + its transcript + its spawned subagents. The manager holds a registry, and the topbar's session tabs and switcher jump between them. A conversation the browser is not attached to stays *open* server-side — its agent, task board and background jobs survive — but nothing streams to a client that is not there, and the browser holds exactly one socket (`frontend/js/ws.js` enforces it with a generation guard). The tabs are shortcuts into that registry, not concurrent live sessions.
- Subagents and teammates are just more AgentInstances with different system prompts, models, and permission caps — one runtime, no special cases. Coordination (task board, teammate messaging, result hand-back) is specced in docs/AGENTS.md.
- **Spend vs. context.** Each AgentInstance owns a `Ledger`, so a child's tokens reach the session only through the recorder, which bridges every subagent bus. It rolls them in with `Ledger.add_subagent`: the cumulative fields (`input_tokens`, `output_tokens`, `cached_tokens`, `cost_usd`) take them, and `last_input_tokens` / `last_output_tokens` never do. That pair is the *live context footprint* — it drives `context_pct()`, the context meter and the compaction threshold — and a subagent fills a context window of its own, so counting its request there would show a short conversation as nearly full and could trip an auto-compaction the parent never needed. `Ledger.from_events` replays the same split from the log, reading the child's usage out of the `agent_event` wrapper it is logged inside.
- Session store: the trace appends to `./.quickcode/sessions/<conv-id>.jsonl`. Not *every* event — `server/serialization.py` holds a `LOGGED_TYPES` set and `loggable()` admits only the assembled shapes (`user_message`, `assistant_message`, `system_prompt`, `context_injection`, `tool_call`, `tool_result`, `usage`, the permission/plan request-and-resolution pairs, `mode_changed`, `model_changed`, `compacted`, `agent_spawned`, `agent_done`, `system_note`, `error`). Streaming deltas and transient status flips stay live-only, which is why the log replays as a transcript rather than as a keystroke recording. A subagent's assembled events (its tool calls, its results, its usage, its final message) are logged the same way, one level down inside an `agent_event` wrapper carrying the child's id and the spawning turn. A plugin can add one more type via `register_event(..., logged=True)`. `--continue` / `--resume` rebuild conversations, including still-open task boards.

## Efficiency checklist

1. **Cache-stable prefix:** request order `tools → system → history`, byte-identical across turns. No timestamps/randomness in the system prompt; dynamic state travels as `<system-reminder>` blocks in user messages.
2. **Parallel tool calls** honored (gather) and encouraged in the prompt.
3. **Cheap models for fan-out:** research/search subagents default to a configured `worker_model` (sonnet-tier), orchestrator stays on the big model.
4. **Diff-based edits**; output caps + pagination hints on every tool; read-dedup (superseded file reads stubbed out of the request).
5. **Compaction at ~80%** of the model's context window; manual `/compact`. Both drivers check it after every turn — the web worker and `TranscriptRecorder.record_turn`, which is what a headless `-p` run goes through — off the one declared setting (`runtime.compaction`).
6. **UI never blocks the loop, loop never blocks the UI** — bounded queues both directions.

## Trust boundary

The API answers the QuickCode window and nothing else: a Host allowlist
defeats DNS rebinding, an Origin allowlist defeats cross-origin requests from
other pages, and a per-install loopback token (`server/auth.py`) stops other
local processes. The token reaches the frontend in the URL fragment, which is
never sent to the server and never logged. Static frontend files carry no
secrets and stay open so the shell can bootstrap.

## Windows notes

- `bash` targets Git Bash when present, else PowerShell; the active shell is named in the tool description so the model writes matching syntax.
- Paths normalized to forward slashes in tool results; `rg` resolved from PATH with a bundled-download helper (`quickcode doctor` offers to fetch it).
