"""The agent workbench's routes: ``/api/kernel/agents/...`` and the composition
edits beside them, unscoped and project-scoped.

The handlers are thin: what they answer is computed in ``server/workbench/``.
Routes are registered from here rather than from ``server/app.py`` following the
``server/gitinfo.py:register_git_routes`` precedent, so the app's diff is one
import and one call.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from starlette.routing import Mount

from quickcode.server import http
from quickcode.server.manager import ConversationManager
from quickcode.server.workbench.compositions import (
    derive_composition,
    save_composition,
    switch_conversation,
)
from quickcode.server.workbench.drafts import preview_payload
from quickcode.server.workbench.inventory import agents_payload
from quickcode.server.workbench.view import resolved_payload

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
        return http.project(hub, pid)

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
        return preview_payload(hub.default, agent_id, await http.read_json(request))

    @app.post("/api/projects/{pid}/kernel/agents/{agent_id}/preview")
    async def project_preview(pid: str, agent_id: str, request: Request) -> dict:
        return preview_payload(_project(pid), agent_id, await http.read_json(request))

    # ---- saving a composition edit ----

    @app.put("/api/kernel/agents/{agent_id}/composition")
    async def write_composition(agent_id: str, request: Request) -> dict:
        return save_composition(hub.default, agent_id, await http.read_json(request))

    @app.put("/api/projects/{pid}/kernel/agents/{agent_id}/composition")
    async def project_write_composition(pid: str, agent_id: str,
                                        request: Request) -> dict:
        return save_composition(_project(pid), agent_id, await http.read_json(request))

    # ---- duplicate-to-customise ----

    @app.post("/api/kernel/compositions/{preset_id}/derive")
    async def derive(preset_id: str, request: Request) -> dict:
        return derive_composition(hub.default, preset_id, await http.read_json(request))

    @app.post("/api/projects/{pid}/kernel/compositions/{preset_id}/derive")
    async def project_derive(pid: str, preset_id: str, request: Request) -> dict:
        return derive_composition(_project(pid), preset_id, await http.read_json(request))

    # ---- session-scoped switching ----

    @app.post("/api/kernel/conversations/{conv_id}/composition")
    async def switch(conv_id: str, request: Request) -> dict:
        return switch_conversation(hub.default, conv_id, await http.read_json(request))

    @app.post("/api/projects/{pid}/kernel/conversations/{conv_id}/composition")
    async def project_switch(pid: str, conv_id: str, request: Request) -> dict:
        return switch_conversation(_project(pid), conv_id, await http.read_json(request))

    # The frontend is mounted at "/" and matches every path, so it has to stay
    # last whatever order this module is registered in. ``app.py`` calls us
    # before the mount; anything registering afterwards (a test building the app
    # first, an embedder) would otherwise get 405s from the static handler.
    # ``sort`` is stable, so this only moves the catch-all mounts.
    app.router.routes.sort(key=lambda r: isinstance(r, Mount) and r.path in ("", "/"))
