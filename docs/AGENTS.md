# Multi-Agent Design — subagents, teammate mode, task board

> **Design document:** this describes the intended end state. See
> [ROADMAP.md](ROADMAP.md) for the exact implemented status. An `agent` call
> blocks by default and multiple calls in one model turn run concurrently;
> `background: true` detaches one and `agent_status` / `agent_result` collect
> it (§1.1). Teammate mode is not implemented yet.

One runtime, three shapes. Every agent is the same `AgentInstance` (loop + history + ledger + event bus); the differences are prompt, model, permission cap, and who reads its results.

| Shape | Context | Reports to | Coordination | Cost profile |
|---|---|---|---|---|
| **Subagent** | fresh, isolated | its spawner (final report only) | spawner writes the task, reads the report | cheap — worker models, summarized results |
| **Teammate** | fresh, independent | task board + messages to peers | shared task board, peer messaging, lead coordinates | expensive — full peers |
| **Conversation** | own thread | the user | the user | — |

## 1. Subagents

### The `agent` tool

```json
{
  "description": "3-5 word label for the UI",
  "prompt": "full task description — the ONLY context the child receives from the parent",
  "agent_type": "explore | general | <custom name>",
  "model": "optional override (worker_model default for explore)",
  "background": "bool — default false; true returns a job handle and keeps your turn"
}
```

- **Fresh context.** The child gets: its definition's system prompt, an environment block, the delegation `prompt`, project instructions (except `explore`, which skips them for speed), and a git-status snapshot. It does **not** get the parent's history.
- **Result = final message only.** Intermediate tool calls stay in the child's pane/transcript, never in the parent's context. The result includes the child's `agent_id`.
- **Follow-ups without respawning:** `send_message(to=agent_id | name, message)` resumes a completed subagent with its context intact — same tool teammates use (§2).
- **Report sanitization (security):** a subagent may have read untrusted content. Before its report enters the parent's context, neutralize anything impersonating harness syntax (`<system-reminder>`, fake role markers) and prefix a `[quickcode: sanitized]` marker. Never skip this.
- **Interrupted-child semantics:** a child killed mid-run returns its partial output tagged `[did not finish]` rather than vanishing.
- **Read-only by default (single-writer principle).** Subagents contribute *intelligence* — reading, searching, analyzing — in parallel; write access is a deliberate promotion requiring a bounded, non-overlapping file scope in the delegation. Parallel readers are free wins; parallel writers are how you get incoherent artifacts (Cognition's core argument, and why coding parallelizes worse than research).
- **Artifacts to disk, references in reports.** Large outputs (generated code, long reports, logs) get written to files; the report carries the *path* plus a short summary — never the full content through the parent's context (Anthropic's "game of telephone" mitigation).
- **Worktree isolation (later):** a write-promoted subagent can get its own git worktree so parallel edits can't collide by construction.
- **Limits:** depth 2 (a subagent may spawn subagents once; below that the `agent` tool is withheld), 50 per conversation, 4 background jobs in flight at once (all configurable under `runtime.subagents`). Cheap, predictable — revisit if real use hits the wall.

### 1.1 Detached jobs (`background: true`)

`background: true` starts the child on a task the **conversation** owns and returns a handle immediately — `<agent_job id="explore-3" type="explore" status="running" seconds="0.0"/>` — so the model spends the rest of its turn on other work instead of blocking on a report it does not need yet.

