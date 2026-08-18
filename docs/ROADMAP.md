# Roadmap

Each milestone ends in something runnable. Checked items are present in the current
`0.1.0` codebase; partial items say exactly what remains.

## M0 — Skeleton that talks

- [x] `pyproject.toml`, `quickcode`/`qc` entry points, ruff and pytest
- [x] Profile/config loader with encrypted API-key storage
- [x] OpenAI-compatible streaming provider (OpenRouter by default)
- [x] Textual transcript, composer, status bar, and live-editable themes
- [x] Environment-aware system prompt

## M1 — It's an agent

- [x] Pydantic tool registry and provider schema translation
- [x] `read`, `edit`, `write`, `glob`, `grep`, and `bash`
- [x] Multi-round agent loop with parallel read-only calls and a loop guard
- [x] Permission modal with allow-once, persist, and deny feedback
- [x] Streaming reasoning/tool rendering and interruption

## M2 — Permissions v2 + efficiency

- [x] Scoped allow/ask/deny rules, command decomposition, protected paths, circuit breakers
- [x] Plan, ask, auto-edit, dontask, and optional yolo modes
- [x] Windows ConPTY/pywinpty backend with subprocess fallback
- [x] Token/cost ledger and context-window meter
- [x] Output clipping, read deduplication, and headless `-p` mode
- [ ] Provider-specific prompt-cache controls and a dedicated live PTY output panel

## M3 — Sessions, context, tasks

- [x] JSONL session persistence and `--continue`
- [x] Manual compaction with history rebuild and post-compaction reminder
- [x] Persistent task create/update/list/get tools, dependencies, owners, and sidebar
- [x] `QUICKCODE.md` / `AGENTS.md` / `CLAUDE.md` project instructions
- [x] Slash menu for `/compact`, `/clear`, `/mode`, `/model`, `/composition`, `/profile`,
      `/init`, and `/help` (models, usage and tasks are pickers and panels, not commands)
- [x] Conversation tabs and switcher, session rename, `@` path autocomplete
- [x] Automatic compaction on a declared threshold, in the web app and in `-p`

## M4 — Subagents

- [x] Built-in/custom agent definitions and worker-model routing
- [x] Live subagent rows, selectable detail panel, mouse controls, and drag resizing
- [x] Permission caps, auto-deny boundary, nesting limit, and concurrent fan-out
- [x] Report sanitization, artifact offload, and `send_message` resume
- [x] Orchestration prompt and structured delegation guidance
- [x] True detached/background jobs (`agent(background: true)`) with `agent_status` /
      `agent_result` collection, a live-parallelism cap, and an `agent_done` event

## M5 — Teammate mode

- [ ] Team lifecycle, peer mailbox, lead approval, and roster UI
- [ ] Atomic task claiming and idle notifications
- [ ] Git-worktree isolation for parallel writers

## M6 — Polish & depth

- [x] `/usage` dashboard and per-session ledger
- [x] `quickcode doctor` environment checks
- [x] Windows installer/bootstrap and shell/path hardening
- [x] Mouse-first focus behavior, resizable subagent pane, and theme presets
- [x] Toasts, prefix-filtered input history, and a fleet view that survives fifty subagents
      (grid layout, per-card follow, filters, solo view)
- [ ] Native Anthropic adapter, hooks, and background bash

## Next priorities

1. Background shell jobs and a dedicated PTY panel (`bash(run_in_background)` is still a
   declared-and-refused stub).
2. Native Anthropic adapter and provider-specific prompt-cache controls.
3. Hooks.
4. Teammate mode after task claiming and worktree isolation are designed together.
