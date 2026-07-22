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

**Design phase.** The plan lives in `docs/`:

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
