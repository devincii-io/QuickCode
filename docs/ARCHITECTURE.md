# Architecture

## Stack

- **Runtime:** Python 3.12+, `uv` for env/packaging, `quickcode` console script (+ `qc` alias)
- **TUI:** Textual (asyncio-native; widgets, mouse, modal screens, command palette, TCSS theming)
- **Wire client:** `openai` package, `AsyncOpenAI(base_url=...)` — one client class, many backends (OpenRouter default)
- **Schemas:** Pydantic models → strict JSON Schema for tools
- **Search:** ripgrep (`rg` on PATH; pure-Python fallback so nothing breaks without it)
- **PTY (bash tool):** `pywinpty` (ConPTY) on Windows, `pty` on POSIX — patterns lifted from QuickTerm (see below)

## Layer diagram

```
┌──────────────────────────────────────────────────────────────┐
│ TUI (Textual App)                                            │
│  ConversationPane(s) · AgentPane(s) · TeamSidebar            │
│  PermissionModal · PlanReviewModal · ModelPicker · Palette   │
│  ConversationSwitcher · StatusBar            (see docs/UI.md)│
└───────────▲──────────────────────────┬───────────────────────┘
            │ AgentEvent (bus)         │ input / approvals / steering
┌───────────┴──────────────────────────▼───────────────────────┐
│ Agent Runtime (multi-agent)                                  │
│  AgentInstance = loop.py + history + token ledger            │
│   · main agent per conversation                              │
│   · subagents (spawned via agent tool)     (see docs/AGENTS) │
│   · teammates (peer agents, shared task board)               │
│  permissions.py (modes, rules, plan-mode gate)               │
│  tasks.py (task board) · compact.py                          │
└──────▲──────────────────────┬────────────────────────────────┘
       │ normalized stream    │ tool_use
┌──────┴────────┐   ┌─────────▼────────────────────────────────┐
│ Provider layer│   │ Tool system                              │
│ openai_compat │   │  registry · read/write/edit/glob/grep    │
│ (OpenRouter,  │   │  bash(PTY) · todo · agent · task_*       │
│  OpenAI,      │   │  is_read_only → parallel + auto-allow    │
│  Ollama, …)   │   └──────────────────────────────────────────┘
│ [anthropic]   │
└───────────────┘
┌──────────────────────────────────────────────────────────────┐
│ Persistence: config.py · session store (JSONL) · task board  │
└──────────────────────────────────────────────────────────────┘
```

## Repo layout (target)

```
pyproject.toml            # [project.scripts] quickcode = "quickcode.cli:main"
quickcode/
  cli.py                  # args, config, TUI vs headless (-p) dispatch
  app.py                  # Textual App: screens, bindings, pane management
  ui/                     # see docs/UI.md
    transcript.py tool_call.py diff.py input.py statusbar.py
    panes.py switcher.py modals.py team.py theme.tcss
  core/
    agent.py              # AgentInstance: loop + history + ledger + event bus
    loop.py               # the agentic loop (single turn driver)
    events.py             # AgentEvent dataclasses (internal protocol)
    history.py            # messages, serialization, read-registry
    compact.py            # threshold + summarization turn
    permissions.py        # modes, rules, plan gate, bash matching
    tasks.py              # shared task board (teammate coordination)
  providers/
    base.py openai_compat.py  # + anthropic.py later
  tools/
    base.py registry.py
    read.py write.py edit.py glob.py grep.py bash.py
    agent.py send_message.py task.py plan.py ask_user.py
  prompts/
    system.py compact.py subagent.py teammate.py
  pty/
    session.py            # ConPTY/posix PTY session (QuickTerm patterns)
  config.py
  session/store.py        # JSONL transcripts + conversation registry
```

## Async model (Textual-native)

- Each **AgentInstance** runs as an asyncio task (Textual worker). No threads except the PTY reader/writer (below).
- Agents emit `AgentEvent`s onto their own **event bus**; UI panes subscribe with **bounded fan-out queues**. On overflow the subscriber drops to a *resync*: reload the pane from the transcript store, then continue live. (QuickTerm's proven pattern for fast producers + slow consumers — never unbounded buffering, never a frozen UI.)
- Transcript widgets batch-apply queued events once per frame (~30fps); streaming text goes through Textual's markdown stream append, not full re-renders.
- Modal flows use `push_screen_wait`: the agent loop literally `await`s the user's decision — `decision = await app.ask_permission(request)` — clean backpressure, no callback soup.
- **Cancellation:** Esc cancels the focused agent's worker → aborts the in-flight HTTP stream, kills the PTY process tree, marks the partial turn `[interrupted]` in history.

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

## Multi-conversation & multi-agent runtime

- A **Conversation** = one main AgentInstance + its transcript + its spawned subagents. The app holds a registry; the **conversation switcher** (docs/UI.md) jumps between them; every conversation keeps running when unfocused (background activity badges).
- Subagents and teammates are just more AgentInstances with different system prompts, models, and permission caps — one runtime, no special cases. Coordination (task board, teammate messaging, result hand-back) is specced in docs/AGENTS.md.
- Session store: every event appends to `./.quickcode/sessions/<conv-id>.jsonl`; `--continue` / `--resume` rebuild conversations, including still-open task boards.

## Efficiency checklist

1. **Cache-stable prefix:** request order `tools → system → history`, byte-identical across turns. No timestamps/randomness in the system prompt; dynamic state travels as `<system-reminder>` blocks in user messages.
2. **Parallel tool calls** honored (gather) and encouraged in the prompt.
3. **Cheap models for fan-out:** research/search subagents default to a configured `worker_model` (sonnet-tier), orchestrator stays on the big model.
4. **Diff-based edits**; output caps + pagination hints on every tool; read-dedup (superseded file reads stubbed out of the request).
5. **Compaction at ~80%** of the model's context window; manual `/compact`.
6. **UI never blocks the loop, loop never blocks the UI** — bounded queues both directions.

## Keybinding discipline (QuickCode runs *inside* QuickTerm)

QuickTerm's UI layer claims Alt+K (palette), Alt+Z (zoom), Alt+W (close), Alt+arrows (focus), and the Alt+Shift split/font namespace — QuickCode must not fight it. Therefore QuickCode binds **Ctrl-based combos and function keys only** (exact map in docs/UI.md), and everything it doesn't claim passes through to the shell. Same philosophy QuickTerm applies to *its* host: claim cold keys only.

## Windows notes

- `bash` targets Git Bash when present, else PowerShell; the active shell is named in the tool description so the model writes matching syntax.
- Paths normalized to forward slashes in tool results; `rg` resolved from PATH with a bundled-download helper (`quickcode doctor` offers to fetch it).
