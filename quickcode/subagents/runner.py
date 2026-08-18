"""Spawn and drive a subagent to completion, returning a sanitized report.

A subagent is the same ``AgentInstance`` as everything else — the differences
are a fresh prompt/history, a bounded toolset, a capped permission mode, and an
auto-deny permission callback (a headless child cannot prompt the user).
"""

from __future__ import annotations

import itertools
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
from quickcode.kernel.composition import MODE_PRIVILEGE, Resolved, cap_mode
from quickcode.kernel.resolve import resolve_composition
from quickcode.prompts.subagent import render_subagent_prompt
from quickcode.providers.base import Provider
from quickcode.subagents.artifacts import maybe_offload
from quickcode.subagents.definitions import AgentDef, load_defs
from quickcode.tools.base import ReadRegistry, ToolCtx
from quickcode.tools.registry import ToolRegistry, build_registry, core_tools

if TYPE_CHECKING:
    from quickcode.core.agent import EventBus

MAX_DEPTH = 2
MAX_AGENTS = 50

# Kept as module names because callers import them from here. The definitions
# moved into ``kernel/composition.py`` so the resolver can narrow a ceiling
# without importing the runtime that calls it.
_PRIV = MODE_PRIVILEGE

__all__ = [
    "MAX_AGENTS",
    "MAX_DEPTH",
    "SubagentDeps",
    "cap_mode",
    "resume_subagent",
    "sanitize_report",
    "spawn_subagent",
]


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
    # UI hook: called synchronously at spawn with (agent_id, definition name,
    # the child's EventBus) so a live pane can subscribe to the child's stream.
    # Optional — headless runs leave it None.
    on_pane: Callable[[str, str, EventBus], None] | None = None
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
            on_pane=self.on_pane,
            tool_pool=self.tool_pool if tool_pool is None else tool_pool,
            allowed_agents=self.allowed_agents,
            pool=self.pool,
            parent=parent if parent is not None else self.parent,
            defs=self.defs,
            preset=self.preset,
            turns=self.turns,
            budgets=self.budgets,
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
    before it enters the parent's context, and mark it as sanitized."""
    for tag in ("system-reminder", "task", "objective", "context", "boundaries"):
        text = text.replace(f"<{tag}>", f"‹{tag}›").replace(f"</{tag}>", f"‹/{tag}›")
    text = text.replace("<system-reminder", "‹system-reminder")
    return "[quickcode: sanitized subagent report]\n" + text.strip()


async def spawn_subagent(
    deps: SubagentDeps,
    *,
    agent_type: str,
    prompt: str,
    model_override: str | None = None,
) -> tuple[str, str]:
    """Run one subagent to completion. Returns ``(agent_id, sanitized_report)``.

    Raises ValueError for an unknown agent_type or when a spawn limit is hit —
    the tool wrapper turns those into an error ToolResult.
    """
    if len(deps.spawned) >= MAX_AGENTS:
        raise ValueError(f"subagent limit reached ({MAX_AGENTS} per conversation)")

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
        max_depth=MAX_DEPTH,
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
    )
    # Registered immediately so the agent is resumable via send_message even if
    # this first run errors out below.
    deps.roster[agent_id] = child

    # Surface a live pane before the child starts streaming. A UI failure here
    # must never break the subagent run, so it is fully isolated.
    if deps.on_pane is not None:
        try:
            deps.on_pane(agent_id, defn.name, child.bus)
        except Exception:  # noqa: BLE001 — the pane is best-effort telemetry
            pass

    report = await _run_and_finish(deps, agent_id, child, prompt)
    return agent_id, report


async def _run_and_finish(
    deps: SubagentDeps, agent_id: str, child: AgentInstance, message: str
) -> str:
    """Drive one turn on ``child`` and post-process the result: exception
    safety, "[did not finish]" tagging, artifact offload, and sanitization.

    Shared by ``spawn_subagent`` (first turn) and ``resume_subagent`` (any
    later turn) so both go through identical treatment.
    """
    deps.turns[agent_id] = deps.turns.get(agent_id, 0) + 1
    try:
        report = await child.run_turn(message)
    except Exception as e:  # a child failure must not crash the parent's loop
        return sanitize_report(f"[did not finish] subagent errored: {e}")

    if child.cancelled or not report.strip():
        report = report or "(no output)"
        report = f"[did not finish]\n{report}"
    report = maybe_offload(deps.cwd, agent_id, report)
    return sanitize_report(report)


async def resume_subagent(
    deps: SubagentDeps, *, agent_id: str, message: str
) -> tuple[str, str]:
    """Send a follow-up message to a previously spawned, now-idle subagent,
    resuming it with its full history intact. Returns ``(agent_id, sanitized_report)``.

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

    report = await _run_and_finish(deps, agent_id, child, message)
    return agent_id, report
