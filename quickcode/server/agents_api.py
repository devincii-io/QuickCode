"""The agent workbench's backend: what an agent gets, and why it does not get
the rest.

Three ideas hold this module together.

**One computation, never two.** ``/resolved`` and ``/preview`` call
``resolve_composition`` -- the same function ``manager.open()`` and
``spawn_subagent`` call -- and render the prompt through
``prompts/system.render_with_sections`` or ``prompts/subagent`` -- the same code
the runner renders with. Nothing here reconstructs a prompt or a tool list. A
reconstruction drifts, and a preview that drifts is worse than no preview,
because it is believed.

**Absences are answers.** A tool that is missing is listed with the reason it is
missing; a prompt section that did not render is listed with the reason it did
not. "You cannot see why you don't have it" is the failure this whole surface
exists to fix, so an omitted key is never the answer to "why".

**Live is labelled live.** A resolution against the current settings files says
``frozen: false``; a resolution read out of a running session's meta record says
``frozen: true`` and carries the digest it was recorded with. When the two
disagree the payload says so rather than picking one.

Routes are registered from here rather than from ``server/app.py`` following the
``server/gitinfo.py:register_git_routes`` precedent, so the app's diff is one
import and one call.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from starlette.routing import Mount

from quickcode.kernel import preset as preset_module
from quickcode.kernel.composition import (
    ORCHESTRATOR_ID,
    Composition,
)
from quickcode.server.manager import ConversationManager, SwitchRefused
from quickcode.server.workbench.drafts import preview_payload
from quickcode.server.workbench.inventory import agents_payload
from quickcode.server.workbench.view import resolved_payload

log = logging.getLogger("quickcode.server.agents")

JSON_BODY_CAP = 1024 * 1024


async def _read_json(request: Request, maximum: int = JSON_BODY_CAP) -> Any:
    """A JSON body, refused once it passes ``maximum`` bytes rather than after
    the whole request has been buffered.

    Local until ``server/http.py``'s shared reader is in this tree; the two
    differ only in the wording of their 413 and 400 details.
    """
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > maximum:
        raise HTTPException(413, "request body too large")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > maximum:
            raise HTTPException(413, "request body too large")
        chunks.append(chunk)
    raw = b"".join(chunks)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise HTTPException(400, f"malformed JSON: {exc}") from exc


# --------------------------------------------------------------------------
# handlers
# --------------------------------------------------------------------------

def _composition_write(
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


def _composition_id(name: str) -> str:
    """``"Review only"`` -> ``review-only``: the id a typed name is stored under,
    by the same rules an authored plugin's file name follows."""
    text = re.sub(r"[^a-z0-9_-]+", "-", name.strip().lower()).strip("-")
    text = re.sub(r"-{2,}", "-", text)
    if text and not text[0].isalpha():
        text = f"c-{text}"
    return text[:48]


def _derive(manager: ConversationManager, preset_id: str, body: Any) -> dict[str, Any]:
    """Duplicate a composition into a project-scoped one you own.

    This is the on-ramp the switcher's last entry uses: most people discover
    they want a custom composition at the moment an existing one is nearly
    right.
    """
    cwd = Path(manager.cwd)
    # As written, for the same reason as ``_composition_write``: the copy goes
    # into the project file, where the gate applies to it on every read anyway.
    presets = preset_module.load_presets(cwd, trusted=True)
    source = presets.get(preset_id)
    if source is None:
        raise HTTPException(404, f"no composition {preset_id!r}")
    wanted = str((body or {}).get("name") or "").strip() if isinstance(body, dict) else ""
    if wanted:
        # A name somebody typed is a name they meant: it becomes the id, and a
        # clash is refused rather than quietly numbered into a different one.
        new_id = _composition_id(wanted)
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


def _switch(manager: ConversationManager, conv_id: str, body: Any) -> dict[str, Any]:
    conv = manager.get(conv_id)
    if conv is None:
        raise HTTPException(404, f"no live conversation {conv_id!r}")
    preset_id = (body or {}).get("preset") if isinstance(body, dict) else None
    if not isinstance(preset_id, str) or not preset_id.strip():
        raise HTTPException(400, "body must be {'preset': <id>}")
    if preset_id not in preset_module.load_presets(Path(manager.cwd)):
        raise HTTPException(404, f"no composition {preset_id!r}")
    try:
        return conv.switch_composition(preset_id.strip())
    except SwitchRefused as exc:
        # 409, not 400: the request is valid and would be valid again in a
        # moment. Refusing with the reason is the whole contract -- a switch
        # that lands invisibly three seconds later is worse than one that does
        # not happen.
        raise HTTPException(409, str(exc)) from exc


