"""Spawn and drive a subagent to completion, returning a sanitized report.

A subagent is the same ``AgentInstance`` as everything else — the differences
are a fresh prompt/history, a bounded toolset, a capped permission mode, and an
auto-deny permission callback (a headless child cannot prompt the user).
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from quickcode.config import Environment, Profile
from quickcode.core.agent import (
    AgentInstance,
    PermissionOutcome,
    PermissionRequest,
)
from quickcode.core.history import History
from quickcode.core.permissions import Mode, PermissionEngine, Rules
from quickcode.prompts.subagent import render_subagent_prompt
from quickcode.providers.base import Provider
from quickcode.subagents.definitions import AgentDef, load_defs
from quickcode.tools.base import ReadRegistry, ToolCtx
from quickcode.tools.registry import ToolRegistry, build_registry

if TYPE_CHECKING:
    from quickcode.core.agent import EventBus

MAX_DEPTH = 2
MAX_AGENTS = 50

# plan < ask < auto-edit < dontask < yolo (least → most privileged).
_PRIV = {Mode.plan: 0, Mode.ask: 1, Mode.auto_edit: 2, Mode.dontask: 3, Mode.yolo: 4}


def cap_mode(parent: Mode, cap: Mode) -> Mode:
    """effective = min(parent, cap); plan collapses to ask (headless children
    don't do the interactive plan-review dance)."""
    eff = parent if _PRIV[parent] <= _PRIV[cap] else cap
    return Mode.ask if eff == Mode.plan else eff


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
    # UI hook: called synchronously at spawn with (agent_id, definition name,
    # the child's EventBus) so a live pane can subscribe to the child's stream.
    # Optional — headless runs leave it None.
    on_pane: Callable[[str, str, EventBus], None] | None = None

    def child(self, depth: int, effective_mode: Mode) -> SubagentDeps:
        """A deps object for the next level down, sharing the counter/roster.

        A child's own spawns are capped by the child's fixed effective mode.
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
            on_pane=self.on_pane,
        )


async def _deny_cb(_req: PermissionRequest) -> PermissionOutcome:
    return PermissionOutcome(
        allow=False,
        deny_message=(
            "A subagent cannot prompt the user for permission. This action needs "
            "a mode that allows it without asking, or the parent must do it."
        ),
    )


def _resolve_model(deps: SubagentDeps, defn: AgentDef, override: str | None) -> str:
    if override:
        return override
    spec = defn.model
    if spec == "worker":
        return deps.profile.resolve("worker")
    if spec == "orchestrator":
        return deps.profile.resolve("orchestrator")
    return spec  # explicit slug


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

    defs = load_defs(deps.cwd)
    defn = defs.get(agent_type)
    if defn is None:
        raise ValueError(
            f"unknown agent_type '{agent_type}'. Available: {', '.join(sorted(defs))}"
        )

    child_depth = deps.depth + 1
    agent_id = f"{defn.name}-{next(deps.counter)}"
    deps.spawned.append(agent_id)

    model = _resolve_model(deps, defn, model_override)
    effective_mode = cap_mode(deps.mode_getter(), defn.mode_cap)

    # Build the child's bounded registry. The agent tool is only granted if the
    # child is still above the depth floor, so nesting stops at MAX_DEPTH.
    include_agent = child_depth < MAX_DEPTH
    child_deps = deps.child(child_depth, effective_mode) if include_agent else None
    registry: ToolRegistry = build_registry(defn.tools, include_agent=include_agent)

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

    # Surface a live pane before the child starts streaming. A UI failure here
    # must never break the subagent run, so it is fully isolated.
    if deps.on_pane is not None:
        try:
            deps.on_pane(agent_id, defn.name, child.bus)
        except Exception:  # noqa: BLE001 — the pane is best-effort telemetry
            pass

    try:
        report = await child.run_turn(prompt)
    except Exception as e:  # a child failure must not crash the parent's loop
        return agent_id, sanitize_report(f"[did not finish] subagent errored: {e}")

    if child.cancelled or not report.strip():
        report = report or "(no output)"
        report = f"[did not finish]\n{report}"
    return agent_id, sanitize_report(report)
