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
│  hooks.py (plan mode) · tasks.py · compact.py · context guard│
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
  cli.py                  # args, config, web app vs headless (-p) dispatch, `doctor`, `why`
  headless.py             # `-p` I/O: stdin prompt, console-safe output, exit codes, failure watch
  config.py               # profiles (base_url, model roles), project environment
  secrets.py              # API keys at rest: DPAPI on Windows, a 0600 file elsewhere
  doctor.py               # `quickcode doctor` environment checks
  permission_cli.py       # `qc why` / `quickcode permissions explain`: the permission dry run as text
  update.py               # the update check, download and verified install
  webapp.py               # uvicorn on a loopback port, single-instance hand-off, window vs browser
  subproc.py              # every child process starts here: no console window, no API keys in its env, killable tree
  fsutil.py               # atomic_write_text/bytes: temp file beside the target, renamed over it
  jsonfile.py             # the one JSON-file decoder: BOM names UTF-8/16/32, else strict UTF-8
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
    context_guard.py      # compaction between rounds; shrink and retry once when refused for length
    context_size.py       # request estimates (ledger + chars/4), cutting tool results to fit
    permissions.py        # modes, rules, PermissionSpec, bash decomposition
    profiles.py           # permission profiles: named {mode, allow, ask, deny} bundles
    permission_posture.py # the engine a new (or live) session would ask, built as open() builds it
    permission_explain.py # "why was I prompted?": the engine's own trace, as prose + rule provenance
    tasks.py              # task board
  hooks/                  # user command hooks on the LoopHook seam (docs/HOOKS.md)
    config.py protocol.py runner.py plugin.py events.py specs.py
    store.py trial.py     # the Hooks page: editing the settings files, test runs
  kernel/                 # the plugin kernel (below)
    spec.py registry.py bootstrap.py state.py
    manifest/             # the internal plugins we ship, one module per family
      core.py sections.py tools.py agents.py authored.py providers.py mcp.py _text.py
    core_settings.py      # the internals' declared settings and bounds, read by the runtime
    settings_file.py      # the one settings.json reader/writer; project writes keep trust
    composition.py        # what is attached to one agent, and the runtime limits
    resolve.py            # what an agent actually gets, with provenance
    orchestrator.py       # resolve_orchestrator: the session's own agent, depth 0
    patterns.py           # a tools:/spawns:/models: entry: literal name or glob
    preset.py             # presets: the composition a session's agents run
    problems.py           # provenance, problem records, and every problem code
    authoring/            # .quickcode/plugins/*.md → plugins
      format.py schema.py model.py discovery.py store.py
      argv.py reserved.py templates.py
  security/
    trust.py              # the project trust gate
    launch.py             # resolving and launching command/MCP executables safely (PATHEXT, .cmd/.bat)
  server/
    app.py                # create_app: loopback guard, security headers, route registration
    http.py               # bounded JSON bodies, id checks, `scoped` (one handler, both path shapes)
    ws.py                 # conversation WebSocket: attach, replay, heartbeat, client messages
    sessions_api.py       # bootstrap, sessions (rename, archive, delete, sweep), models
    projects_api.py       # project registry, data purge, directory browser, trust gate
    kernel_api.py         # plugin registry and settings, presets
    profiles_api.py prompt_api.py config_api.py update_api.py
    manager.py            # ConversationManager: opens and tracks one project's conversations
    conversation.py       # Conversation: one live agent, its windows, its turn worker
    reviews.py            # ReviewDesk: permission / plan requests awaiting a client decision
    projects.py           # ProjectHub, project registry
    serialization.py      # re-exports session/wire.py under its old import path
    agents_api.py authoring_api.py gitinfo.py paths.py terminal.py auth.py
    workbench/            # the agent workbench behind agents_api.py's routes
      inventory.py view.py drafts.py compositions.py resolution.py
      prompt_view.py tool_rows.py provenance.py
    hooks_api.py          # /api/hooks: list, add, change, remove, test-run
    permissions_api.py    # POST .../permissions/explain: a dry run of the permission gate
  session/
    store.py              # JSONL transcripts + conversation registry
    recorder.py           # TranscriptRecorder: what a session log contains
    wire.py               # AgentEvent → wire JSON, LOGGED_TYPES, register_event
    assemble.py           # build_session: the one way a session is put together, app and -p
  subagents/
    definitions.py runner.py jobs.py artifacts.py
  providers/
    base.py openai_compat.py credits.py
    overflow.py           # "context length exceeded", in each provider's words
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

