"""Spawn and drive a subagent to completion, returning a sanitized report.

A subagent is the same ``AgentInstance`` as everything else — the differences
are a fresh prompt/history, a bounded toolset, a capped permission mode, and an
auto-deny permission callback (a headless child cannot prompt the user).

Two spawn shapes share every line of that. ``spawn_subagent`` awaits the child
and hands back its report, which is what the ``agent`` tool has always done.
``spawn_subagent_background`` runs the identical preparation, then puts the
same finishing coroutine on a task the *conversation* owns and returns a
``JobRecord`` immediately -- so the model keeps its turn and collects later
through ``agent_status`` / ``agent_result``. The child, its ceiling, its
sanitization and its artifact offload are the same either way; only who waits
differs.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import TYPE_CHECKING, Any

from quickcode.config import Environment, Profile
from quickcode.core.agent import (
    AgentInstance,
    PermissionOutcome,
    PermissionRequest,
)
from quickcode.core.history import History
from quickcode.core.permissions import Mode, PermissionEngine, Rules
from quickcode.kernel import preset as preset_module
from quickcode.kernel.composition import (
    MODE_PRIVILEGE,
    Resolved,
    RuntimeLimits,
    cap_mode,
)
from quickcode.kernel.resolve import resolve_composition
from quickcode.prompts.subagent import render_subagent_prompt
from quickcode.providers.base import Provider
from quickcode.subagents.artifacts import maybe_offload
from quickcode.subagents.definitions import AgentDef, load_defs
from quickcode.subagents.jobs import CANCELLED, DONE, ERROR, JobRecord
from quickcode.tools.base import ReadRegistry, ToolCtx
from quickcode.tools.registry import ToolRegistry, build_registry, core_tools

if TYPE_CHECKING:
    from quickcode.core.agent import EventBus

# The fallbacks, kept as module names because callers and tests import them.
# What a session actually enforces is ``deps.limits``, resolved once at open
# from ``runtime.subagents.max_depth`` and ``max_agents`` and clamped to the
# maxima those settings declare -- a settings file cannot raise either backstop
# past the number its own card promises.
MAX_DEPTH = RuntimeLimits().max_depth
MAX_AGENTS = RuntimeLimits().max_agents
# How many detached jobs may be *in flight* at once. ``max_agents`` bounds the
# total a conversation may ever spawn; it says nothing about how many run
# together, and a tool that returns immediately makes an unbounded fan-out one
# cheap loop away. Blocking spawns are bounded by the number of tool calls in a
# single assistant message and are deliberately not counted here.
MAX_PARALLEL = RuntimeLimits().max_parallel

# Kept as module names because callers import them from here. The definitions
# moved into ``kernel/composition.py`` so the resolver can narrow a ceiling
# without importing the runtime that calls it.
_PRIV = MODE_PRIVILEGE

__all__ = [
    "MAX_AGENTS",
    "MAX_DEPTH",
    "MAX_PARALLEL",
    "BackgroundUnavailable",
    "JobRecord",
    "SubagentDeps",
    "cap_mode",
    "resume_subagent",
    "sanitize_report",
    "spawn_subagent",
    "spawn_subagent_background",
]


class BackgroundUnavailable(ValueError):
    """This session has nowhere to park a detached job.

    A ``ValueError`` so a caller that does not care still turns it into an
    error result, but its own type so the ``agent`` tool can do the useful
    thing instead: run the delegation inline and say that it did. A headless
    ``-p`` run is the case that matters -- the process ends with the turn, so a
    task nobody awaits is a report nobody reads.
    """


@dataclass
class SubagentDeps:
    """Everything the ``agent`` tool needs to build a child, carried in
    ``ToolCtx.extra['subagent']``.

    ``mode_getter`` is read live at spawn time so runtime mode changes (the user
    cycling Shift+Tab) correctly cap later delegations.
    """

    provider: Provider
    profile: Profile
    env: Environment
    mode_getter: Callable[[], Mode]
    cwd: Path
    depth: int = 0
    # Shared across the whole conversation's agent tree.
    counter: itertools.count = field(default_factory=lambda: itertools.count(1))
    spawned: list[str] = field(default_factory=list)
    # Every spawned child, keyed by agent_id, so ``send_message`` can resume one
    # by id from anywhere in the tree. Shared down the tree like counter/spawned.
    roster: dict[str, AgentInstance] = field(default_factory=dict)
    # Detached runs, keyed by the same agent_id. Shared down the tree for the
    # same reason the roster is: a job started three levels down is still this
    # conversation's job to cap, cancel and clean up.
    jobs: dict[str, JobRecord] = field(default_factory=dict)
    # UI hook: called synchronously at spawn with (agent_id, definition name,
    # the child's EventBus) so a live pane can subscribe to the child's stream.
    # Optional — headless runs leave it None.
    on_pane: Callable[[str, str, EventBus], None] | None = None
    # The other end of ``on_pane``: called once, when a child reaches a terminal
    # state, with (agent_id, definition name, status, seconds). It is what emits
    # ``agent_done``.
    #
    # Blocking spawns fire it too, which they did not used to. The old rule --
    # a blocking child's completion *is* the spawner's tool result, so a second
    # marker is redundant -- holds for the transcript and for nothing else. The
    # roster, and anything replaying the log, had to infer the ending from "the
    # last thing I saw from this agent was an assistant_message", which is
    # simply wrong for every round that ends in tool calls: the provider emits
    # one ``TurnDone`` per round, so a busy child looks finished several times
    # before it is. An ending is a fact; it gets an event.
    on_done: Callable[[str, str, str, float], None] | None = None
    # Where a detached job's task goes to be owned. A task nobody holds is
    # garbage-collected at the end of the turn that created it, so this is not
    # bookkeeping -- it is the difference between a background job and a
    # cancelled one. None means this session cannot detach at all (see
    # ``BackgroundUnavailable``).
    adopt_task: Callable[[Any], None] | None = None
    # The agent these deps belong to -- the one that calls the ``agent`` tool,
    # and so the one to wake when one of its jobs finishes. Set per level, not
    # shared down: a nested agent's jobs are news for the nested agent.
    owner: AgentInstance | None = None
    # The tools this session actually has, including plugin and MCP ones. A
    # definition's ``tools:`` list is selected from this. None falls back to
    # the built-in core tools, which is what a bare embedder gets.
    tool_pool: list | None = None
    # Which agent definitions this session's preset admits (names or globs).
    # None means no restriction; an empty list means no delegation at all.
    # Superseded by ``parent.spawns``; kept for embedders that set it.
    allowed_agents: list[str] | None = None

    # -- composition ------------------------------------------------------
    # The session pool: everything this install has, minus the plugins that are
    # switched off. Set once at open and never narrowed on the way down -- it
    # is the *session's* capability envelope, not any one agent's grant, and
    # keeping the two apart is what makes "the orchestrator may not edit files,
    # but its children may" expressible at all.
    pool: list | None = None
    # The spawning agent's resolved composition. Children are intersected
    # against it from depth 1 down, so narrowing compounds instead of resetting.
    parent: Resolved | None = None
    # Agent definitions, snapshotted at session open. Reloading them per spawn
    # would change an agent's behaviour mid-conversation.
    defs: dict[str, AgentDef] | None = None
    # The session's preset, for the layer-3 contribution.
    preset: Any = None
    # Delegation turns spent per agent id, against that agent's max_turns.
    turns: dict[str, int] = field(default_factory=dict)
    budgets: dict[str, int] = field(default_factory=dict)
    # Which definition each spawned id came from. Shared down the tree like the
    # roster, so a resume three levels down can name the same definition in its
    # ``agent_done`` that the matching ``agent_spawned`` named.
    kinds: dict[str, str] = field(default_factory=dict)
    # The session's frozen runtime numbers, shared down the whole tree so every
    # depth counts against the same budget the session opened with.
    limits: RuntimeLimits = field(default_factory=RuntimeLimits)

    def child(self, depth: int, effective_mode: Mode,
              *, tool_pool: list | None = None,
              parent: Resolved | None = None) -> SubagentDeps:
        """A deps object for the next level down, sharing the counter/roster.

        A child's own spawns are capped by the child's fixed effective mode and
        narrowed against ``parent`` -- the composition this child was itself
        given. Passing the session's own composition down instead would make
        delegation an escalation: a read-only agent could spawn one whose
        definition says ``tools: null`` and have it inherit write, edit and
        bash.
        """
        return SubagentDeps(
            provider=self.provider,
            profile=self.profile,
            env=self.env,
            mode_getter=lambda: effective_mode,
            cwd=self.cwd,
            depth=depth,
            counter=self.counter,
            spawned=self.spawned,
            roster=self.roster,
            jobs=self.jobs,
            on_pane=self.on_pane,
            on_done=self.on_done,
            adopt_task=self.adopt_task,
            tool_pool=self.tool_pool if tool_pool is None else tool_pool,
            allowed_agents=self.allowed_agents,
            pool=self.pool,
            parent=parent if parent is not None else self.parent,
            defs=self.defs,
            preset=self.preset,
            turns=self.turns,
            budgets=self.budgets,
            kinds=self.kinds,
            limits=self.limits,
        )

    def session_pool(self) -> list:
        """The pool to resolve against, with the legacy fallbacks in order."""
        if self.pool is not None:
            return self.pool
        if self.tool_pool is not None:
            return self.tool_pool
        return core_tools(include_plan=False, include_agent=False)

    def definitions(self) -> dict[str, AgentDef]:
        return self.defs if self.defs is not None else load_defs(self.cwd)

    # -- detached jobs ----------------------------------------------------

    def background_available(self) -> bool:
        """Whether a detached job would have an owner to outlive the turn."""
        return self.adopt_task is not None

    def running_jobs(self) -> list[JobRecord]:
        return [j for j in self.jobs.values() if j.running]

    def uncollected_jobs(self) -> list[JobRecord]:
        """Finished jobs whose report the spawner has not read yet."""
        return [j for j in self.jobs.values() if not j.running and not j.collected]

    def cancel_jobs(self) -> int:
        """Cancel every job still in flight. Returns how many were cancelled.

        Called by the conversation on interrupt and on close. The tasks mark
        themselves ``cancelled`` as they unwind, so the registry stays truthful
        without this having to guess.
        """
        live = self.running_jobs()
        for job in live:
            if job.task is not None and not job.task.done():
                job.task.cancel()
        return len(live)


async def _deny_cb(_req: PermissionRequest) -> PermissionOutcome:
    return PermissionOutcome(
        allow=False,
        deny_message=(
            "A subagent cannot prompt the user for permission. This action needs "
            "a mode that allows it without asking, or the parent must do it."
        ),
    )


ROLES = ("worker", "orchestrator")


def _resolve_role(deps: SubagentDeps, spec: str) -> str:
    return deps.profile.resolve(spec) if spec in ROLES else spec


def sanitize_report(text: str) -> str:
    """Neutralize harness-impersonating syntax in untrusted subagent output
    before it enters the parent's context, and mark it as sanitized.

    TOON is deliberately *not* on the list. What this function mangles are
    tags that carry no author -- a ``<system-reminder>`` in a report reads as
    the harness speaking, and nothing in the surrounding text says otherwise.
    A TOON table carries no such authority: it is data, it arrives inside the
    ``[quickcode: sanitized subagent report]`` marker and the ``<subagent
    id=... status=...>`` wrapper the collector adds, and a forged
    ``matches[3]{path,line,text}:`` block is worth exactly what the sentence
    "I found three matches" is worth from the same child. Mangling it would
    cost more than it buys: the subagents most likely to emit a TOON block are
    the search-and-report ones, whose findings *are* tool output they are
    quoting back.
    """
    for tag in ("system-reminder", "task", "objective", "context", "boundaries"):
        text = text.replace(f"<{tag}>", f"‹{tag}›").replace(f"</{tag}>", f"‹/{tag}›")
    text = text.replace("<system-reminder", "‹system-reminder")
    return "[quickcode: sanitized subagent report]\n" + text.strip()


def _prepare_child(
    deps: SubagentDeps,
    *,
    agent_type: str,
    model_override: str | None = None,
) -> tuple[str, AgentInstance]:
    """Everything a spawn does before the child's first turn.

    Split out so a detached spawn refuses *synchronously*: an unknown agent
    type, an exhausted budget or a refused composition comes back as a tool
    error the model can act on, instead of as a job that exists only to report
    that it should never have been started.
    """
    max_agents = deps.limits.max_agents
    if len(deps.spawned) >= max_agents:
        raise ValueError(f"subagent limit reached ({max_agents} per conversation)")
    # The depth backstop, enforced here as well as in the resolver. The
    # resolver withholds the delegation pair from an agent that is already at
    # the limit, which is what the model sees; this is what holds if an
    # embedder hands the tool out anyway.
    if deps.depth >= deps.limits.max_depth:
        raise ValueError(
            f"delegation depth limit reached (max_depth={deps.limits.max_depth})"
        )

    defs = deps.definitions()
    if deps.allowed_agents is not None:
        defs = {
            name: d for name, d in defs.items()
            if any(fnmatchcase(name, p) for p in deps.allowed_agents)
        }

    # Resolution is total, so this cannot fail; the refusal comes next, and it
    # comes before the id is minted -- a refused composition should not burn an
    # agent slot for a child that never existed.
    resolved = resolve_composition(
        agent_type,
        pool=deps.session_pool(),
        preset=deps.preset if deps.preset is not None
        else preset_module.builtin_presets()[preset_module.DEFAULT_PRESET],
        defs=defs,
        cwd=deps.cwd,
        parent=deps.parent,
        depth=deps.depth,
        overrides={"model": model_override} if model_override else None,
        max_depth=deps.limits.max_depth,
        resolve_model=lambda spec: _resolve_role(deps, spec),
    )
    if resolved.errors():
        raise ValueError(resolved.refusal())

    defn = defs[agent_type]
    model = _resolve_role(deps, resolved.model or defn.model)

    child_depth = deps.depth + 1
    agent_id = f"{defn.name}-{next(deps.counter)}"
    deps.spawned.append(agent_id)
    deps.budgets[agent_id] = resolved.max_turns
    deps.turns[agent_id] = 0
    effective_mode = cap_mode(deps.mode_getter(), resolved.ceiling)

    # The child's bounded registry is built from the resolved tool list, so the
    # answer the introspection endpoint gives and the tools the model is handed
    # come from one computation. The delegation pair is still granted by depth,
    # never by allowlist -- the resolver decides whether it is in the list, and
    # ``build_registry`` is what adds the instances.
    include_agent = "agent" in resolved.tools
    registry: ToolRegistry = build_registry(
        list(resolved.tools), include_agent=include_agent, pool=deps.session_pool()
    )
    # Built after the registry, because what this child may delegate is bounded
    # by what this child itself got -- never by what the session has.
    child_deps = (
        deps.child(child_depth, effective_mode,
                   tool_pool=list(registry.tools.values()), parent=resolved)
        if include_agent else None
    )
    # ``defn.name`` follows the child around: the pane, the done event and the
    # job record all name the definition rather than the id's prefix.
    child_definition = defn.name
    deps.kinds[agent_id] = child_definition

    child_ctx = ToolCtx(
        cwd=deps.cwd,
        read_registry=ReadRegistry(),
        shell_name=deps.env.shell_name,
        platform=deps.env.platform,
        extra={"subagent": child_deps} if include_agent else {},
    )

    system_prompt = render_subagent_prompt(defn, deps.env, model=model)
    child = AgentInstance(
        name=agent_id,
        provider=deps.provider,
        registry=registry,
        history=History(system_prompt),
        ctx=child_ctx,
        permissions=PermissionEngine(effective_mode, Rules(), deps.cwd),
        model=model,
        permission_cb=_deny_cb,
        limits=deps.limits,
    )
    # Registered immediately so the agent is resumable via send_message even if
    # this first run errors out below.
    deps.roster[agent_id] = child
    # The child's own deps point at the child, so a job *it* starts wakes it
    # rather than whoever is three levels up. Set here because the deps have to
    # exist before the ``ToolCtx`` that carries them, and the child after that.
    if child_deps is not None:
        child_deps.owner = child

    # Surface a live pane before the child starts streaming. A UI failure here
    # must never break the subagent run, so it is fully isolated.
    if deps.on_pane is not None:
        try:
            deps.on_pane(agent_id, child_definition, child.bus)
        except Exception:  # noqa: BLE001 — the pane is best-effort telemetry
            pass

    return agent_id, child


async def spawn_subagent(
    deps: SubagentDeps,
    *,
    agent_type: str,
    prompt: str,
    model_override: str | None = None,
) -> tuple[str, str, str]:
    """Run one subagent to completion. Returns ``(agent_id, report, status)``.

    The status is the same vocabulary a ``JobRecord`` uses, so a caller can
    tell a report that came back from a finished child apart from one that
    came back because the child died — the text alone cannot be trusted for
    that, and the tool result has to say which it was.

    Raises ValueError for an unknown agent_type or when a spawn limit is hit —
    the tool wrapper turns those into an error ToolResult.
    """
    agent_id, child = _prepare_child(
        deps, agent_type=agent_type, model_override=model_override
    )
    status, report = await _blocking_run(deps, agent_id, child, prompt)
    return agent_id, report, status


async def _blocking_run(
    deps: SubagentDeps, agent_id: str, child: AgentInstance, message: str
) -> tuple[str, str]:
    """One awaited delegation turn, with its ending announced.

    The announcement is the point. ``_run_and_finish`` already turns every way
    a child can stop into a report, so the *spawner* is told either way; this
    is what tells everyone else. Cancellation gets its own branch because the
    parent's interrupt cancels the gather this coroutine is running in, so the
    only chance to say "that child is over" is on the way out.
    """
    started = time.monotonic()
    try:
        status, report = await _run_and_finish(deps, agent_id, child, message)
    except asyncio.CancelledError:
        _announce_done(deps, agent_id, CANCELLED, time.monotonic() - started)
        raise
    _announce_done(deps, agent_id, status, time.monotonic() - started)
    return status, report


def _announce_done(
    deps: SubagentDeps, agent_id: str, status: str, seconds: float
) -> None:
    """Fire ``on_done`` for one child, never letting telemetry break a run."""
    if deps.on_done is None:
        return
    try:
        deps.on_done(agent_id, deps.kinds.get(agent_id, ""), status, round(seconds, 1))
    except Exception:  # noqa: BLE001 — the event is best-effort telemetry
        pass


def spawn_subagent_background(
    deps: SubagentDeps,
    *,
    agent_type: str,
    prompt: str,
    description: str = "",
    model_override: str | None = None,
) -> JobRecord:
    """Start a subagent and hand back its job handle without waiting for it.

    Synchronous on purpose: everything that can be refused is refused before
    the caller's tool result is written, and the only thing the caller gets
    back is a handle to something already running.

    Raises ``BackgroundUnavailable`` when this session has no owner for the
    task, and ``ValueError`` for every refusal a blocking spawn would raise
    plus the live-parallelism cap.
    """
    if not deps.background_available():
        raise BackgroundUnavailable(
            "this session cannot run detached subagent jobs (nothing here "
            "outlives the turn to own them)"
        )
    cap = deps.limits.max_parallel
    live = deps.running_jobs()
    if len(live) >= cap:
        names = ", ".join(j.agent_id for j in live)
        raise ValueError(
            f"background job limit reached ({cap} running at once: {names}). "
            "Collect one with agent_result before starting another, or spawn "
            "this one without background."
        )

    agent_id, child = _prepare_child(
        deps, agent_type=agent_type, model_override=model_override
    )
    job = JobRecord(
        agent_id=agent_id, agent_type=agent_type, description=description
    )
    deps.jobs[agent_id] = job
    job.task = asyncio.ensure_future(_run_job(deps, job, child, prompt))
    deps.adopt_task(job.task)
    return job


async def _run_job(
    deps: SubagentDeps, job: JobRecord, child: AgentInstance, prompt: str
) -> None:
    """One detached run: the same finishing path, then announce the result.

    Never raises anything but ``CancelledError`` — a background task that
    raises has nobody to catch it, and its traceback would surface as an
    unretrieved-exception warning long after the turn that started it.
    """
    try:
        status, report = await _run_and_finish(deps, job.agent_id, child, prompt)
    except asyncio.CancelledError:
        job.finish(CANCELLED, sanitize_report(
            "[did not finish] the background job was cancelled."
        ))
        _announce(deps, job)
        raise
    except Exception as e:  # noqa: BLE001 — a job failure must not escape
        job.finish(ERROR, sanitize_report(
            f"[did not finish] the background job errored: {e}"
        ))
        _announce(deps, job)
        return
    job.finish(status, report)
    _announce(deps, job)


def _announce(deps: SubagentDeps, job: JobRecord) -> None:
    """Tell the UI and the spawner that a job reached a terminal state.

    Both halves are isolated: telemetry and a queued reminder are the two
    things least entitled to break a job that has already done its work.
    """
    _announce_done(deps, job.agent_id, job.status, job.seconds())
    if deps.owner is not None:
        try:
            deps.owner.queue_reminder(job.reminder())
        except Exception:  # noqa: BLE001 — so is the nudge
            pass


async def _run_and_finish(
    deps: SubagentDeps, agent_id: str, child: AgentInstance, message: str
) -> tuple[str, str]:
    """Drive one turn on ``child`` and post-process the result: exception
    safety, "[did not finish]" tagging, artifact offload, and sanitization.

    Shared by ``spawn_subagent`` (first turn) and ``resume_subagent`` (any
    later turn) so both go through identical treatment.

    Returns ``(status, report)``. The status is the same vocabulary a
    ``JobRecord`` uses, so how a run ended is decided once, here, rather than
    once per caller -- a child that raised reported ``done`` for as long as
    that decision lived in ``_run_job``. An empty report stays ``done``: the
    "[did not finish]" tag says the model produced nothing, not that the
    machinery failed.
    """
    deps.turns[agent_id] = deps.turns.get(agent_id, 0) + 1
    try:
        report = await child.run_turn(message)
    except Exception as e:  # a child failure must not crash the parent's loop
        return ERROR, sanitize_report(f"[did not finish] subagent errored: {e}")

    status = CANCELLED if child.cancelled else DONE
    if child.cancelled or not report.strip():
        report = report or "(no output)"
        report = f"[did not finish]\n{report}"
    report = maybe_offload(deps.cwd, agent_id, report)
    return status, sanitize_report(report)


async def resume_subagent(
    deps: SubagentDeps, *, agent_id: str, message: str
) -> tuple[str, str]:
    """Send a follow-up message to a previously spawned, now-idle subagent,
    resuming it with its full history intact. Returns ``(agent_id, report, status)``.

    Raises ValueError for an unknown agent_id or if the agent is still mid-turn
    — the tool wrapper turns those into an error ToolResult.
    """
    child = deps.roster.get(agent_id)
    if child is None:
        known = ", ".join(deps.roster) or "(none)"
        raise ValueError(f"unknown agent_id '{agent_id}'. Known: {known}")
    if child.busy:
        raise ValueError(f"agent '{agent_id}' is still running")

    # ``max_turns`` is the child's delegation budget: one turn for the spawn,
    # one per resume. It was stored and rendered and read nowhere until now, so
    # a definition asking for a small agent got an unbounded one.
    budget = deps.budgets.get(agent_id)
    spent = deps.turns.get(agent_id, 0)
    if budget is not None and spent >= budget:
        raise ValueError(
            f"agent '{agent_id}' has used its {budget}-turn budget. Spawn a "
            "fresh agent if there is more to do."
        )

    status, report = await _blocking_run(deps, agent_id, child, message)
    return agent_id, report, status
