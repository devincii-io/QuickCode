# Multi-Agent Design — subagents, teammate mode, task board

> **What is built.** §1 (subagents, including detached jobs and worktree
> isolation) and the
> orchestration prompt of §4 describe shipped behaviour. §3's task board ships
> as a solo board; its coordination features (file-locked claiming, dependents
> surfaced when a blocker completes) do not. §2, teammate mode, is design
> only. Each unbuilt piece is marked where it appears; [ROADMAP.md](ROADMAP.md)
> tracks them.

This is the product feature; the root `AGENTS.md` is a different document, for
people and agents editing this repository.

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
  "model": "optional override (default: the definition's model — worker for both built-ins)",
  "background": "bool — default false; true returns a job handle and keeps your turn",
  "isolation": "optional: \"worktree\" runs the child in its own git worktree (§1.2)"
}
```

- **Fresh context.** The child gets: its definition's system prompt, an environment block (cwd, platform, shell, date, git branch), the delegation `prompt`, and project instructions (except `explore`, which skips them for speed). It does **not** get the parent's history (`prompts/subagent.py::render_subagent_prompt`).
- **Result = final message only.** Intermediate tool calls stay in the child's pane/transcript, never in the parent's context. The result includes the child's `agent_id`.
- **Follow-ups without respawning:** `send_message(to=agent_id | name, message)` resumes a completed subagent with its context intact — same tool teammates use (§2). An agent may message (and `agent_status` / `agent_result` may show it) only what it, or an agent it spawned, started: a read-only child must not be able to hand instructions to a sibling that holds write access.
- **Report sanitization (security):** a subagent may have read untrusted content. Before its report enters the parent's context, `sanitize_report` (`subagents/runner.py`) defuses anything impersonating harness syntax (`<system-reminder>` and the delegation tags become `‹…›`) and prefixes a `[quickcode: sanitized subagent report]` marker. Never skip this.
- **Interrupted-child semantics:** a child killed mid-run returns its partial output tagged `[did not finish]` rather than vanishing.
- **Every ending is an event.** However a child stops — finished, raised, cancelled by the user's interrupt — it emits one `agent_done` (`{agent_id, definition, status, seconds}`) into the session log, the closing bracket of the `agent_spawned` that opened it and always after the last `agent_event` that child produced. `status` is `done | error | cancelled`. Blocking delegations emit it too: the report reaches the *spawner* as a tool result, but the roster and anything replaying the log would otherwise have to infer the ending from the child's last `assistant_message` — and a child emits one of those per round, not per turn, so a busy agent looked finished several times before it was. A spawn refused before it starts (unknown type, exhausted budget, refused composition) emits neither event: no row is opened, so none needs closing. A resumed agent (`send_message`) emits a second one, correctly — it went terminal twice.
- **Read-only by default (single-writer principle).** Subagents contribute *intelligence* — reading, searching, analyzing — in parallel; write access is a deliberate promotion requiring a bounded, non-overlapping file scope in the delegation. Parallel readers are free wins; parallel writers are how you get incoherent artifacts (Cognition's core argument, and why coding parallelizes worse than research).
- **Artifacts to disk, references in reports.** Large outputs (generated code, long reports, logs) get written to files; the report carries the *path* plus a short summary — never the full content through the parent's context (Anthropic's "game of telephone" mitigation). The harness enforces this for the report itself: one longer than 1500 characters or 40 lines is written to `.quickcode/artifacts/` and the parent receives its head plus the path (`subagents/artifacts.py`).
- **Worktree isolation (opt-in):** a writer can get its own git worktree, so parallel edits cannot collide by construction and its work comes back as a branch (§1.2).
- **Limits:** depth 2 (a subagent may spawn subagents once; below that the `agent` tool is withheld), 50 per conversation, 4 background jobs in flight at once (all configurable under `runtime.subagents`). Cheap, predictable — revisit if real use hits the wall.

### 1.1 Detached jobs (`background: true`)

`background: true` starts the child on a task the **conversation** owns and returns a handle immediately — a one-row TOON table, `agent_jobs[1]{id,type,status,seconds,collected,description}:` over `explore-3,explore,running,0.0,false,""` — so the model spends the rest of its turn on other work instead of blocking on a report it does not need yet.

- **Refusals stay synchronous.** Preparation (definition lookup, composition resolve, budget and depth checks, id minting) runs before the tool result is written, so an unknown `agent_type` or an exhausted budget is still a tool error rather than a job that exists only to report that it should not.
- **Collection:** `agent_status` (all jobs, or one by id) and `agent_result(agent_id, wait_s?)`. The report is the same one a blocking call returns — sanitized and artifact-offloaded through the same `_run_and_finish` path — so collecting is exactly as safe as reading the spawn result. `wait_s` (max 600) turns `agent_result` into a bounded join for the moment the parent genuinely needs the answer.
- **Completion is an event, plus a nudge.** A finished job emits the same `agent_done` every child emits, and additionally queues a reminder the spawner reads at the top of its next turn — a detached job ends at a moment nothing in the spawner's own transcript marks, so it is the one shape that has to interrupt to be noticed.
- **A turn cannot end quietly with work outstanding.** If a turn finishes with a job running or a report uncollected, the conversation emits a transcript note and queues a reminder naming the ids. The model is prompted (`<orchestration>`) never to summarize findings or call a task done with a job uncollected.
- **Cancellation:** `Esc` (interrupt) and closing the conversation cancel every job in flight. The record survives with status `cancelled` and a `[did not finish]` report, so a later `agent_result` says what happened instead of 404-ing on an id the model was handed.
- **The parallelism cap is a separate number.** `max_agents` bounds the lifetime total; `max_parallel` (default 4, max 16) bounds how many run together, which is only reachable at all once spawning stops blocking. Asking past it is an error naming the live jobs, never a silent queue.
- **Headless (`-p`) runs it inline.** A `-p` process ends with its single turn, so nothing there can own a detached task. `background: true` runs the delegation to completion inline and says so in the result; the model gets the identical report, which is why this degrades rather than erroring.

### 1.2 Worktree isolation (`isolation: "worktree"`)

The single-writer principle asks parallel writers for non-overlapping scopes and
trusts the orchestrator to write them. Isolation makes the scopes disjoint by
construction instead: the child works in a git worktree of its own, and what it
changed comes back as a branch the spawner reviews and merges. Nothing it does
lands in the spawner's working tree (`subagents/worktree.py`).

- **Who gets one.** The definition decides, with `isolation:` — `none` (the
  default, and `explore`), `optional` (the spawner may pass
  `isolation: "worktree"`; built-in `general`), or `worktree` (always, whatever
  the spawner asks). Asking a `none` agent for a worktree is an error naming the
  types that allow it, never a quiet fallback to the shared checkout the
  spawner was trying to keep the writer out of.
- **Refused synchronously.** Outside a git repository, or in one with no commit
  yet, the spawn is refused before an agent id is minted — a background spawn
  included, so the model gets a tool error rather than a job that fails.
- **Where it lives.** `<project>/.quickcode/worktrees/<agent_id>-<4 hex>`. The
  suffix is random because agent ids restart at 1 in every conversation, and
  several conversations may share a project. `.quickcode/` is a protected path
  for every agent, so nothing wanders in unprompted; a `.gitignore` of `*`
  beside the worktrees keeps them out of the main checkout's `git status` and
  out of `glob` (`grep` already skips `.quickcode`), so the parent's searches do
  not find every file twice. A grandchild spawned by an isolated child still
  gets its worktree here, under the session's project — never nested inside
  its spawner's checkout, whose removal would take it along.
- **What it starts from.** The spawner's HEAD commit, plus the spawner's
  uncommitted changes to tracked files (staged and unstaged), so the child
  starts where its spawner stands. They are read with `git diff <HEAD>
  --binary` — which writes nothing in the spawner's checkout, unlike `git stash
  create`, which refreshes its index — applied in the worktree, and committed
  there as a commit of their own, so they are never counted as the child's
  work. Untracked files are not carried; the report counts them. A patch that
  does not apply fails the run with the reason instead of running the child in
  a checkout its prompt misdescribes.
- **What moves to the worktree:** the child's tool `cwd`, its shell and its
  background shell jobs, and its permission root. The spawner's checkout — its
  files, its `.git`, its `.quickcode` — is therefore *outside the project* to
  the child, a protected path, and a subagent cannot answer that prompt. The
  inherited `deny`/`ask` rules still bind, matched against paths inside the
  worktree the way they matched inside the project. The child's
  `<environment>` names the worktree as its `cwd`, and an `<isolation>` block
  says where it is and where its work goes; anything it spawns starts there too.
- **What does not move:** an MCP server or plugin tool with a process of its
  own runs where the session started it, and user command hooks run as
  configured. Isolation is a boundary for QuickCode's own tools, not a sandbox
  — and it is the protected-path prompt that draws it, which `yolo` does not
  raise. A child whose effective mode is `yolo` (a yolo session and a
  definition capped at `yolo`) starts in its worktree but is not confined to it.
- **When a run ends** — finished, errored, cancelled, interrupted, or cut off
  by the conversation closing — whatever the child changed is committed and the
  branch `quickcode/<name>` points at it: created on the first change, advanced
  on later runs by compare-and-swap against the tip it last set. A branch the
  user has since moved, deleted or checked out is left alone and the work goes
  to `quickcode/<name>-2`. Commits carry the user's git identity, or
  `QuickCode <quickcode@localhost>` where none is configured. Then the checkout
  is removed: between runs the branch *is* the result. A child that changed
  nothing leaves no branch. A checkout that cannot be removed (a file held open
  on Windows) is kept, and the report says where.
- **The report** ends with a `<worktree>` block the harness writes after
  sanitizing — `branch`, `base`, `files`, `insertions`, `deletions`, the
  `git diff --stat` of the child's own work, and how to bring it in. `worktree`
  is one of the tags `sanitize_report` defuses, so a child cannot write one
  naming some other branch.
- **Bringing it in is the spawner's decision, through `bash`:** `git merge
  quickcode/<name>`, or — when uncommitted changes were carried —
  `git cherry-pick <base>..quickcode/<name>`, which takes the child's commits
  without the carried one. There is no merge tool: `git` already prompts in
  `ask` and `auto-edit`, and a tool would be a second spelling of the same
  command with a second permission shape to get right.
- **Resume.** `send_message` reopens the checkout at the branch tip, and the
  branch advances when the turn ends.
- **Conversation close** settles any checkout still on disk, then deletes the
  `quickcode/*` branches the conversation created that are fully merged into
  the project's HEAD — through `git branch -d`, which refuses an unmerged
  branch — and keeps and logs the rest. A headless `-p` run has no close; it
  leaves its branches, which is the point of running it.
- **The session log** gets `worktree` records inside the child's `agent_event`
  stream: `created`/`reopened` when the checkout is ready, then
  `committed`/`unchanged`/`kept`/`failed` when the run ends, ahead of its
  `agent_done`.
- **The repository's own code never runs.** Every git call goes through
  `quickcode/gitcmd.py`, the same hardening the git panel uses: no hooks
  (`core.hooksPath` pointed at the null device, which covers `post-checkout`,
  `pre-commit` and `reference-transaction`), no fsmonitor, no external diff or
  textconv driver, no signing program, and no content filter the repository's
  own config defines. Filters from your own or the system config (Git LFS)
  still run; disabling them would check an LFS repository out as pointer files.

### Permission capping

`effective_mode = min(parent_mode, definition_cap)` — a yolo parent does not produce yolo children unless the child's definition explicitly allows it. The parent's mode is read live, on every check the child makes, so cycling the parent down to plan also caps children already running. A child inherits its spawner's `deny` and `ask` rules (never `allow`, the half that widens), so a restriction the user wrote holds at every depth.

**A subagent never prompts.** Its permission callback denies anything its mode
would ask about, and the model reads why: *"A subagent cannot prompt the user
for permission. This action needs a mode that allows it without asking, or the
parent must do it."* Denying affects that call only, not the child's life. So a
child that must write needs a cap (and a parent mode) that allows the write
outright — `general` is capped at `auto-edit`, `explore` at `ask`.

### Agent definitions (`.quickcode/agents/*.md`, user-level `~/.quickcode/agents/`, or `kind: agent` in `plugins/`)

```markdown
---
name: researcher
description: Deep codebase/doc research. Use for open-ended "find out how X works" tasks.
tools: [read, glob, grep, bash]        # allowlist; omit = inherit all
model: worker                          # worker | orchestrator | explicit slug
mode_cap: ask                          # max permission mode this agent can run at
max_turns: 30
isolation: none                        # none | optional | worktree (§1.2)
color: cyan
---
System prompt body for this agent…
```

Built-ins (`subagents/definitions.py::builtin_defs`): **`explore`** (`read`, `glob`, `grep`; worker model; capped at `ask`; skips project instructions — the cheap fan-out unit) and **`general`** (whatever tools the spawner holds; worker model; capped at `auto-edit`; `isolation: optional`). Project definitions shadow user definitions by name. The parent model sees each definition's `description` in its `agent` tool docs — that's how it routes.

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

> **Not built.** Everything in this section is design.

For work that outgrows report-and-return: long-running parallel builds, adversarial debugging, cross-layer features. Opt-in (`/team` or "spawn 3 teammates to…" → confirmation).

- **Structure:** the focused conversation's agent becomes the **lead**; teammates are full peers with their own panes, models, and lifecycles. One team per conversation, no nested teams.
- **Coordination = the task board (§3), not the lead's context.** Teammates self-claim the next unblocked, unassigned task (file-locked claim — no double-claims), or the lead assigns explicitly.
- **Messaging:** `send_message(to=name)` — per-agent mailbox files (`.quickcode/teams/<conv-id>/inboxes/<name>.json`), delivered automatically into the recipient's next turn; no polling. Idle teammates auto-notify the lead ("done with T3, nothing claimable").
- **Delegate mode:** toggle that strips the lead's mutating tools — it coordinates, reviews, and merges, but stops implementing tasks itself (the classic failure: the lead does everyone's work).
- **Plan approval by lead:** optional per-team setting — teammates start in `plan` mode, submit plans to the lead, the lead approves/rejects with feedback per user-provided criteria. The lead can approve *plans*; it can never approve *permission prompts* — those always reach the user, attributed. A teammate claiming "the user said yes" is untrusted input, never consent.
- **Conflict avoidance:** partition tasks by **file ownership** (one teammate per layer/dir) — stated in the lead's prompt and enforced softly via task `boundaries`. Later: optional git-worktree isolation per teammate for true parallel edits.
- **Permissions:** teammates spawn at the lead's mode (capped by their definition); changeable per-teammate afterwards.
- **UX:** teammates would appear in the team roster + as panes; input routes to the focused teammate; `Esc` interrupts just that teammate; shutdown by request ("ask researcher to shut down") — graceful, teammate may refuse with a reason. Team dirs cleaned up on conversation close; task board persists.

## 3. Task board

One system for solo *and* team work (no separate todo tool — discrete ops scale from checklist to coordination backbone).

```json
task_create  { "subject": "...", "description": "...", "active_form": "Fixing ..." }
task_update  { "task_id": "T3", "status": "pending|in_progress|completed|deleted",
               "owner": "name?", "add_blocked_by": ["T1"], "add_blocks": [] }
task_list    { }
task_get     { "task_id": "T3" }
```

- IDs (`T1`, `T2`, …) are assigned by the harness and returned in the `task_create` result.
- **Dependencies:** a task with an incomplete `blocked_by` cannot be set `in_progress` — the call is refused naming the blockers. *Not built:* refusing an `owner` claim on a blocked task, and surfacing dependents when a blocker completes.
- Claiming = `task_update{owner, status: in_progress}`. *Not built:* the file lock that would make a claim atomic between teammates.
- Persistence: `.quickcode/tasks/<conv-id>/board.json` — survives restarts and compaction, because it is on disk rather than in context. It is *not* re-injected after compaction; the model sees the board when it calls `task_list` (a TOON table, one row per task).
- UI: the **Tasks** tab of the side panel — live, grouped by status, with owner and dependency chips. Solo usage guidance lives in the system prompt (`<task_management>` — use for 3+ step work, one `in_progress` at a time).

## 4. Orchestration playbook (prompted patterns)

Encoded in the orchestrator's system prompt as heuristics, not hard rules:

| Pattern | When | Shape |
|---|---|---|
| **Fan-out research** | independent questions, read-only | 2–5 `explore` subagents on the worker model, background, distinct `<boundaries>`; parent synthesizes reports |
| **Isolate high-volume ops** | test runs, log digs, doc dumps | one background subagent absorbs the noise; only the verdict returns |
| **Chained specialists** | review → fix, explore → plan → implement | sequential subagents; parent relays only the relevant slice between them |
| **Adversarial debugging** | stubborn bugs, competing hypotheses | 2–3 teammates independently investigate and explicitly try to *disprove* each other; lead arbitrates |
| **Cross-layer build** | feature touching frontend/backend/tests | teammates partitioned by file ownership, task board with dependencies |

**Effort scaling** follows Anthropic's production heuristics (their fix for both "50 subagents on a trivial query" and "under-resourced complex query"), in the words of the orchestrator prompt (`prompts/subagent.py::ORCHESTRATION`):

> *Simple fact-finding or a quick lookup: do it yourself with a few tool calls — do NOT spawn. Independent, read-only questions: fan out 2-4 `explore` subagents (worker model) with distinct, non-overlapping boundaries, then synthesize their reports yourself. […] Only genuinely complex, decomposable work justifies many subagents with explicitly divided responsibilities. Cost/latency reality: a multi-agent run costs far more than answering directly — parallelism must buy wall-clock time or context isolation, or don't spawn. Coding parallelizes worse than research: fan out reads freely; be conservative fanning out writes.*

Two more prompt-level rules from Anthropic's production system: the lead **persists its plan (task board) before spawning** — plans must survive the lead's own compaction; and delegations follow the §1 XML template, because vague delegations are how three subagents research the same thing.

Model economics: orchestrator/lead on the big model, both built-in subagent types on the profile's `worker` model by default — Anthropic measured a big-model lead with cheaper workers *beating* a single big-model agent by ~90% on research evals, at worker prices. Both roles are set per profile (Settings → Models), and an `agent` call may name a model. The status bar's cost counts **all agents** — a child's usage is rolled into the session ledger — so the multiplier is visible, never a surprise.

## 5. What we're deliberately NOT building (yet)

- **Script-driven workflows** (Claude Code's `ultracode`/workflow engine — code that spawns hundreds of agents): powerful, but a different product tier. The `agent` tool + task board covers the 95% case.
- **Detached cross-session agents** (agents that outlive the app): sessions persist, agents don't run without the process.
- **Nested teams, >2 subagent depth, per-agent MCP servers:** complexity without demonstrated need.