- Each **AgentInstance** runs as an asyncio task. Threads appear in three places only: the reader/watcher threads of a PTY session and of a background shell job (below), the worker thread a blocking call runs on (`asyncio.to_thread`: a PTY command, `taskkill`), and the server thread when the native window owns the main one. A command on plain pipes, a hook, an authored command tool and an MCP server are asyncio subprocesses (`subproc.spawn_async`).
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
        compacted = await before_request(agent)   # context guard (below)
        if round_no == max_rounds:
            agent.history.push_user("", [wrap_up_reminder])
        msg = await _stream_once(agent, compacted=compacted)  # streams, emits, assembles;
                                                  # refused for length: shrink, retry once
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
- **Loop guard:** `runtime.agent_loop.max_rounds` tool rounds per turn, then a system reminder to wrap up. 50 is the default (`RuntimeLimits.max_rounds` in `kernel/composition.py`, declared as a setting in `kernel/core_settings.py`), not a constant — it is resolved per session by `kernel/resolve.runtime_limits` and frozen for the turn, so editing the setting mid-turn cannot move the budget under a turn already counting.
- **The loop knows no tool by name.** Which tools are offered is decided by hooks (`visible_tools`), a hook may answer a call itself (`intercept`, which is how plan review works), and how a call is gated comes from the tool's own `PermissionSpec`. Plan mode used to be an `if` in this file; it is now `PlanModeHook` in `core/hooks.py`.

### Context guard

Compaction between turns (the web worker, `TranscriptRecorder.record_turn`)
cannot help a turn whose own tool results outgrow the window: the provider
refuses the next request with "context length exceeded", and `/compact`, whose
request carries the same history, used to overflow as well.
`core/context_guard.py` works inside the turn, at two points of `run_turn`:

- **Before every request.** The request is estimated as the ledger's last
  measured one (`last_input_tokens + last_output_tokens`: that request plus the
  reply now at the end of history) plus chars/4 of everything appended after
  that reply (`core/context_size.py::request_estimate`). When it crosses
  `runtime.compaction.threshold` of the window, the history is compacted there
  and then — between rounds, after a round's results were pushed, so the same
  `_select_tail` cut applies and no call is parted from its result — and the
  turn continues. Only a *measured* estimate triggers it: right after a
  compaction nothing is measured until the next request comes back, so the
  guard never compacts two requests in a row.
- **When the provider refuses anyway.** `providers/overflow.py` recognises the
  refusal in OpenAI-compatible, OpenRouter, Anthropic, vLLM, llama.cpp, Mistral
  and Gemini wording, raised as a `ProviderError` or reported as a `TurnDone`
  error, and takes the window from it when the message names one — which is
  how a subagent on an uncatalogued model, or `-p` before its catalog arrives,
  learns its own. If nothing of the round was shown yet, the error is held
  back; the history's largest tool results are cut to head + tail under a
  `<truncated … hint="middle cut to fit the context window; …"/>` marker, all
  to one cap (the highest that frees enough, so a single giant result is all
  that goes when it alone is the problem); the history is compacted if cutting
  could not free enough, unless the guard already compacted ahead of this very
  request; and the request is sent once more. A second refusal surfaces the
  way any provider error does. A refusal the retry recovered from leaves no
  `error` in the log and is not a failure to `-p`.

The summary request has to fit as well (`core/compact.py::_fit_for_summary`):
before it is sent, the oldest tool results in it are cut first, then all of
them to one cap, and if the history is still too long its oldest rounds are
left out whole (the seed of an earlier compaction stays). Only the request is
cut. Refused for length anyway, it is fitted once more, with a wider margin, to
the window the refusal names.

A compaction mid-turn is the between-turn one in every other respect: the same
`compacted` record (widened with `"mid_turn": true`), the rebuilt history
written to the log as a `compaction` record so a resume loads it, the ledger's
context footprint reset. The core emits it as a `Compacted` event carrying the
rebuilt history, and the recorder does the bookkeeping, so the web worker and
`-p` get it without code of their own. The post-compaction reminder (with the
mode, which the summary may have lost) cannot wait for the next user message,
so it is pushed at once as a reminder-only user message, the way the wrap-up
reminder is. The system prompt is not touched.

