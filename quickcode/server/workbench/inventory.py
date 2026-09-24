"""The workbench's agent list."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from quickcode.kernel import preset as preset_module
from quickcode.kernel.composition import ORCHESTRATOR_ID
from quickcode.kernel.orchestrator import resolve_orchestrator
from quickcode.kernel.resolve import resolve_composition, runtime_limits, session_pool
from quickcode.server.manager import ConversationManager
from quickcode.server.workbench.view import identity
from quickcode.subagents.definitions import load_defs


def agents_payload(manager: ConversationManager) -> dict[str, Any]:
    """Every agent identity, ``@orchestrator`` first and first-class.

    The orchestrator is not a definition on disk, which is exactly why it has to
    appear here: an inventory that lists the spawnable agents and omits the one
    you are talking to answers the wrong question.
    """
    cwd = Path(manager.cwd)
    preset = preset_module.resolve(cwd)
    defs = load_defs(cwd)
    pool = session_pool(cwd, list(manager.registry_factory().tools.values()))
    limits = runtime_limits(cwd)

    orchestrator = resolve_orchestrator(
        pool=pool, preset=preset, defs=defs, cwd=cwd, max_depth=limits.max_depth,
        resolve_model=manager.resolve_role,
    )
    rows = [{
        **identity(ORCHESTRATOR_ID, None, preset),
        "tool_count": len(orchestrator.tools),
        "denied_count": len(orchestrator.denied_tools),
        "model": orchestrator.model or (
            manager.config.last_model or manager.config.profile.resolve("orchestrator")
        ),
        "ceiling": orchestrator.ceiling.value,
        "spawns": list(orchestrator.spawns),
        "problems": len(orchestrator.problems),
    }]
    for agent_id in sorted(k for k in defs if k != ORCHESTRATOR_ID):
        child = resolve_composition(
            agent_id, pool=pool, preset=preset, defs=defs, cwd=cwd,
            parent=orchestrator, depth=0, max_depth=limits.max_depth,
            resolve_model=manager.resolve_role,
        )
        rows.append({
            **identity(agent_id, defs[agent_id], preset),
            "tool_count": len(child.tools),
            "denied_count": len(child.denied_tools),
            "model": child.model,
            "ceiling": child.ceiling.value,
            "spawns": list(child.spawns),
            "problems": len(child.problems),
        })
    return {"agents": rows, "preset": preset.id, "preset_title": preset.title}
