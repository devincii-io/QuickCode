# QuickCode

A local-first coding agent with a **traceable web UI** — in the spirit of
Claude Code and DeepSeek Harness. Python core, no terminal library: the CLI
starts a loopback FastAPI server and opens a browser app. Models are reached
through a **pluggable provider layer** (OpenRouter by default, any
OpenAI-compatible endpoint by config), and the agent's capabilities — tools,
providers, MCP servers — are plugins; the UI is built in.

**Every run is traceable.** Everything the model sees is recorded in an
append-only session event log: the system prompt, context injections, tool
calls and results, subagent activity, permission decisions. The **Trajectory**
view renders that log as an inspectable table (role chips, timeline strip,
search, per-event Summary/Payload/Result/Timing inspector) and switches
seamlessly with **Chat** — or opens beside it in **Split** view. Resume and
replay operate on the same event stream.

## Status

Complete rewrite of the former Textual TUI (v2). Persistent, permission-gated
agent with streaming, plan review, tasks, compaction, concurrent subagent
fan-out, usage tracking, session resume, and the trajectory inspector.

### Quickstart

```bash
uv venv --python 3.12
uv pip install -e ".[dev,pty]"
export QUICKCODE_OPENROUTER_API_KEY=sk-...  # or save it in Settings (encrypted)
uv run quickcode                        # start the web app  (qc also works)
qc .                                    # open the app on this directory
qc C:\proj "fix the build"              # open a project, with a first prompt
uv run quickcode --no-browser           # print the URL instead of opening it
uv run quickcode -p "explain this repo" # headless / print mode
```

One running app hosts many projects (like editor windows): the launch
directory is the default project, and further directories are opened on demand
through `/api/projects/open`. Recently-opened projects are remembered in
`~/.quickcode/projects.json`.

In the app: `Enter` send · `Shift+Enter` newline · `Esc` interrupt · mode and
model pickers live on the composer · `⚙` Settings has General / Models /
Plugins · messages sent while the agent is busy are queued. Tests:
`uv run pytest -q`.

### Plugins (agent capabilities)

- **Tools** — Python entry point group `quickcode.tools` returning `Tool`
  instances.
- **Providers** — entry point group `quickcode.providers`; select per profile
  via `"provider"` in `~/.quickcode/config.json`.
- **MCP servers** — Claude-compatible `"mcpServers"` config in
  `.quickcode/settings.json` (project) or `~/.quickcode/settings.json` (user);
  stdio transport, tools appear as `mcp__<server>__<tool>` behind the same
  permission gate.

### Installation

QuickCode 1.0 installs three ways. All of them give you the same local web app.

**Windows installer (`.exe`)** — the turnkey path. Build
`packaging\quickcode.iss` with the [Inno Setup](https://jrsoftware.org/isinfo.php)
compiler, then run the resulting `QuickCode-Setup-1.0.0.exe`. It installs
per-user, ensures Git and Python 3.12+ (installing them silently if missing),
creates a private venv, puts `quickcode`/`qc` on your `PATH`, and adds a Start
Menu shortcut — plus optional desktop and *"Open QuickCode here"* folder
context-menu entries. See [packaging/README.md](packaging/README.md).

**pip / uv** — for anyone who already has Python 3.12+:

```bash
uv pip install quickcode        # or: pip install quickcode
```

**From source** — see the Quickstart above, or run
`powershell -ExecutionPolicy Bypass -File scripts\install.ps1` from a checkout
to get a `.venv` (or `-UsePipx` for a global install).

Then, from any terminal:

```bash
quickcode        # start the app on the current directory (qc works too)
qc .             # same thing, explicitly
qc C:\proj       # open another project
```

The installer's **QuickCode** shortcut runs `quickcode-app`, a windowed entry
point that opens your home directory as the default project with no console
window behind the browser.

QuickCode's mark is a friendly blue ghost — it's the Start Menu icon, the
browser favicon, and the app's own brand mark
([`quickcode/frontend/assets/icon.svg`](quickcode/frontend/assets/icon.svg)).

### Design docs

The full plan lives in `docs/`:

| Doc | Contents |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Layers, async agent loop, provider abstraction, PTY subsystem, efficiency techniques |
| [docs/UI.md](docs/UI.md) | The web UI: chat/trajectory/split views, event log protocol, modals (partly historical — describes the retired TUI) |
| [docs/AGENTS.md](docs/AGENTS.md) | Subagents, teammate mode, task board, orchestration playbook |
| [docs/PERMISSIONS.md](docs/PERMISSIONS.md) | Permission modes (plan → yolo), rules engine, plan mode, bypass guardrails |
| [docs/PROMPTS.md](docs/PROMPTS.md) | System prompt (XML-sectioned), dynamic reminders, compaction + delegation prompts |
| [docs/TOOLS.md](docs/TOOLS.md) | Tool surface: schemas, description copy, safety rules |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Milestones M0–M6 |

## Principles

1. **Efficiency is architecture, not magic** — cache-stable prompt prefixes, parallel tool execution, diff-based edits, hard output truncation, compaction before overflow.
2. **The harness owns safety** — the model emits tool calls; the permission layer decides what runs.
3. **Provider-agnostic core** — the agent loop speaks a normalized event stream; adapters translate.
4. **Every run is traceable** — the append-only event log is the source of truth; the trajectory view shows everything the model saw, and replay/resume derive from the same stream.
5. **Agent capabilities are plugins; the UI is not** — tools, providers, and MCP servers are swappable, the built-in web UI stays coherent.