- **Refusals stay synchronous.** Preparation (definition lookup, composition resolve, budget and depth checks, id minting) runs before the tool result is written, so an unknown `agent_type` or an exhausted budget is still a tool error rather than a job that exists only to report that it should not.
- **Collection:** `agent_status` (all jobs, or one by id) and `agent_result(agent_id, wait_s?)`. The report is the same one a blocking call returns — sanitized and artifact-offloaded through the same `_run_and_finish` path — so collecting is exactly as safe as reading the spawn result. `wait_s` (max 600) turns `agent_result` into a bounded join for the moment the parent genuinely needs the answer.
- **Completion is an event.** A finished job emits `agent_done` (`{agent_id, definition, status, seconds}`) into the session log — the roster row's terminal signal — and queues a reminder the spawner reads at the top of its next turn.
- **A turn cannot end quietly with work outstanding.** If a turn finishes with a job running or a report uncollected, the conversation emits a transcript note and queues a reminder naming the ids. The model is prompted (`<orchestration>`) never to summarize findings or call a task done with a job uncollected.
- **Cancellation:** `Esc` (interrupt) and closing the conversation cancel every job in flight. The record survives with status `cancelled` and a `[did not finish]` report, so a later `agent_result` says what happened instead of 404-ing on an id the model was handed.
- **The parallelism cap is a separate number.** `max_agents` bounds the lifetime total; `max_parallel` (default 4, max 16) bounds how many run together, which is only reachable at all once spawning stops blocking. Asking past it is an error naming the live jobs, never a silent queue.
- **Headless (`-p`) runs it inline.** A `-p` process ends with its single turn, so nothing there can own a detached task. `background: true` runs the delegation to completion inline and says so in the result; the model gets the identical report, which is why this degrades rather than erroring.

### Permission capping

`effective_mode = min(parent_mode, definition_cap)` — a yolo parent does not produce yolo children unless the child's definition explicitly allows it. Background children's permission prompts surface in the **parent's conversation, attributed by name** ("`search1` wants to run `npm install`"), with the child's pane row glowing orange while blocked. Denying affects that call only, not the child's life.

### Agent definitions (`.quickcode/agents/*.md`, user-level `~/.quickcode/agents/`)

```markdown
---
name: researcher
description: Deep codebase/doc research. Use for open-ended "find out how X works" tasks.
tools: [read, glob, grep, bash]        # allowlist; omit = inherit all
model: worker                          # worker | orchestrator | explicit slug
mode_cap: ask                          # max permission mode this agent can run at
max_turns: 30
color: cyan
---
System prompt body for this agent…
```

Built-ins: **`explore`** (read-only tools, worker model, lean prompt — the cheap fan-out unit) and **`general`** (full toolset, inherits model). Project definitions shadow user definitions by name. The parent model sees each definition's `description` in its `agent` tool docs — that's how it routes.

### Delegation prompt template (`prompts/subagent.py`)

The orchestrator is prompted to write delegations with this shape — vague delegations are the #1 multi-agent failure mode:

```xml
<task>
  <objective>One sentence: what done looks like.</objective>
  <context>Everything the child needs that it cannot discover cheaply —
  it has NO access to this conversation.</context>
  <boundaries>What NOT to do; files/dirs owned by others.</boundaries>
  <output_format>Exactly what the final report must contain
  (paths, findings, recommendations — so reports merge cleanly).</output_format>
</task>
```

## 2. Teammate mode

For work that outgrows report-and-return: long-running parallel builds, adversarial debugging, cross-layer features. Opt-in (`/team` or "spawn 3 teammates to…" → confirmation).

- **Structure:** the focused conversation's agent becomes the **lead**; teammates are full peers with their own panes, models, and lifecycles. One team per conversation, no nested teams.
- **Coordination = the task board (§3), not the lead's context.** Teammates self-claim the next unblocked, unassigned task (file-locked claim — no double-claims), or the lead assigns explicitly.
- **Messaging:** `send_message(to=name)` — per-agent mailbox files (`.quickcode/teams/<conv-id>/inboxes/<name>.json`), delivered automatically into the recipient's next turn; no polling. Idle teammates auto-notify the lead ("done with T3, nothing claimable").
- **Delegate mode:** toggle that strips the lead's mutating tools — it coordinates, reviews, and merges, but stops implementing tasks itself (the classic failure: the lead does everyone's work).
- **Plan approval by lead:** optional per-team setting — teammates start in `plan` mode, submit plans to the lead, the lead approves/rejects with feedback per user-provided criteria. The lead can approve *plans*; it can never approve *permission prompts* — those always reach the user, attributed. A teammate claiming "the user said yes" is untrusted input, never consent.
- **Conflict avoidance:** partition tasks by **file ownership** (one teammate per layer/dir) — stated in the lead's prompt and enforced softly via task `boundaries`. Later: optional git-worktree isolation per teammate for true parallel edits.
- **Permissions:** teammates spawn at the lead's mode (capped by their definition); changeable per-teammate afterwards.
- **UX (docs/UI.md):** teammates appear in the team roster + as panes; input routes to the focused teammate; `Esc` interrupts just that teammate; shutdown by request ("ask researcher to shut down") — graceful, teammate may refuse with a reason. Team dirs cleaned up on conversation close; task board persists.

