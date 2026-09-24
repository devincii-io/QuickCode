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

Version 2.7.0. A persistent, permission-gated agent with streaming, plan
review, a task board, compaction, concurrent and background subagents, web
fetch and search, usage tracking, session resume, the trajectory inspector, a
terminal panel, and project workspaces with split agent panes. The web UI
replaced the original Textual TUI in 1.0.0. What is shipped, in progress and
planned is in [docs/ROADMAP.md](docs/ROADMAP.md).

### Quickstart

```bash
uv venv --python 3.12
uv sync --all-extras --dev
export QUICKCODE_OPENROUTER_API_KEY=sk-...  # or save it in Settings (see below)
uv run quickcode                        # start the web app  (qc also works)
uv run qc .                             # open the app on this directory
uv run qc C:\proj "fix the build"       # open a project, with a first prompt
uv run quickcode --no-browser           # print the URL instead of opening it
uv run quickcode -p "explain this repo" # headless / print mode
```

One running app hosts many project workspaces. Open a folder in the sidebar,
then use New agent to work with independent conversations side by side. Split
right or below, drag pane headers to rearrange them, resize the dividers, and
maximize a pane when it needs the space. Switching workspaces keeps mounted
agents connected. Layouts restore on this device; session history remains in
the project's existing event logs.

`Alt+N` opens an agent, `Alt+Z` maximizes or restores it, `Alt+B` toggles the
sidebar, and `Alt+arrow keys` focus another pane. Closing a pane keeps the
conversation and lets its agent continue; use Stop first to interrupt it.
Reopen closed pane restores the most recently closed view. Unsent drafts
survive pane closure and reload within the same browser tab.

Appearance controls text size, spacing, conversation width, metrics, and
animation. Settings offers provider defaults, themes, agents, tools, and
permissions. Theme changes apply across open panes. Workspace names, sidebar
width, pane positions, and split ratios are saved locally.

