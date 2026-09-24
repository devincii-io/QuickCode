"""The project registry: which directories QuickCode knows, opening one,
forgetting one (or its data), browsing for one, and the trust gate on each.

These routes address a project by id rather than through its manager -- a
registered project need not be open -- so the unscoped ``/api/data`` and
``/api/trust`` shapes pass the hub's default id where the others pass ``pid``.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request

from quickcode.server.http import project, read_json
from quickcode.server.projects import ProjectBusyError, ProjectHub, list_dirs
from quickcode.session.store import SESSIONS_DIRNAME


def _session_count(root: Path) -> int:
    sessions_dir = root / SESSIONS_DIRNAME
    try:
        return sum(1 for _ in sessions_dir.glob("*.jsonl"))
    except OSError:
        return 0


def register_project_routes(app: FastAPI, hub: ProjectHub) -> None:
    # ---- project registry + browsing ----

    @app.get("/api/projects")
    def projects() -> dict:
        out = []
        for entry in hub.registry.list():
            manager = hub.get(entry.id)
            out.append(
                {
                    "id": entry.id,
                    "path": str(entry.path),
                    "name": entry.name,
                    "last_opened": entry.last_opened,
                    "session_count": _session_count(entry.path),
                    "live_sessions": len(manager.conversations) if manager else 0,
                }
            )
        return {"home": str(Path.home()), "projects": out}

    @app.post("/api/projects/open")
    async def open_project(request: Request) -> dict:
        body = await read_json(request)
        path = body.get("path") if isinstance(body, dict) else None
        if not isinstance(path, str) or not path.strip():
            raise HTTPException(400, "body must be {'path': <directory>}")
        try:
            manager = await hub.open(path.strip())
        except NotADirectoryError as e:
            raise HTTPException(400, f"not a directory: {e}") from e
        # The response shape is a stable contract; the UI learns what was left
        # inert by calling GET /api/projects/{id}/trust right after open.
        return {
            "id": hub.id_of(manager),
            "path": str(manager.cwd),
            "name": manager.cwd.name,
        }

    # ---- forgetting a project: the list entry, or QuickCode's data for it ----
    #
    # Two clearly separate acts, never one route with a mood:
    #
    #   DELETE /api/projects/{pid}        drops the registry entry. Nothing
    #                                     inside the project is touched.
    #   DELETE /api/projects/{pid}/data   additionally deletes
    #                                     <project>/.quickcode and the project's
    #                                     entry in the trust store.
    #
    # Neither ever removes the project directory itself, and neither can reach
    # anything outside <project>/.quickcode — the containment proof lives in
    # session.store.project_data_dir and raises rather than guessing, which
    # surfaces here as a 400.
    #
    # Both refuse a project with live conversations (409), which is the answer
    # DELETE /api/sessions/{conv_id} already gives for a live session.

    async def _forget(pid: str, *, purge_data: bool) -> dict:
        try:
            return await hub.forget(pid, purge_data=purge_data)
        except KeyError as e:
            raise HTTPException(404, f"unknown project: {pid}") from e
        except ProjectBusyError as e:
            raise HTTPException(409, str(e)) from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except OSError as e:
            raise HTTPException(500, f"could not delete project data: {e}") from e

    def _data_summary(pid: str) -> dict:
        try:
            return {"id": pid, "project": str(hub.path_of(pid)), **hub.data_summary(pid)}
        except KeyError as e:
            raise HTTPException(404, f"unknown project: {pid}") from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    async def _forget_many(request: Request, *, purge_data: bool) -> dict:
        """Remove a selection, reporting each id that could not be removed.

        Same contract as the bulk session delete: what worked is in ``removed``,
        what did not is in ``skipped`` with a reason, and the response is a 200
        either way. A bulk action that failed whole on the first live project
        would leave the user guessing which of ten rows went through.
        """
        body = await read_json(request)
        ids = body.get("ids") if isinstance(body, dict) else None
        if not isinstance(ids, list) or not ids:
            raise HTTPException(400, "body must be {'ids': [<project id>, …]}")
        if len(ids) > 200:
            raise HTTPException(400, "too many projects in one request")
        removed: list[dict] = []
        skipped: list[dict] = []
        for raw in ids:
            if not isinstance(raw, str):
                raise HTTPException(400, f"invalid project id: {raw!r}")
            try:
                removed.append(await hub.forget(raw, purge_data=purge_data))
            except KeyError:
                skipped.append({"id": raw, "reason": "unknown"})
            except ProjectBusyError as e:
                skipped.append({"id": raw, "reason": "live", "detail": str(e)})
            except (ValueError, OSError) as e:
                skipped.append({"id": raw, "reason": "failed", "detail": str(e)})
        return {"removed": removed, "skipped": skipped}

    # Literal segments first, so neither can be read as a project id.
    @app.post("/api/projects/remove")
    async def remove_projects(request: Request) -> dict:
        return await _forget_many(request, purge_data=False)

    @app.post("/api/projects/purge")
    async def purge_projects(request: Request) -> dict:
        return await _forget_many(request, purge_data=True)

    @app.get("/api/data")
    def data_summary() -> dict:
        return _data_summary(hub.default_id)

    @app.delete("/api/data")
    async def purge_data() -> dict:
        return await _forget(hub.default_id, purge_data=True)

    @app.get("/api/projects/{pid}/data")
    def project_data_summary_route(pid: str) -> dict:
        return _data_summary(pid)

    @app.delete("/api/projects/{pid}/data")
    async def project_purge_data(pid: str) -> dict:
        return await _forget(pid, purge_data=True)

    @app.delete("/api/projects/{pid}")
    async def remove_project(pid: str) -> dict:
        return await _forget(pid, purge_data=False)

    @app.get("/api/dir")
    def browse_dir(path: str | None = None) -> dict:
        try:
            return list_dirs(path)
        except NotADirectoryError as e:
            raise HTTPException(400, f"not a directory: {e}") from e
        except OSError as e:
            raise HTTPException(400, f"cannot read directory: {e}") from e

    # ---- project trust gate ----
    # A project's MCP servers (executable-bearing project-scope config) are
    # inert until the project is explicitly trusted once. These routes let the
    # UI report what was refused and grant/revoke that trust. See
    # docs/archive/TRUST-HANDOFF.md and quickcode/security/trust.py.

    def _with_tool_detail(pid: str, status: dict) -> dict:
        """Add each command tool's argv to the report.

        The trust module hashes those files but cannot parse them -- security
        sits below the kernel, and the authoring parser imports it. The server
        sits above both, so this is the layer where the two can meet.
        """
        if not status.get("tools"):
            return status
        try:
            from quickcode.kernel.authoring import discovery

            detail = discovery.tools_for_review(project(hub, pid).cwd)
        except Exception:  # a report that loses its detail still reports
            return status
        return {**status, "tool_detail": detail}

    def _trust_status(pid: str) -> dict:
        try:
            return _with_tool_detail(pid, hub.trust_status(pid))
        except KeyError as e:
            raise HTTPException(404, f"unknown project: {pid}") from e

    async def _grant_trust(pid: str, request: Request) -> dict:
        from quickcode.security.trust import ConfigChanged

        # The hash the prompt showed, when the caller sends it: the grant is
        # then for that configuration or for nothing.
        body = await read_json(request)
        expected = body.get("hash") if isinstance(body, dict) else None
        try:
            return _with_tool_detail(pid, await hub.grant_trust(
                pid, expected=expected if isinstance(expected, str) else None))
        except KeyError as e:
            raise HTTPException(404, f"unknown project: {pid}") from e
        except ConfigChanged as e:
            raise HTTPException(409, str(e)) from e

    async def _revoke_trust(pid: str) -> dict:
        try:
            return _with_tool_detail(pid, await hub.revoke_trust(pid))
        except KeyError as e:
            raise HTTPException(404, f"unknown project: {pid}") from e

    @app.get("/api/trust")
    def trust_status() -> dict:
        return _trust_status(hub.default_id)

    @app.post("/api/trust")
    async def grant_trust(request: Request) -> dict:
        return await _grant_trust(hub.default_id, request)

    @app.delete("/api/trust")
    async def revoke_trust() -> dict:
        return await _revoke_trust(hub.default_id)

    @app.get("/api/projects/{pid}/trust")
    def project_trust_status(pid: str) -> dict:
        return _trust_status(pid)

    @app.post("/api/projects/{pid}/trust")
    async def project_grant_trust(pid: str, request: Request) -> dict:
        return await _grant_trust(pid, request)

    @app.delete("/api/projects/{pid}/trust")
    async def project_revoke_trust(pid: str) -> dict:
        return await _revoke_trust(pid)
