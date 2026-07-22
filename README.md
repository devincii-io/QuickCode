# QuickCode

A terminal coding agent — an interactive TUI in the spirit of Claude Code and Codex CLI, built with **Python + Textual**, talking to models through a **pluggable provider layer** (OpenRouter by default, any OpenAI-compatible endpoint by config).

```
┌─ quickcode ─────────────────────────────┬─ tasks ──────────────┐
│ ⏺ Read src/index.py (142 lines)     ▸   │ ✓ Locate failing test│
│ ⏺ Edit src/paginate.py              ▾   │ ◐ Fix off-by-one     │
│   - end = start + size + 1              │ ○ Run full suite     │
│   + end = start + size                  ├─ files ──────────────┤
│ ● Running tests…                        │ M src/paginate.py    │
│                                         │                      │
│ > _                                     │                      │
├─────────────────────────────────────────┴──────────────────────┤
│ anthropic/claude-opus-4.8 · ctx 12% · $0.08 · Esc interrupt   │
└────────────────────────────────────────────────────────────────┘
```

## Status

**M0 + M1 core implemented.** A runnable TUI that streams model chat and acts as
a coding agent (six tools, permission-gated). See the roadmap for what's next.

### Quickstart

```bash
uv venv --python 3.12
uv pip install -e ".[dev,pty]"
export OPENROUTER_API_KEY=sk-...        # or edit the Profile tab in Settings
uv run quickcode                        # launch the TUI  (qc also works)
uv run quickcode -p "explain this repo" # headless / print mode
```

Keys: `Enter` send · `Ctrl+J` newline · `Shift+Tab` cycle permission mode ·
`F1` help · `F2` model picker · `F3` settings (curate OpenRouter models by
tier/role, view usage) · `Esc` interrupt. Tests: `uv run pytest -q`.

### Installation (Windows)

Two ways to install QuickCode on Windows without the manual `uv` steps above:

- **Installer (`.exe`)** — build `packaging\quickcode.iss` with the
  [Inno Setup](https://jrsoftware.org/isinfo.php) compiler to produce a
  wizard-driven installer that installs Git and Python (3.12+) if they're
  missing, `pip install`s QuickCode, adds it to your `PATH`, and creates a
  Start Menu shortcut. See [packaging/README.md](packaging/README.md).
- **PowerShell script** — from a checkout of this repo:

  ```powershell
  powershell -ExecutionPolicy Bypass -File scripts\install.ps1
  ```

  Creates/reuses a `.venv` (or pass `-UsePipx` for a global pipx install),
  ensuring Git/Python along the way. See
  [packaging/README.md](packaging/README.md) for flags and details.

### Design docs

The full plan lives in `docs/`:

| Doc | Contents |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Layers, async agent loop, provider abstraction, PTY subsystem, efficiency techniques |
| [docs/UI.md](docs/UI.md) | The interactive TUI: panes, conversation switcher, modals, keybindings (QuickTerm-safe) |
| [docs/AGENTS.md](docs/AGENTS.md) | Subagents, teammate mode, task board, orchestration playbook |
| [docs/PERMISSIONS.md](docs/PERMISSIONS.md) | Permission modes (plan → yolo), rules engine, plan mode, bypass guardrails |
| [docs/PROMPTS.md](docs/PROMPTS.md) | System prompt (XML-sectioned), dynamic reminders, compaction + delegation prompts |
| [docs/TOOLS.md](docs/TOOLS.md) | Tool surface: schemas, description copy, safety rules |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Milestones M0–M6 |

## Principles

1. **Efficiency is architecture, not magic** — cache-stable prompt prefixes, parallel tool execution, diff-based edits, hard output truncation, compaction before overflow.
2. **The harness owns safety** — the model emits tool calls; the permission layer decides what runs.
3. **Provider-agnostic core** — the agent loop speaks a normalized event stream; adapters translate.
4. **Actually interactive** — mouse-clickable, collapsible, streaming everything; the terminal UI should feel like an app, not a log file.
