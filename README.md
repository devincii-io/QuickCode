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
uv sync --all-extras --dev
export QUICKCODE_OPENROUTER_API_KEY=sk-...  # or save it in Settings (see below)
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

In the app: `Enter` send · `Shift+Enter` newline · `Esc` interrupt · mode,
model and composition pickers live on the composer · `⚙` opens
**Configuration**, a view organised around agents (Agents → Compositions →
Parts → Machine room → Install) rather than a flat list of settings ·
messages sent while the agent is busy are queued. Tests: `uv run pytest -q`.

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
uv pip install https://github.com/devincii-io/QuickCode/releases/download/v2.0.0/quickcode-2.0.0-py3-none-any.whl
# or, for the interactive terminal tool on Windows:
uv pip install "quickcode[pty] @ https://github.com/devincii-io/QuickCode/releases/download/v2.0.0/quickcode-2.0.0-py3-none-any.whl"
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
that rather than take our word for it. Turn it off under Install → Updates and
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
| [docs/design/AUTHORING.md](docs/design/AUTHORING.md) | Writing plugins as files: the five kinds, argv-first command tools, ids, validation, the trust gate |
| [docs/design/BINDING.md](docs/design/BINDING.md) | Compositions and bindings: how a capability is resolved, why intersection, and the pool-vs-grant split |
| [docs/design/UX.md](docs/design/UX.md) | Configuration as a view: the visual grammar, and the six questions every plugin answers |

## Principles

1. **Efficiency is architecture, not magic** — cache-stable prompt prefixes, parallel tool execution, diff-based edits, hard output truncation, compaction before overflow.
2. **The harness owns safety** — the model emits tool calls; the permission layer decides what runs.
3. **Provider-agnostic core** — the agent loop speaks a normalized event stream; adapters translate.
4. **Every run is traceable** — the append-only event log is the source of truth; the trajectory view shows everything the model saw, and replay/resume derive from the same stream.
5. **Agent capabilities are plugins; the UI is not** — tools, providers, and MCP servers are swappable, the built-in web UI stays coherent.

## Development

```bash
uv sync --all-extras --dev
.venv\Scripts\python.exe scripts\release.py --check   # tests + ruff + JS syntax + clean-diff
```

See [AGENTS.md](AGENTS.md) for the architecture conventions agents (and
humans) working in this repo should follow, and
[CONTRIBUTING.md](CONTRIBUTING.md) for the contribution workflow. Release
artifacts (wheel, sdist, Windows installer) are built locally with
`scripts\release.py --build`; see [SECURITY.md](SECURITY.md) for the
vulnerability-reporting process. Release history is in
[CHANGELOG.md](CHANGELOG.md).

MIT licensed.