`runtime.compaction.enabled: false` turns off the first point and the
compacting half of the second; cutting tool results and retrying stays on,
because without it the conversation cannot take another request at all.
Subagents run the same loop and so the same guard: `SubagentDeps.context_window`
hands each child the catalog's window for its own model (the spawner's, when it
runs the same model). A cut rewrites the message in memory; a result persisted
in an earlier turn keeps its full text on disk, so a resumed session that
overflows is cut again by the same path.

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
- Either way the command is started through `quickcode/subproc.py`, like every child process: its environment is QuickCode's minus the app's API keys (and, in a frozen build, minus PyInstaller's loader path), its stdin on the pipe path is the null device, and it leads a process group of its own so Stop and timeouts kill everything it started (`subproc.kill_tree`).

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
- Session store: the trace appends to `./.quickcode/sessions/<conv-id>.jsonl`. Not *every* event — `session/wire.py` holds a `LOGGED_TYPES` set and `loggable()` admits only the assembled shapes (`user_message`, `assistant_message`, `system_prompt`, `context_injection`, `tool_call`, `tool_result`, `usage`, `permission_request`, `permission_resolved`, `plan_request`, `plan_resolved`, `mode_changed`, `model_changed`, `compacted`, `agent_spawned`, `agent_done`, `bash_job_started`, `bash_job_done`, `hook_run`, `system_note`, `error`) — `hook_run` is registered by `hooks/events.py` through `register_event(..., logged=True)`. Two more are logged by their emitter passing `log_it=True`: `profile_changed` and `composition_changed`. Streaming deltas and transient status flips stay live-only, which is why the log replays as a transcript rather than as a keystroke recording. A subagent's assembled events (its tool calls, its results, its usage, its mid-turn compactions and system notes, its final message) are logged the same way, one level down inside an `agent_event` wrapper carrying the child's id and the spawning turn. A plugin can add one more type via `register_event(..., logged=True)`. `--continue` resumes the most recent conversation, including its still-open task board; any other one is reopened from the session list in the UI.

## Headless runs (`-p`)

`quickcode -p` runs one turn on the session the app would open on the same
project. `ConversationManager.open()` and `cli._build_agent()` both call
`session/assemble.py::build_session`, which is the only place a session is put
together: the store (and the resume, when `--continue` names one), the task
board, the preset (a resumed session keeps the one it started with), the
session pool (`kernel/resolve.session_pool`: switched-off plugins removed, the
project's authored command tools added), the composition
(`kernel/orchestrator.resolve_orchestrator`, or the one the session recorded),
the runtime limits, the starting mode and rules, the tool registry and the
permission engine, the prompt rendered from the composition's section bodies,
the command hooks and the `AgentInstance` with the user's generation settings.
`Session.wire` then adds the tables the agent shares with its subagents — the
background shell jobs and the `agent` tool's deps, which carry the pool, the
parent composition, the definitions snapshot and the preset — and
`Session.begin_log` queues the opening `meta` record with the preset and the
composition, so `--continue` resumes on the composition the run started with.

The starting mode is `--mode`, else the active permission profile's, else the
composition's `default_mode`, else the `runtime.permissions` setting, capped at
the composition's ceiling. Yolo that nothing armed (`--yolo`, `allow_yolo`)
starts in ask instead and says so: `--mode yolo` alone is an argument error in
`-p`, anything else is a note on stderr there and a system note in the app.

Before this was one path, `-p` ran the unfiltered default registry — a disabled
plugin, an authored tool and the whole composition, including its spawn list
and model allow-lists, meant nothing there — rendered its prompt from other
inputs, named the backend differently, ignored the user's `max_tokens` and
`temperature`, recorded no composition, and resolved its subagents against no
pool, parent or definitions.

What still differs is what drives the session, not what it is:

- **Tools in the pool.** The app adds entry-point plugin tools and the
  project's MCP servers (`server/projects.py`); `-p` starts neither, so its pool
  is the built-ins plus authored command tools.
- **Prompts and plan review.** The app answers them over the WebSocket; `-p`
  refuses every permission prompt (`docs/PERMISSIONS.md#headless-mode`) and
  records a plan without review. The prompt gains `<headless_mode>`.
- **Detached work.** Nothing outlives the one turn, so a `background: true`
  delegation runs inline and `_run_headless` kills any shell job left running.
- **Context window.** The app reads it off the catalog; `-p` fetches it
  alongside the turn rather than in front of it.

## Efficiency checklist

1. **Cache-stable prefix:** request order `tools → system → history`, byte-identical across turns. No timestamps/randomness in the system prompt; dynamic state travels as `<system-reminder>` blocks in user messages.
2. **Parallel tool calls** honored (gather) and encouraged in the prompt.
3. **Cheap models for fan-out:** both built-in subagent types (`explore`, `general`) default to the profile's `worker` model role; the orchestrator stays on its own model.
4. **Diff-based edits**; output caps + pagination hints on every tool; read-dedup (superseded file reads stubbed out of the request).
5. **Compaction at ~80%** of the model's context window; manual `/compact`. Both drivers check it after every turn — the web worker and `TranscriptRecorder.record_turn`, which is what a headless `-p` run goes through — off the one declared setting (`runtime.compaction`), and the loop checks it before every request inside a turn (§Context guard above), where a refusal for length is also answered by cutting tool results and one retry.
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