In the app: `Enter` send · `Shift+Enter` newline · `Esc` interrupt · mode,
model and composition pickers live on the composer · `⚙` opens
**Configuration**, with application settings first, followed by agents,
compositions, permission profiles, and parts (tools, prompt sections, models,
MCP servers, policies) · messages sent while the agent is busy are queued.
The full keyboard reference is in [docs/UI.md](docs/UI.md#keyboard).

### Plugins (agent capabilities)

Every capability the agent has — tools, prompt sections, providers, subagents,
MCP servers — is a declared plugin with its own mutability tier: `free` (change
it, nothing asks), `confirm` (the dialog names the risk), `locked` (not
editable, but always **viewable** — locked never means hidden).

**Written as files**, in `~/.quickcode/plugins/*.md` for every project or
`<project>/.quickcode/plugins/*.md` for one. The kind is in the frontmatter:

- **`kind: tool`** — a command tool. The argv is a JSON array and parameters
  substitute into elements, so a parameter value can never become two
  arguments and there is no shell to quote against.
- **`kind: agent`** — a subagent: its tools, model, permission ceiling and
  system prompt.
- **`kind: prompt`** — a section of the system prompt, at an order you choose.

**Written in Python**, for what files cannot express:

- **Tools** — entry point group `quickcode.tools` returning `Tool` instances.
- **Providers** — entry point group `quickcode.providers`; select per profile
  via `"provider"` in `~/.quickcode/config.json`.
- **MCP servers** — Claude-compatible `"mcpServers"` config in
  `.quickcode/settings.json` (project) or `~/.quickcode/settings.json` (user);
  stdio transport, tools appear as `mcp__<server>__<tool>` behind the same
  permission gate.

**Opening a project does not run it.** A project's own committed files can name
programs to execute — `mcpServers`, and `kind: tool` plugins — so both stay
inert until you trust that project once. The prompt shows every command line
before you approve it, and the grant is bound to a hash of what you saw, so a
later edit asks again. Trust is recorded at user scope; a project cannot
declare itself trustworthy.

### Installation

QuickCode installs three ways. All of them give you the same local web app.

**Windows installer (`.exe`)** — the turnkey path, and the only one that needs
nothing installed first: it carries a frozen copy of QuickCode and its Python
runtime, so **no Python and no Git are required** and nothing is downloaded
while it runs. Run `QuickCode-Setup-<version>.exe` from a
[GitHub release](https://github.com/devincii-io/QuickCode/releases) (or build it
yourself with `scripts\release.py --build`). It installs per-user into
`%LOCALAPPDATA%\Programs\QuickCode`, puts `quickcode`/`qc` on your `PATH`, and
adds a Start Menu shortcut — plus optional desktop and *"Open QuickCode here"*
folder context-menu entries. See [packaging/README.md](packaging/README.md).

**pip / uv** — for anyone who already has Python 3.12+. QuickCode is not
published on PyPI; install the wheel from a
[GitHub release](https://github.com/devincii-io/QuickCode/releases) (verify it
against the release's `SHA256SUMS.txt` first):

```bash
# <version> is the release you picked, e.g. 2.7.0
uv pip install https://github.com/devincii-io/QuickCode/releases/download/v<version>/quickcode-<version>-py3-none-any.whl
# or, on Windows, with ConPTY support for the terminal panel:
uv pip install "quickcode[pty] @ https://github.com/devincii-io/QuickCode/releases/download/v<version>/quickcode-<version>-py3-none-any.whl"
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

The installer's **QuickCode** shortcut runs the windowed entry point
(`QuickCodeApp.exe`; `quickcode-app` in a pip install), which opens your home
directory as the default project with no console window behind it. Right-click
a folder and *"Open QuickCode here"* opens that folder instead.

QuickCode's mark is a friendly blue ghost — it's the Start Menu icon, the
browser favicon, and the app's own brand mark
([`quickcode/frontend/assets/icon.svg`](quickcode/frontend/assets/icon.svg)).

### What it sends, and what it stores

**No telemetry, no analytics, no crash reporting, no phone-home.** Almost every
network call QuickCode makes is one you asked for: the model provider you
configured, and the `web_search` / `web_fetch` tools when the agent calls them
and you approve. The frontend loads nothing from the internet — no CDN, no
fonts, no external scripts.

There is exactly **one** request it makes on its own initiative: an
unauthenticated `GET` of the GitHub releases API to see whether a newer version
exists, at most once every six hours. It carries no API key, no cookie, no
identifier, no project path, no session or usage data and no version number —
the whole request is printed verbatim on its Settings card so you can check
that rather than take our word for it. Turn it off under Settings → Updates and
nothing is sent at all.

**But your prompts, source code and shell output do go to your model provider**
— that is what the product does — and the full transcript is written to
`<project>/.quickcode/sessions/*.jsonl` in plaintext, unredacted and
unexpired. Add `.quickcode/` to your project's `.gitignore`; QuickCode does not
do it for you.

[`docs/COMPLIANCE.md`](docs/COMPLIANCE.md) is the full write-up for a security,
legal or procurement review: every outbound connection, every file written,
the dependency licence table, the security model, the supply chain, and an
honest list of the known gaps. `sbom.cdx.json` is a CycloneDX SBOM of the
runtime dependency closure. Third-party attribution is in
[`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md).

### Documentation

[docs/README.md](docs/README.md) indexes everything under `docs/`: the
reference documents (architecture, permissions, tools, prompts, UI, subagents,
compliance, roadmap), the design rationale behind the plugin system, and an
archive of completed plans.

| Doc | Contents |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Layers, repository layout, async agent loop, provider abstraction, the bash tool and PTYs, the event log |
| [docs/PERMISSIONS.md](docs/PERMISSIONS.md) | Permission modes (plan → yolo), rules engine, protected paths, the trust gate, plan mode |
| [docs/TOOLS.md](docs/TOOLS.md) | Tool surface: descriptions, schemas, limits, safety rules |
| [docs/PROMPTS.md](docs/PROMPTS.md) | System prompt (XML-sectioned), dynamic reminders, compaction prompt |
| [docs/UI.md](docs/UI.md) | The web UI: workspaces and agent panes, trajectory, event protocol, dialogs, keyboard |
| [docs/AGENTS.md](docs/AGENTS.md) | Subagents, background jobs, task board; the teammate-mode design |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Shipped, in progress, next |

## Principles

1. **Efficiency is architecture, not magic** — cache-stable prompt prefixes, parallel tool execution, diff-based edits, hard output truncation, compaction before overflow.
2. **The harness owns safety** — the model emits tool calls; the permission layer decides what runs.
3. **Provider-agnostic core** — the agent loop speaks a normalized event stream; adapters translate.
4. **Every run is traceable** — the append-only event log is the source of truth; the trajectory view shows everything the model saw, and replay/resume derive from the same stream.
5. **Agent capabilities are plugins; the UI is not** — tools, providers, and MCP servers are swappable, the built-in web UI stays coherent.

## Development

```bash
uv sync --all-extras --dev
uv run --no-sync pytest -q                            # the test suite
.venv\Scripts\python.exe scripts\release.py --check   # tests + ruff + byte-compile + JS checks + clean diff
```

See [AGENTS.md](AGENTS.md) for the architecture conventions agents (and
humans) working in this repo should follow, and
[CONTRIBUTING.md](CONTRIBUTING.md) for the contribution workflow. Release
artifacts (wheel, sdist, Windows installer) are built locally with
`scripts\release.py --build`; see [SECURITY.md](SECURITY.md) for the
vulnerability-reporting process. Release history is in
[CHANGELOG.md](CHANGELOG.md).

MIT licensed.
