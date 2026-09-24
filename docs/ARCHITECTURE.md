# Architecture

## Stack

- **Runtime:** Python 3.12+, `uv` for env/packaging, `quickcode` console script (+ `qc` alias, + the windowed `quickcode-app`)
- **Server:** FastAPI + uvicorn on 127.0.0.1, WebSocket for the live event stream
- **UI:** vanilla ES modules, no bundler and no build step, served as static files (see docs/UI.md)
- **Window:** pywebview (WebView2 on Windows) — a native app window, not a browser tab; the default browser when pywebview is unavailable
- **Wire clients:** `openai` package, `AsyncOpenAI(base_url=...)` — one client class, many backends (OpenRouter default); plain `httpx` for the native Anthropic Messages API
- **Schemas:** Pydantic models → strict JSON Schema for tools
- **Search:** ripgrep (`rg` on PATH; pure-Python fallback so nothing breaks without it)
- **PTY:** the POSIX `pty` module for `bash` commands off Windows; `pywinpty` (ConPTY) for the terminal panel on Windows — patterns lifted from QuickTerm (see below)

## Layer diagram

```
┌──────────────────────────────────────────────────────────────┐
│ Native window (pywebview) → frontend/ (ES modules)           │
│  workspace shell → one iframe per agent pane                 │
│  chat · trajectory · agents/tasks/files/usage · terminal     │
│  configuration · help                        (see docs/UI.md)│
└───────────▲──────────────────────────┬───────────────────────┘
            │ WebSocket events         │ input / approvals / steering
┌───────────┴──────────────────────────▼───────────────────────┐
│ Server (FastAPI)                                             │
│  ProjectHub → ConversationManager → Conversation             │
│  REST: bootstrap, sessions, models, kernel, trust, authoring │
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
│ (OpenRouter,  │   │  bash · web_fetch/web_search · task_*    │
│  OpenAI,      │   │  agent · plan · PermissionSpec → gating  │
│  Ollama, …)   │   │  + entry-point, authored and MCP tools   │
└───────────────┘   └──────────────────────────────────────────┘
┌──────────────────────────────────────────────────────────────┐
│ Plugin kernel: what exists, and what may be changed          │
│  spec · registry · manifest · composition · resolve · state  │
└──────────────────────────────────────────────────────────────┘
┌──────────────────────────────────────────────────────────────┐
│ Persistence: config.py · session store (JSONL) · task board  │
└──────────────────────────────────────────────────────────────┘
```

## Repo layout

Every module under `quickcode/`, one line each. `tests/test_docs_accuracy.py`
checks that every path here exists and that every package is listed.

