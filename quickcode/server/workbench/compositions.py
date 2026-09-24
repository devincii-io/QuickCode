"""Editing compositions from the workbench: saving an edit, duplicating one to
customise it, and switching a running session onto another.
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from quickcode.kernel import preset as preset_module
from quickcode.kernel.composition import ORCHESTRATOR_ID, Composition
from quickcode.server.manager import ConversationManager, SwitchRefused


def save_composition(
    manager: ConversationManager, agent_id: str, body: Any,
) -> dict[str, Any]:
    """Save a composition edit into a project-scoped preset.

    Refused on a built-in composition with the reason and the recourse, rather
    than silently forking one: which composition a session runs is a name the
    user chose, and a write that quietly changes what that name means is worse
    than a refusal.
    """
    if not isinstance(body, dict) or not isinstance(body.get("composition"), dict):
        raise HTTPException(400, "body must be {'composition': {...}}")
    cwd = Path(manager.cwd)
    # Read as the file states it, not as the trust gate lets a session obey it:
    # this is written back, and a gated field left out of the write would be
    # erased from the file rather than merely ignored.
    preset = preset_module.resolve(cwd, str(body.get("preset") or ""), trusted=True)
    if preset.builtin:
        raise HTTPException(
            409,
            f"“{preset.title}” is a built-in composition and cannot be edited. "
            f"Duplicate it into this project first.",
        )
    incoming = body["composition"]
    if agent_id == ORCHESTRATOR_ID:
        merged = Composition.from_dict({**preset.orchestrator.to_dict(), **incoming})
        updated = replace(preset, orchestrator=merged)
    else:
        agents = dict(preset.agents)
        current = agents.get(agent_id, Composition())
        agents[agent_id] = Composition.from_dict({**current.to_dict(), **incoming})
        updated = replace(preset, agents=agents)
    preset_module.save_preset(cwd, updated)
    return {
        "preset": updated.id,
        "applies_to": "new sessions, and any running session you switch",
        "composition": (updated.orchestrator if agent_id == ORCHESTRATOR_ID
                        else updated.agents[agent_id]).to_dict(),
    }


def composition_id(name: str) -> str:
    """``"Review only"`` -> ``review-only``: the id a typed name is stored under,
    by the same rules an authored plugin's file name follows."""
    text = re.sub(r"[^a-z0-9_-]+", "-", name.strip().lower()).strip("-")
    text = re.sub(r"-{2,}", "-", text)
    if text and not text[0].isalpha():
        text = f"c-{text}"
    return text[:48]


def derive_composition(manager: ConversationManager, preset_id: str, body: Any) -> dict[str, Any]:
    """Duplicate a composition into a project-scoped one you own.

    This is the on-ramp the switcher's last entry uses: most people discover
    they want a custom composition at the moment an existing one is nearly
    right.
    """
    cwd = Path(manager.cwd)
    # As written, for the same reason as ``save_composition``: the copy goes
    # into the project file, where the gate applies to it on every read anyway.
    presets = preset_module.load_presets(cwd, trusted=True)
    source = presets.get(preset_id)
    if source is None:
        raise HTTPException(404, f"no composition {preset_id!r}")
    wanted = str((body or {}).get("name") or "").strip() if isinstance(body, dict) else ""
    if wanted:
        # A name somebody typed is a name they meant: it becomes the id, and a
        # clash is refused rather than quietly numbered into a different one.
        new_id = composition_id(wanted)
        if not new_id:
            raise HTTPException(400, (
                f"{wanted!r} is not a usable composition name: it needs at least "
                "one letter or digit"))
        if new_id in presets:
            raise HTTPException(409, (
                f"a composition called {new_id!r} already exists"
                + (" and is built in" if presets[new_id].builtin else "")
                + " — pick another name, or open that one"))
    else:
        new_id = f"{preset_id}-copy"
        if new_id in presets:
            n = 2
            while f"{new_id}-{n}" in presets:
                n += 1
            new_id = f"{new_id}-{n}"
    copy = replace(
        source,
        id=new_id,
        title=f"{source.title} (yours)" if not wanted else wanted,
        description=source.description or f"Derived from {source.title}.",
        builtin=False,
    )
    preset_module.save_preset(cwd, copy)
    return {"id": new_id, "title": copy.title, "derived_from": preset_id,
            "path": str(preset_module.project_settings_path(cwd))}


def switch_conversation(manager: ConversationManager, conv_id: str, body: Any) -> dict[str, Any]:
    conv = manager.get(conv_id)
    if conv is None:
        raise HTTPException(404, f"no live conversation {conv_id!r}")
    preset_id = (body or {}).get("preset") if isinstance(body, dict) else None
    if not isinstance(preset_id, str) or not preset_id.strip():
        raise HTTPException(400, "body must be {'preset': <id>}")
    preset_id = preset_id.strip()
    if preset_id not in preset_module.load_presets(Path(manager.cwd)):
        raise HTTPException(404, f"no composition {preset_id!r}")
    try:
        return conv.switch_composition(preset_id)
    except SwitchRefused as exc:
        # 409, not 400: the request is valid and would be valid again in a
        # moment. Refusing with the reason is the whole contract -- a switch
        # that lands invisibly three seconds later is worse than one that does
        # not happen.
        raise HTTPException(409, str(exc)) from exc
