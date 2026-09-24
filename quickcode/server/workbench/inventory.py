"""The workbench's agent list."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from quickcode.kernel import preset as preset_module
from quickcode.kernel.composition import ORCHESTRATOR_ID
from quickcode.server.manager import ConversationManager
from quickcode.server.workbench.resolution import live_inputs, resolve_under
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
    inputs = live_inputs(manager, preset=preset, defs=defs)
    orchestrator, _, _ = resolve_under(manager, ORCHESTRATOR_ID, "", inputs)
    under_orchestrator = replace(inputs, orchestrator=orchestrator)
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
        child, _, _ = resolve_under(manager, agent_id, "", under_orchestrator)
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
