"""Previewing an unsaved edit.

The draft rides on top of the saved agent as a real ``AgentDef`` or preset, so
it travels through exactly the resolver and prompt renderer a saved one does.
Nothing is written.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from quickcode.kernel import preset as preset_module
from quickcode.kernel.composition import ORCHESTRATOR_ID, Composition
from quickcode.server.manager import ConversationManager
from quickcode.server.workbench.view import resolve_view
from quickcode.subagents.definitions import AgentDef, load_defs


def _draft_defs(
    defs: dict[str, AgentDef], agent_id: str, comp: dict[str, Any] | None,
    body: str | None,
) -> dict[str, AgentDef]:
    """A definitions snapshot with one agent replaced by an unsaved draft.

    The draft is a real ``AgentDef`` so it travels through exactly the same
    resolver and the same prompt renderer as a saved one. Nothing is written.
    """
    base = defs.get(agent_id)
    if base is None:
        return defs
    composition = base.composition
    if comp:
        composition = Composition.from_dict({**base.composition.to_dict(), **comp})
    out = dict(defs)
    out[agent_id] = AgentDef(
        name=base.name,
        description=base.description,
        role=base.role,
        composition=composition,
        source=base.source,
        path=base.path,
        prompt_body=base.prompt_body if body is None else body,
        isolation=base.isolation,
    )
    return out


def _draft_preset(preset: Any, comp: dict[str, Any] | None) -> Any:
    if not comp:
        return preset
    merged = Composition.from_dict({**preset.orchestrator.to_dict(), **comp})
    return replace(preset, orchestrator=merged)


def preview_payload(
    manager: ConversationManager, agent_id: str, body: Any,
) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise HTTPException(400, "request body must be a JSON object")
    comp = body.get("composition")
    if comp is not None and not isinstance(comp, dict):
        raise HTTPException(400, "composition must be an object")
    prompt_body = body.get("prompt_body")
    if prompt_body is not None and not isinstance(prompt_body, str):
        raise HTTPException(400, "prompt_body must be a string")

    cwd = Path(manager.cwd)
    preset = preset_module.resolve(cwd, str(body.get("preset") or ""))
    defs = load_defs(cwd)
    if agent_id == ORCHESTRATOR_ID:
        preset = _draft_preset(preset, comp)
    else:
        if agent_id not in defs:
            raise HTTPException(404, f"no agent {agent_id!r} in this project")
        defs = _draft_defs(defs, agent_id, comp, prompt_body)

    payload = resolve_view(
        manager, agent_id, preset=preset, defs=defs,
        parent_id=str(body.get("parent") or ""),
    )
    # A preview resolves against the current files with an unsaved draft on top,
    # so it is live by construction and says so. It is never frozen and it never
    # writes.
    payload["frozen"] = False
    payload["live"] = True
    payload["draft"] = True
    return payload