```
pyproject.toml            # [project.scripts] quickcode = "quickcode.cli:main"
quickcode/
  cli.py                  # args, config, web app vs headless (-p) dispatch, `quickcode doctor`
  headless.py             # `-p` I/O: stdin prompt, console-safe output, exit codes, failure watch
  config.py               # profiles (base_url, model roles), project environment
  secrets.py              # API keys at rest: DPAPI on Windows, a 0600 file elsewhere
  doctor.py               # `quickcode doctor` environment checks
  update.py               # the update check, download and verified install
  webapp.py               # uvicorn on a loopback port, single-instance hand-off, window vs browser
  subproc.py              # every subprocess goes through here (no console window on Windows)
  workspace.py            # the project's .quickcode/ directory and its .gitignore
  frontmatter.py          # the one frontmatter parser: plugin loader and trust gate read files the same way
  ui/window.py            # pywebview window, browser fallback
  frontend/               # index.html, css/, js/, assets/  (see docs/UI.md)
  context/toon.py         # TOON, the table encoding structured tool results use
  core/
    agent.py              # AgentInstance: loop + history + ledger + event bus
    loop.py               # the agentic loop (single turn driver)
    hooks.py              # LoopHook: tool visibility, interception, tighten-only gating
    events.py             # AgentEvent dataclasses (internal protocol)
    history.py            # messages, read-dedup, cache breakpoints
    compact.py            # threshold + summarization turn
    permissions.py        # modes, rules, PermissionSpec, bash decomposition
    profiles.py           # permission profiles: named {mode, allow, ask, deny} bundles
    tasks.py              # task board
  hooks/                  # user command hooks on the LoopHook seam (docs/HOOKS.md)
    config.py protocol.py runner.py plugin.py events.py specs.py
    store.py trial.py     # the Hooks page: editing the settings files, test runs
  kernel/                 # the plugin kernel (below)
    spec.py registry.py manifest.py bootstrap.py state.py
    composition.py        # what is attached to one agent, and the runtime limits
    resolve.py            # what an agent actually gets, with provenance
    preset.py             # presets: the composition a session's agents run
    problems.py           # provenance and problem records
    authoring/            # .quickcode/plugins/*.md → plugins
      format.py schema.py model.py discovery.py store.py
      argv.py reserved.py templates.py
  security/
    trust.py              # the project trust gate
    launch.py             # resolving and launching command/MCP executables safely (PATHEXT, .cmd/.bat)
  server/
    app.py                # FastAPI routes + WebSocket attach
    manager.py            # ConversationManager / Conversation
    projects.py           # ProjectHub, project registry
    serialization.py      # AgentEvent → wire JSON, LOGGED_TYPES
    agents_api.py authoring_api.py gitinfo.py paths.py terminal.py auth.py
    hooks_api.py          # /api/hooks: list, add, change, remove, test-run
  session/
    store.py              # JSONL transcripts + conversation registry
    recorder.py           # TranscriptRecorder: what a session log contains
  subagents/
    definitions.py runner.py jobs.py artifacts.py
  providers/
    base.py openai_compat.py credits.py
    choice.py             # built-in providers' defaults, switching between them
    anthropic/            # native Messages API: wire, sse, stream, retry, models
  tools/
    base.py registry.py command.py
    read.py write.py edit.py glob.py grep.py bash.py
    bash_jobs.py bash_job_tools.py  # background shell jobs, bash_output / bash_kill
    fs/                   # textfile.py (encodings, line endings, staleness), walk.py, patterns.py
    web_fetch.py web_search.py
    agent.py agent_jobs.py send_message.py task.py plan.py
  web/
    fetch.py ssrf.py markdown.py
  search/                 # web_search providers
    base.py resolve.py brave.py serper.py tavily.py searxng.py exa.py google_cse.py
    __main__.py           # python -m quickcode.search (set-key, list, status)
  plugins/
    loader.py             # quickcode.tools / quickcode.providers entry points
    mcp.py                # MCP client + tool adapter
  prompts/
    sections.py           # the system prompt, one section per block
    system.py compact.py subagent.py
  pty/
    session.py            # one PTY per bash command (QuickTerm patterns)
    interactive.py        # the terminal panel's long-lived shell
    registry.py           # live terminals, per project
    shells.py flow.py teardown.py  # shell choice, bounded input queue, session teardown
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

A **preset** (shown in the UI as a *composition*) is the plugin composition one
session runs — its tools, its subagents, its prompt, its default mode. A
session records the composition it started with and keeps it on resume: the
conversation was already told what tools it had. Switching one mid-session is
an explicit act (`/composition`), refused while a turn is running, re-renders
the system prompt, and is logged as `composition_changed` so the trajectory
shows that the conversation had two different agents in it.

## Async model

- Each **AgentInstance** runs as an asyncio task. Threads appear in three places only: the reader/watcher threads of a PTY session (below), the worker thread a blocking subprocess runs on (`asyncio.to_thread`), and the server thread when the native window owns the main one.
- Agents emit `AgentEvent`s onto their own **event bus**; each attached WebSocket subscribes with a **bounded queue**. On overflow the client is dropped with a sentinel and reconnects, replaying from the log. (QuickTerm's pattern for fast producers + slow consumers — never unbounded buffering, never a frozen UI.)
- The frontend batches bursts with `requestAnimationFrame`; streaming text patches one live node rather than re-rendering the transcript.
- Permission and plan review round-trip over the WebSocket: the loop `await`s an `asyncio.Future` that a `permission_decision` / `plan_decision` message resolves — clean backpressure, no callback soup.
- **Cancellation:** interrupt cancels the agent's task → aborts the in-flight HTTP stream, kills the running command's process tree, and closes the round with `[interrupted]` in history.

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
- **Consecutive read-only tools run concurrently** (`asyncio.gather`); any other call is a barrier that runs alone, in call order — so a `read` issued after a `write` in the same response sees the write.
- **Failed tools still return a result** with `is_error: true` so the model can recover.
- **Loop guard:** `runtime.agent_loop.max_rounds` tool rounds per turn, then a system reminder to wrap up. 50 is the default (`RuntimeLimits.max_rounds` in `kernel/composition.py`, declared as a setting in `kernel/manifest.py`), not a constant — it is resolved per session by `kernel/resolve.runtime_limits` and frozen for the turn, so editing the setting mid-turn cannot move the budget under a turn already counting.
- **The loop knows no tool by name.** Which tools are offered is decided by hooks (`visible_tools`), a hook may answer a call itself (`intercept`, which is how plan review works), and how a call is gated comes from the tool's own `PermissionSpec`. Plan mode used to be an `if` in this file; it is now `PlanModeHook` in `core/hooks.py`.

## Provider layer

The core only speaks this (`providers/base.py`); adapters translate wire formats:

```python
class Provider(Protocol):
    def stream_chat(self, req: ChatRequest) -> AsyncIterator[AgentEvent]: ...
    async def list_models(self) -> list[ModelInfo]: ...

