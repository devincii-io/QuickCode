"""The agent workbench's routes: ``/api/kernel/agents/...`` and the composition
edits beside them, unscoped and project-scoped.

The handlers are thin: what they answer is computed in ``server/workbench/``.
Routes are registered from here rather than from ``server/app.py`` following the
``server/gitinfo.py:register_git_routes`` precedent, so the app's diff is one
import and one call.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from starlette.routing import Mount

from quickcode.server.manager import ConversationManager
from quickcode.server.workbench.compositions import (
    derive_composition,
    save_composition,
    switch_conversation,
)
from quickcode.server.workbench.drafts import preview_payload
from quickcode.server.workbench.inventory import agents_payload
from quickcode.server.workbench.view import resolved_payload

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
        return save_composition(hub.default, agent_id, await _read_json(request))

    @app.put("/api/projects/{pid}/kernel/agents/{agent_id}/composition")
    async def project_write_composition(pid: str, agent_id: str,
                                        request: Request) -> dict:
        return save_composition(_project(pid), agent_id, await _read_json(request))

    # ---- duplicate-to-customise ----

    @app.post("/api/kernel/compositions/{preset_id}/derive")
    async def derive(preset_id: str, request: Request) -> dict:
        return derive_composition(hub.default, preset_id, await _read_json(request))

    @app.post("/api/projects/{pid}/kernel/compositions/{preset_id}/derive")
    async def project_derive(pid: str, preset_id: str, request: Request) -> dict:
        return derive_composition(_project(pid), preset_id, await _read_json(request))

    # ---- session-scoped switching ----

    @app.post("/api/kernel/conversations/{conv_id}/composition")
    async def switch(conv_id: str, request: Request) -> dict:
        return switch_conversation(hub.default, conv_id, await _read_json(request))

    @app.post("/api/projects/{pid}/kernel/conversations/{conv_id}/composition")
    async def project_switch(pid: str, conv_id: str, request: Request) -> dict:
        return switch_conversation(_project(pid), conv_id, await _read_json(request))

    # The frontend is mounted at "/" and matches every path, so it has to stay
    # last whatever order this module is registered in. ``app.py`` calls us
    # before the mount; anything registering afterwards (a test building the app
    # first, an embedder) would otherwise get 405s from the static handler.
    # ``sort`` is stable, so this only moves the catch-all mounts.
    app.router.routes.sort(key=lambda r: isinstance(r, Mount) and r.path in ("", "/"))