## 3. Task board

One system for solo *and* team work (no separate todo tool — discrete ops scale from checklist to coordination backbone).

```json
task_create  { "subject": "...", "description": "...", "active_form": "Fixing ..." }
task_update  { "task_id": "T3", "status": "pending|in_progress|completed|deleted",
               "owner": "name?", "add_blocked_by": ["T1"], "add_blocks": [] }
task_list    { }
task_get     { "task_id": "T3" }
```

- IDs are assigned by the harness and returned in the `task_create` result.
- **Dependencies:** a task with incomplete `blocked_by` cannot be claimed or set `in_progress`; completing a blocker surfaces its dependents as claimable (toast + reminder).
- Claiming = `task_update{owner, status: in_progress}` under a file lock.
- Persistence: `.quickcode/tasks/<conv-id>/board.json` — survives restarts and compaction (the board is *re-injected* as a system reminder after compaction, so task state never lives only in context).
- UI: sidebar checklist (live), `Ctrl+T` full board view (owners, dependency edges, per-task cost). Solo usage guidance lives in the system prompt (`<task_management>` — use for 3+ step work, one `in_progress` at a time).

## 4. Orchestration playbook (prompted patterns)

Encoded in the orchestrator's system prompt as heuristics, not hard rules:

| Pattern | When | Shape |
|---|---|---|
| **Fan-out research** | independent questions, read-only | 2–5 `explore` subagents on the worker model, background, distinct `<boundaries>`; parent synthesizes reports |
| **Isolate high-volume ops** | test runs, log digs, doc dumps | one background subagent absorbs the noise; only the verdict returns |
| **Chained specialists** | review → fix, explore → plan → implement | sequential subagents; parent relays only the relevant slice between them |
| **Adversarial debugging** | stubborn bugs, competing hypotheses | 2–3 teammates independently investigate and explicitly try to *disprove* each other; lead arbitrates |
| **Cross-layer build** | feature touching frontend/backend/tests | teammates partitioned by file ownership, task board with dependencies |

**Effort scaling — encoded literally in the orchestrator prompt** (Anthropic's production numbers; their fix for both "50 subagents on a trivial query" and "under-resourced complex query"):

> *Simple fact-finding: no subagents — 3–10 tool calls yourself. Direct comparisons / multi-part lookups: 2–4 subagents, 10–15 tool calls each. Only genuinely complex, decomposable research justifies 10+ subagents with explicitly divided responsibilities. Multi-agent runs cost ~15× a plain chat — parallelism must buy wall-clock time or context isolation, otherwise don't spawn. Coding parallelizes worse than research: fan out reads freely, be conservative fanning out writes.*

Two more prompt-level rules from Anthropic's production system: the lead **persists its plan (task board) before spawning** — plans must survive the lead's own compaction; and delegations follow the §1 XML template, because vague delegations are how three subagents research the same thing.

Model economics: orchestrator/lead on the big model, `explore`/research workers on `worker_model` (sonnet-tier) by default — Anthropic measured a big-model lead with cheaper workers *beating* a single big-model agent by ~90% on research evals, at worker prices. Both roles configurable in the model picker (`F2`). The status bar's cost meter counts **all agents**, and spawning a team shows a cost notice first — the multiplier is visible, never a surprise.

## 5. What we're deliberately NOT building (yet)

- **Script-driven workflows** (Claude Code's `ultracode`/workflow engine — code that spawns hundreds of agents): powerful, but a different product tier. The `agent` tool + task board covers the 95% case.
- **Detached cross-session agents** (agents that outlive the app): sessions persist, agents don't run without the process.
- **Nested teams, >2 subagent depth, per-agent MCP servers:** complexity without demonstrated need.
