# Roadmap

Status as of **2.7.0**. Checked items ship in 2.7.0; unchecked items do not.
`CHANGELOG.md` has the release-by-release detail. The M0–M4 milestones were
first built on the Textual TUI (0.1.0) and carried over when the web UI
replaced it in 1.0.0.

## Unreleased (after 2.7.0)

Merged on `main`, not yet in a tagged release (CHANGELOG.md §Unreleased).

- [x] Background bash jobs: `bash(run_in_background)` with `bash_output` and
      `bash_kill`, a per-conversation cap, bounded output and process-tree
      cleanup on close, and a Jobs tab in the terminal drawer (docs/TOOLS.md §bash).
- [x] User-configurable command hooks — PreToolUse, PostToolUse,
      UserPromptSubmit, Stop, SessionStart — on the `LoopHook` seam, trust-gated
      for project hooks, with a Hooks page in Settings (docs/HOOKS.md).
- [x] A native Anthropic provider with prompt caching: `provider: "anthropic"`,
      plain `httpx`, explicit system and conversation-tail cache breakpoints,
      cache reads and writes in the ledger (docs/ARCHITECTURE.md §Provider layer).
- [x] Checkpoints and rewind for edits and writes (docs/CHECKPOINTS.md).
- [x] A context guard: compaction between rounds of one long turn, and one
      truncate-and-retry on a length refusal.
- [x] "Why was I prompted?" from the real engine — the dialog, the Help sandbox
      and `qc why` — and an "Always allow" that saves exactly what was approved.
- [x] Notifications, a `Ctrl+K` command palette, and search across sessions.
- [x] Headless `-p` builds the app's session: presets, plugins, MCP servers.

## M0 — Skeleton that talks

- [x] `pyproject.toml`, `quickcode`/`qc` entry points, ruff and pytest
- [x] Profile/config loader with encrypted API-key storage
- [x] OpenAI-compatible streaming provider (OpenRouter by default)
- [x] Transcript, composer, status bar and themes (a local web UI since 1.0.0)
- [x] Environment-aware system prompt

## M1 — It's an agent

- [x] Pydantic tool registry and provider schema translation
- [x] `read`, `edit`, `write`, `glob`, `grep`, and `bash`
- [x] Multi-round agent loop with parallel read-only calls and a loop guard
- [x] Permission dialog with allow-once, persist, and deny feedback
- [x] Streaming reasoning/tool rendering and interruption

## M2 — Permissions v2 + efficiency

- [x] Scoped allow/ask/deny rules, command decomposition, protected paths, circuit breakers
- [x] Plan, ask, auto-edit, dontask, and optional yolo modes
- [x] PTY backend (POSIX `pty`, Windows ConPTY/pywinpty) with a subprocess fallback
- [x] Token/cost ledger and context-window meter
- [x] Output clipping and headless `-p` mode (read deduplication was listed here and
      never built; it costs the prompt cache more than it saves — see ARCHITECTURE)
- [x] A terminal panel: your own shell in the project, plus every command the
      agent ran with its output (2.6.0)

## M3 — Sessions, context, tasks

- [x] JSONL session persistence and `--continue`
- [x] Manual compaction with history rebuild and post-compaction reminder
- [x] Persistent task create/update/list/get tools, dependencies, owners, and a Tasks panel
- [x] `QUICKCODE.md` / `AGENTS.md` / `CLAUDE.md` project instructions
- [x] Slash menu for `/compact`, `/clear`, `/mode`, `/model`, `/composition`, `/profile`,
      `/init`, and `/help` (models, usage and tasks are pickers and panels, not commands)
- [x] Conversation tabs, session rename, archive and delete, `@` path autocomplete
- [x] Automatic compaction on a declared threshold, in the web app and in `-p`

## M4 — Subagents

- [x] Built-in/custom agent definitions and worker-model routing
- [x] Live subagent cards, a selectable detail view, and a fleet view that survives fifty subagents
- [x] Permission caps, auto-deny boundary, nesting limit, and concurrent fan-out
- [x] Report sanitization, artifact offload, and `send_message` resume
- [x] Orchestration prompt and structured delegation guidance
- [x] Detached jobs (`agent(background: true)`) with `agent_status` /
      `agent_result` collection, a live-parallelism cap, and an `agent_done` event

## M5 — Teammate mode

Designed in docs/AGENTS.md §2. Only the worktree isolation is built, and for
subagents rather than teammates.

- [ ] Team lifecycle, peer mailbox, lead approval, and roster UI
- [ ] Atomic task claiming (a file-locked claim) and idle notifications
- [x] Git-worktree isolation for parallel writers — opt-in per subagent
      (`isolation: worktree`, docs/AGENTS.md §1.2); its work comes back as a
      `quickcode/*` branch

## M6 — Polish & depth

- [x] Usage panel and per-session ledger, subagents included
- [x] `quickcode doctor` environment checks
- [x] Windows installer (a frozen app since 2.3.0 — no Python or Git needed) and in-app updates
- [x] Resizable panels, theme presets, and shared appearance settings
- [x] Toasts and prefix-filtered input history
- [x] Project workspaces with split agent panes, saved layouts and a workspace sidebar (2.7.0)

## Beyond the milestones (shipped in 2.x)

- [x] Plugin kernel: every capability is a declared plugin with a `free` / `confirm` / `locked` tier (2.0.0)
- [x] Plugins as markdown files — command tools, agents, prompt sections — and duplicate-to-customise (2.0.0)
- [x] Compositions: one resolution model for the orchestrator and its subagents, switchable in a session (2.0.0)
- [x] The project trust gate for `mcpServers`, command tools and widening project settings (2.0.0)
- [x] Permission profiles (2.1.0)
- [x] `web_fetch` with an SSRF guard, and `web_search` over six providers (2.1.0)
- [x] A Help view (2.1.0)
- [x] TOON encoding for structured tool results where it saves tokens (2.5.0)

## Next

Not started. Roughly in order of value.

1. Teammate mode (M5), once task claiming is designed alongside the worktree isolation
   subagents already have.
2. Conversation rewind: truncate the history to before a turn from the same dialog
   that rewinds its files, and undo a rewind from the backups it keeps.
3. An `ask_user` tool: a structured question rendered as a dialog (docs/TOOLS.md).
4. Rule syntax: a `\*` escape so a glob command can be allowed exactly, quote-aware
   subcommand splitting, and allow rules that tolerate harmless redirections (`2>&1`).
5. MCP over Streamable HTTP, behind the same trust gate as stdio servers.
6. Cost and token budgets per session, and model fallback chains.
7. A `deny` rule on a bare tool name that withholds the tool from the request instead of
   refusing the call (docs/PERMISSIONS.md §Rules).
8. Plan mode follow-through: pin the approved plan as a reminder and seed the task board from it
   (docs/PERMISSIONS.md §Plan mode).
9. Windows Job Objects for process-tree kills, and a code-signed installer
   (docs/COMPLIANCE.md §8).
