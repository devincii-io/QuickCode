# Roadmap

Each milestone ends in something runnable. Order optimizes for "usable coding agent early", then efficiency, then the multi-agent superstructure.

## M0 — Skeleton that talks
- [ ] `pyproject.toml` (uv), `quickcode` entry point (+ `qc`), ruff + pytest wiring
- [ ] Config loader: `~/.quickcode/config.json` profiles (base_url, api_key_env, orchestrator/worker models)
- [ ] `openai_compat` provider: streaming chat vs OpenRouter → normalized `AgentEvent`s
- [ ] Minimal Textual app: transcript (streaming markdown), input, status bar, TCSS theme
- [ ] System prompt v1 rendered from the XML template (no tools yet)

**Exit:** `uv run quickcode` opens the TUI and streams model chat.

## M1 — It's an agent
- [ ] Tool registry (Pydantic → strict JSON Schema) + wire translation
- [ ] Tools: `read`, `edit`, `write`, `glob`, `grep`, `bash` (plain subprocess first; PTY in M2)
- [ ] Agent loop: tool rounds, parallel read-only execution, single tool-result message, loop guard
- [ ] Permissions v1: `ask` mode, PermissionModal via `push_screen_wait` (once/always/deny+message)
- [ ] Rendering: collapsible tool calls, diff widget, error styling; Esc interrupt (worker cancel + process kill)

**Exit:** it fixes a real bug in a real repo, asking before edits.

## M2 — Permissions v2 + efficiency
- [ ] Rules engine: allow/ask/deny arrays, compound-command decomposition, wrapper stripping, builtin read-only list, protected paths, settings scopes (deny beats allow across scopes)
- [ ] Modes: `plan` (structural tool withholding + `plan` tool + PlanReviewModal), `auto-edit`, `yolo` (entry confirm, circuit breakers), `dontask`; `Shift+Tab` cycling
- [ ] PTY subsystem (`pty/session.py`, QuickTerm patterns) behind the bash tool; live-tailing output panel
- [ ] Cache-stable request builder + `cache_control` breakpoints; token/cost ledger → status bar
- [ ] Output truncation + pagination hints everywhere; read-dedup
- [ ] `-p` headless mode (dontask); prompt snapshot tests

**Exit:** long sessions stay fast/cheap; plan → approve → auto-edit flow works end to end.

## M3 — Sessions, context, tasks
- [ ] JSONL session store; conversation registry; tabs + `Ctrl+O` switcher; `--continue` / `--resume`
- [ ] Compaction: threshold, compaction prompt, history rebuild, post-compaction reminder, `/compact`
- [ ] Task board: `task_create/update/list/get`, dependencies, sidebar checklist, `Ctrl+T` board view, persistence + re-injection after compaction
- [ ] `QUICKCODE.md`/`AGENTS.md`/`CLAUDE.md` project instructions; `/init` generator
- [ ] Slash commands + command palette: `/model`, `/plan`, `/clear`, `/compact`, `/help`; `@path` + `/` autocomplete

**Exit:** day-long multi-conversation sessions survive limits and restarts.

## M4 — Subagents
- [ ] `agent` tool + agent definitions (`.quickcode/agents/*.md`), built-in `explore`/`general`
- [ ] Agent panes: strip rows, expand-to-pane, focus routing, idle collapsing, background-by-default
- [ ] Permission capping + attributed prompts from background agents (orange glow)
- [ ] Report sanitization; artifacts-to-disk convention; `send_message` resume
- [ ] Orchestrator prompt section (`<orchestration>`) + delegation template; worker-model routing

**Exit:** "research X with 3 subagents" fans out on the worker model, panes live-stream, reports merge.

## M5 — Teammate mode
- [ ] Team lifecycle (`/team`, spawn confirmation with cost notice, graceful shutdown)
- [ ] Task board claiming under file lock; dependency gating; idle notifications
- [ ] Mailbox messaging (`send_message` between peers); delegate mode for the lead
- [ ] Plan-approval-by-lead flow; team roster UI; per-teammate mode changes
- [ ] Git-worktree isolation option for write-parallel teammates

**Exit:** a 3-teammate cross-layer build coordinated entirely through the board.

## M6 — Polish & depth
- [ ] Native `anthropic` provider adapter (adaptive thinking, exact caching, effort)
- [ ] `pre_tool_use` hooks; background bash + toasts; input history/paste polish
- [ ] Cost dashboards per agent/conversation; `/usage`
- [ ] Windows hardening pass (Git Bash detection, PowerShell rule canonicalization, path audit)
- [ ] `quickcode doctor` (rg, PTY, keyboard-protocol checks)

## Open questions (decide when we get there)
- Worktree-per-teammate as default vs opt-in (start opt-in; default on if conflicts bite)
- Reasoning display: collapsed dim vs hidden (start collapsed)
- Cost source of truth: OpenRouter usage field vs local price table (start usage field)
- Script-driven workflows (ultracode-style): only if the agent tool + board proves insufficient
