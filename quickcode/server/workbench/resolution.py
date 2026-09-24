"""What one agent is resolved against, and resolving it there.

A live view resolves against the settings files as they are now; a view of a
running session resolves against what that session snapshotted when it opened.
Either way the call is ``resolve_composition`` -- the one ``manager.open()`` and
``spawn_subagent`` make -- under the parent and at the depth the runner uses.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from quickcode.kernel import preset as preset_module
from quickcode.kernel.composition import ORCHESTRATOR_ID, Resolved
from quickcode.kernel.orchestrator import resolve_orchestrator
from quickcode.kernel.resolve import resolve_composition, runtime_limits, session_pool
from quickcode.server.manager import ConversationManager
from quickcode.subagents.definitions import AgentDef


@dataclass(frozen=True)
class Inputs:
    """Everything one resolution reads that a session also snapshots."""

    preset: Any
    defs: dict[str, AgentDef]
    pool: list[Any]
    max_depth: int
    # The orchestrator's composition to resolve a subagent under. None means
    # "resolve it from ``preset``", which is what a live view does; a session
    # hands its frozen one.
    orchestrator: Resolved | None = None


def live_inputs(
    manager: ConversationManager, *, preset: Any, defs: dict[str, AgentDef],
    max_depth: int | None = None,
) -> Inputs:
    """What an agent resolves against right now: the settings files as they
    are, and this install's pool as a session opened now would see it."""
    cwd = Path(manager.cwd)
    return Inputs(
        preset=preset, defs=defs,
        pool=session_pool(cwd, list(manager.registry_factory().tools.values())),
        max_depth=runtime_limits(cwd).max_depth if max_depth is None else max_depth,
    )


def session_inputs(conv: Any) -> Inputs | None:
    """What a spawn in this running session resolves against.

    The session snapshotted its preset, its definitions and its pool at open,
    and froze its orchestrator. Resolving a subagent "for this session" off the
    files instead would describe an agent the session cannot spawn.
    """
    deps = conv.agent.ctx.extra.get("subagent")
    if deps is None:
        return None
    return Inputs(
        preset=deps.preset if deps.preset is not None else preset_module.resolve(
            Path(conv.manager.cwd), conv.preset_id),
        defs=deps.definitions(),
        pool=deps.session_pool(),
        max_depth=deps.limits.max_depth,
        orchestrator=conv.resolved,
    )


def resolve_under(
    manager: ConversationManager, agent_id: str, parent_id: str, inputs: Inputs,
) -> tuple[Resolved, Resolved | None, int]:
    """``agent_id`` resolved the way the runner resolves it: under a parent, at
    the spawner's depth. Depth 0 is where the pool carve-out lives, which is
    what makes "the orchestrator may not edit files, but its children may"
    expressible at all."""
    shared: dict[str, Any] = {
        "pool": inputs.pool, "preset": inputs.preset, "defs": inputs.defs,
        "cwd": Path(manager.cwd), "max_depth": inputs.max_depth,
        "resolve_model": manager.resolve_role,
    }
    if agent_id == ORCHESTRATOR_ID:
        return resolve_orchestrator(**shared), None, 0
    parent = inputs.orchestrator or resolve_orchestrator(**shared)
    depth = 0
    if parent_id and parent_id != ORCHESTRATOR_ID:
        parent = resolve_composition(parent_id, parent=parent, depth=0, **shared)
        depth = 1
    return resolve_composition(agent_id, parent=parent, depth=depth, **shared), parent, depth