AgentEvent = (
    TextDelta | ReasoningDelta
    | ReasoningBlock  # opaque signed block, replayed next request; never shown
    | ToolCallStart | ToolCallDelta | ToolCallEnd
    | Usage        # input/output/cached/cache-write tokens, cost → ledger + status bar
    | TurnDone     # finish_reason: stop | tool_calls | length | error
)
```

Cancellation is not a parameter: the stream is consumed inside the agent's
task, and cancelling that task is what aborts the request.

### `openai_compat` (default)

- `base_url` from the active profile; default `https://openrouter.ai/api/v1`, key from `QUICKCODE_OPENROUTER_API_KEY` or the encrypted value saved from Settings (`secrets.py`). Any OpenAI-compatible endpoint works (OpenAI, Groq, Ollama at `localhost:11434/v1`, …).
- Streaming chat completions, OpenAI-style `tools`, buffered `tool_calls` argument deltas.
- Usage in-stream (`stream_options.include_usage`, plus OpenRouter's `usage: {include: true}`) feeds the ledger. The model list comes from `GET /models` and is not filtered: the picker shows the whole catalog with a search box and a custom-id entry.
- `reasoning` param passthrough (OpenRouter normalizes effort across vendors); deltas surface as `ReasoningDelta`.
- Prompt caching: `cache_control` breakpoints on the system message and the last history block — forwarded to Anthropic models by OpenRouter; OpenAI-family caches automatically; harmless elsewhere.
- **Per-agent model choice:** every AgentInstance carries its own model — expensive orchestrator, cheap workers (see docs/AGENTS.md).

Third-party providers are selected per profile through the `quickcode.providers`
entry-point group (`plugins/loader.py`).

### `anthropic` (native Messages API)

Selected with `"provider": "anthropic"` in the profile, or the provider select in
Settings → General. Plain `httpx`, no SDK; `providers/anthropic/` splits it into
request building (`wire.py`), SSE decoding (`sse.py`), event translation
(`stream.py`), retry policy (`retry.py`) and model knowledge (`models.py`).

- **Endpoint and key.** `https://api.anthropic.com` unless the profile names
  another host (a gateway); a profile still pointing at openrouter.ai falls back
  to the first-party endpoint so the Anthropic key is never sent there. Its own
  key: `QUICKCODE_ANTHROPIC_API_KEY`, else `~/.quickcode/anthropic.key` saved
  from Settings through the same encrypted store as every other key.
- **Models.** Defaults `claude-opus-5-5` (orchestrator) and `claude-sonnet-5`
  (worker) on a switch. OpenRouter slugs (`anthropic/claude-opus-4.8`) are
  translated to Messages API ids (`claude-opus-4-8`), so existing profiles and
  agent definitions keep working; another vendor's slug fails before a request.
  The picker is filled from `GET /v1/models`, whose `capabilities` also decide
  how thinking is configured.
- **Thinking.** Current models run adaptive thinking with summarized display
  (surfaced as `ReasoningDelta`) and never receive sampling parameters; models
  that predate it get a `budget_tokens` budget only when reasoning is asked for.
  Each finished block, with its signature, is handed to the loop as a
  `ReasoningBlock`, stored on the assistant message (`reasoning_blocks`, also in
  the session file) and replayed verbatim — a tool-use turn replayed without its
  thinking is refused. A signature binds a block to the history before it, so
  `History` drops them where it rewrites itself (a compaction, a new system
  prompt); if the API still refuses one (a resumed session), the request is
  resent without reasoning and the adapter never sends those blocks again.
- **Prompt caching.** Two explicit breakpoints: the system prompt (caching tools
  + system; byte-stable within a session) and the conversation tail, which
  `History.build_messages` moves forward every round so a tool loop reads the
  whole prior prefix. Request bodies are serialized deterministically.
- **Usage.** `input_tokens` is the whole prompt (uncached + cache writes + cache
  reads), `cached_tokens` the reads, `cache_write_tokens` the writes; the cost
  is computed from first-party prices (writes 1.25× input, reads at the model's
  read rate). A model with no known price reports no cost rather than a guess.
- **Errors.** 408/409/429/5xx/529, dropped connections and mid-stream
  `overloaded_error` are retried with backoff that honours `Retry-After`, but
  only while nothing has reached the caller; after that the error surfaces with
  the API's own type, message and request id. A `refusal` stop ends the round as
  an error.
- **Compaction.** The summary request declares no tools, which the API refuses
  alongside tool blocks, so that one request carries the tool history as text.

## Permission system

Full design in docs/PERMISSIONS.md; core model:

| Mode | Read-only | Edits | Bash/mutating | Notes |
|---|---|---|---|---|
| `plan` | ✅ auto | ❌ blocked | ❌ blocked | research only; exits via plan approval |
| `ask` (default) | ✅ auto | prompt | prompt | |
| `auto-edit` | ✅ auto | ✅ auto | prompt | edits only; no file-op command allowlist |
| `dontask` | ✅ auto | rule-matched, else auto-deny | rule-matched, else auto-deny | never prompts |
| `yolo` | ✅ auto | ✅ auto | ✅ auto | explicit opt-in; the mode pill turns red |

`Mode` has these five members and no others. A four-mode summary that omits
`dontask` used to sit here, which is how the one mode that silently *denies*
went undocumented in the architecture overview.

- Prompt choices: **allow once · always allow (persist rule) · deny with message** (deny text returns as the tool result so the model adapts).
- Rules persist in `./.quickcode/settings.local.json` — "always allow" writes there; `./.quickcode/settings.json` is the shared, checked-in half (`allow`/`deny`/`ask` arrays, `bash(npm test*)`-style patterns). Deny beats allow.
- A compound line is **split** on `;`, `&&`, `||`, `|` and `&`, and each subcommand is rule-matched on its own — that is the "parse, don't prefix-match" principle, and it means a rule whose pattern spans a splitter can never match. Substitution and redirection are the different case: a line containing `$(`, a backtick, `>` or `<` never matches an allow rule at all and never takes the read-only auto-allow — full-string deny rule or prompt.
- The mode is chosen from the mode pill or `/mode`, per conversation; there is no cycling hotkey. Subagents inherit a *capped* mode (a yolo main agent does not imply yolo workers — see docs/AGENTS.md).
- Edits outside the project root always prompt (except in `yolo`).

## The bash tool and PTYs (QuickTerm lessons, applied)

`tools/bash.py` runs one command per call and returns when it exits. How it
runs depends on the platform:

- **POSIX:** inside a real pseudo-terminal (`pty/session.py`), so programs see a tty and take their tty code paths.
- **Windows:** on plain pipes by default. Under a tty a command that reads stdin (`git commit` without `-m`, `ssh`, a pager) waits for a person who is not there; under a pipe it gets EOF and exits. `QUICKCODE_BASH_PTY=1` opts back into ConPTY.
- Any PTY failure (backend missing, spawn error) falls back to the plain subprocess path, which is the same code either way.

Patterns carried over from QuickTerm:

- **Reader and watcher threads per PTY session:** the reader coalesces available output (64 KB reads) into the scrollback; the watcher waits on the real process rather than the PTY's EOF, which on ConPTY lags seconds behind the actual exit.
- **Bytes on the hot path**, decoded once at the boundary by `tools/base.decode_output`: UTF-8 first, then the system code page, never `surrogateescape` — a lone surrogate used to kill the turn inside the recorder. Both paths then strip ANSI escapes and apply carriage returns the way a terminal would.
- **Scrollback ring as a deque of chunks** (O(chunk) trim), capped at 16 MB.
- **Process-tree kill** on Stop and on timeout (`taskkill /T /F` on Windows, the process group on POSIX).
- Output to the model stays capped (30k chars, head + tail) with a truncation marker.

The **terminal panel** is a separate thing: `pty/interactive.py` holds one
long-lived shell per project for the *human*, served by `server/terminal.py`.
No tool can reach it, and the agent never sees what is typed there.

## Multi-project, multi-conversation, multi-agent runtime

- A **ProjectHub** holds one `ConversationManager` per open project; a project id is a stable hash of its resolved path, so it is the same id every run.
- A **Conversation** = one main AgentInstance + its transcript + its spawned subagents. A conversation nobody is attached to stays *open* server-side — its agent, task board and background jobs survive — but nothing streams to a client that is not there. In the browser each agent pane is its own iframe holding exactly one socket to one conversation (`frontend/js/ws.js` enforces the one with a generation guard); several panes make several concurrent live conversations.
- Subagents and teammates are just more AgentInstances with different system prompts, models, and permission caps — one runtime, no special cases. Coordination (task board, teammate messaging, result hand-back) is specced in docs/AGENTS.md.
- **Spend vs. context.** Each AgentInstance owns a `Ledger`, so a child's tokens reach the session only through the recorder, which bridges every subagent bus. It rolls them in with `Ledger.add_subagent`: the cumulative fields (`input_tokens`, `output_tokens`, `cached_tokens`, `cost_usd`) take them, and `last_input_tokens` / `last_output_tokens` never do. That pair is the *live context footprint* — it drives `context_pct()`, the context meter and the compaction threshold — and a subagent fills a context window of its own, so counting its request there would show a short conversation as nearly full and could trip an auto-compaction the parent never needed. `Ledger.from_events` replays the same split from the log, reading the child's usage out of the `agent_event` wrapper it is logged inside.
- Session store: the trace appends to `./.quickcode/sessions/<conv-id>.jsonl`. Not *every* event — `server/serialization.py` holds a `LOGGED_TYPES` set and `loggable()` admits only the assembled shapes (`user_message`, `assistant_message`, `system_prompt`, `context_injection`, `tool_call`, `tool_result`, `usage`, `permission_request`, `permission_resolved`, `plan_request`, `plan_resolved`, `mode_changed`, `model_changed`, `compacted`, `agent_spawned`, `agent_done`, `bash_job_started`, `bash_job_done`, `hook_run`, `system_note`, `error`) — `hook_run` is registered by `hooks/events.py` through `register_event(..., logged=True)`. Two more are logged by their emitter passing `log_it=True`: `profile_changed` and `composition_changed`. Streaming deltas and transient status flips stay live-only, which is why the log replays as a transcript rather than as a keystroke recording. A subagent's assembled events (its tool calls, its results, its usage, its final message) are logged the same way, one level down inside an `agent_event` wrapper carrying the child's id and the spawning turn. A plugin can add one more type via `register_event(..., logged=True)`. `--continue` resumes the most recent conversation, including its still-open task board; any other one is reopened from the session list in the UI.

## Efficiency checklist

1. **Cache-stable prefix:** request order `tools → system → history`, byte-identical across turns. No timestamps/randomness in the system prompt; dynamic state travels as `<system-reminder>` blocks in user messages.
2. **Parallel tool calls** honored (gather) and encouraged in the prompt.
3. **Cheap models for fan-out:** both built-in subagent types (`explore`, `general`) default to the profile's `worker` model role; the orchestrator stays on its own model.
4. **Diff-based edits**; output caps + pagination hints on every tool; read-dedup (superseded file reads stubbed out of the request).
5. **Compaction at ~80%** of the model's context window; manual `/compact`. Both drivers check it after every turn — the web worker and `TranscriptRecorder.record_turn`, which is what a headless `-p` run goes through — off the one declared setting (`runtime.compaction`).
6. **UI never blocks the loop, loop never blocks the UI** — bounded queues both directions.

## Trust boundary

The API answers the QuickCode window and nothing else: a Host allowlist
defeats DNS rebinding, an Origin allowlist defeats cross-origin requests from
other pages, and a per-install loopback token (`server/auth.py`) stops other
local processes. The token reaches the frontend in the URL fragment, which is
never sent to the server and never logged; WebSockets carry it as a
`qcauth.<token>` subprotocol. Static frontend files carry no secrets and stay
open so the shell can bootstrap.

## Windows notes

- `bash` targets Git Bash when present, else PowerShell; the active shell is named in the `<environment>` block of the system prompt so the model writes matching syntax.
- `glob` and `grep` report paths with forward slashes. `rg` is found on PATH; `quickcode doctor` reports whether it is there (it is optional — the pure-Python search is the fallback).