# --------------------------------------------------------------------------
# registration
# --------------------------------------------------------------------------

def register_agent_routes(app: FastAPI, hub: Any) -> None:
    """Mount the workbench routes, unscoped and project-scoped alike.

    ``hub`` is a ``ProjectHub``: ``hub.default`` is the launch directory and
    ``hub.get(pid)`` is whichever project the UI is showing. Both shapes run one
    pair of handlers, matching the rest of the API.
    """

    def _project(pid: str) -> ConversationManager:
        manager = hub.get(pid)
        if manager is None:
            raise HTTPException(404, f"unknown project: {pid}")
        return manager

    # ---- inventory ----

    @app.get("/api/kernel/agents")
    def agents() -> dict:
        return agents_payload(hub.default)

    @app.get("/api/projects/{pid}/kernel/agents")
    def project_agents(pid: str) -> dict:
        return agents_payload(_project(pid))

    # ---- resolved ----

    @app.get("/api/kernel/agents/{agent_id}/resolved")
    def resolved(agent_id: str, preset: str = "", parent: str = "",
                 conv: str = "") -> dict:
        return resolved_payload(hub.default, agent_id, preset_id=preset,
                                 parent_id=parent, conv_id=conv)

    @app.get("/api/projects/{pid}/kernel/agents/{agent_id}/resolved")
    def project_resolved(pid: str, agent_id: str, preset: str = "",
                         parent: str = "", conv: str = "") -> dict:
        return resolved_payload(_project(pid), agent_id, preset_id=preset,
                                 parent_id=parent, conv_id=conv)

    # ---- preview ----

    @app.post("/api/kernel/agents/{agent_id}/preview")
    async def preview(agent_id: str, request: Request) -> dict:
        return preview_payload(hub.default, agent_id, await _read_json(request))

    @app.post("/api/projects/{pid}/kernel/agents/{agent_id}/preview")
    async def project_preview(pid: str, agent_id: str, request: Request) -> dict:
        return preview_payload(_project(pid), agent_id, await _read_json(request))

    # ---- saving a composition edit ----

    @app.put("/api/kernel/agents/{agent_id}/composition")
    async def write_composition(agent_id: str, request: Request) -> dict:
        return _composition_write(hub.default, agent_id, await _read_json(request))

    @app.put("/api/projects/{pid}/kernel/agents/{agent_id}/composition")
    async def project_write_composition(pid: str, agent_id: str,
                                        request: Request) -> dict:
        return _composition_write(_project(pid), agent_id, await _read_json(request))

    # ---- duplicate-to-customise ----

    @app.post("/api/kernel/compositions/{preset_id}/derive")
    async def derive(preset_id: str, request: Request) -> dict:
        return _derive(hub.default, preset_id, await _read_json(request))

    @app.post("/api/projects/{pid}/kernel/compositions/{preset_id}/derive")
    async def project_derive(pid: str, preset_id: str, request: Request) -> dict:
        return _derive(_project(pid), preset_id, await _read_json(request))

    # ---- session-scoped switching ----

    @app.post("/api/kernel/conversations/{conv_id}/composition")
    async def switch(conv_id: str, request: Request) -> dict:
        return _switch(hub.default, conv_id, await _read_json(request))

    @app.post("/api/projects/{pid}/kernel/conversations/{conv_id}/composition")
    async def project_switch(pid: str, conv_id: str, request: Request) -> dict:
        return _switch(_project(pid), conv_id, await _read_json(request))

    # The frontend is mounted at "/" and matches every path, so it has to stay
    # last whatever order this module is registered in. ``app.py`` calls us
    # before the mount; anything registering afterwards (a test building the app
    # first, an embedder) would otherwise get 405s from the static handler.
    # ``sort`` is stable, so this only moves the catch-all mounts.
    app.router.routes.sort(key=lambda r: isinstance(r, Mount) and r.path in ("", "/"))
