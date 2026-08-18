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
async def run_turn(self, user_input):
    self.history.push_user(user_input, self.system_reminders())
    while True:
        stream = self.provider.stream_chat(self.build_request(), self.cancel_scope)
        async for ev in stream: self.bus.emit(ev)
        msg = stream.final()
        self.history.push_assistant(msg)
        if not msg.tool_calls: break
        results = await self.execute_tools(msg.tool_calls)   # permission-gated
        self.history.push_tool_results(results)              # ALL in ONE message
```

Rules that matter:

- **All tool results return in a single message** — splitting them trains the model out of parallel calls.
- **Read-only tools run concurrently** (`asyncio.gather`); mutating tools sequentially in call order.
- **Failed tools still return a result** with `is_error: true` so the model can recover.
- **Loop guard:** max 50 tool rounds per turn, then a system reminder to wrap up.
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
| `auto-edit` | ✅ auto | ✅ auto | prompt | |
| `yolo` | ✅ auto | ✅ auto | ✅ auto | explicit opt-in, red status bar |

- Prompt choices: **allow once · always allow (persist rule) · deny with message** (deny text returns as the tool result so the model adapts).
- Rules persist in `./.quickcode/settings.json` (`allow`/`deny`/`ask` arrays, `bash(npm test*)`-style patterns). Deny beats allow. Compound bash commands (`;`, `&&`, `|`, `$(`) never prefix-match — full-string rule or prompt.
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
- A **Conversation** = one main AgentInstance + its transcript + its spawned subagents. The manager holds a registry; the **session switcher** (docs/UI.md) jumps between them; every conversation keeps running when unfocused.
- Subagents and teammates are just more AgentInstances with different system prompts, models, and permission caps — one runtime, no special cases. Coordination (task board, teammate messaging, result hand-back) is specced in docs/AGENTS.md.
- Session store: every event appends to `./.quickcode/sessions/<conv-id>.jsonl`; `--continue` / `--resume` rebuild conversations, including still-open task boards.

## Efficiency checklist

1. **Cache-stable prefix:** request order `tools → system → history`, byte-identical across turns. No timestamps/randomness in the system prompt; dynamic state travels as `<system-reminder>` blocks in user messages.
2. **Parallel tool calls** honored (gather) and encouraged in the prompt.
3. **Cheap models for fan-out:** research/search subagents default to a configured `worker_model` (sonnet-tier), orchestrator stays on the big model.
4. **Diff-based edits**; output caps + pagination hints on every tool; read-dedup (superseded file reads stubbed out of the request).
5. **Compaction at ~80%** of the model's context window; manual `/compact`.
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
