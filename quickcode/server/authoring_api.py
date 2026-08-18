"""HTTP surface for authored plugins, registered from one call.

Follows the ``server/gitinfo.py:register_git_routes`` precedent: every handler
lives here, both route shapes are declared together, and ``app.py`` gains one
import and one call. Each route has the ``/api/projects/{pid}/…`` twin the file
pairs for everything else, because a second open project must be able to answer
the same questions as the first.

Two behaviours are load-bearing and are the reason these are not five lines
each:

* **Save writes, then validates.** ``PUT .../source`` never refuses. It returns
  the problems so the editor can show them immediately, rather than making you
  discover the typo when a tool you expected never appears.
* **Create and duplicate refuse a reserved id.** ``.quickcode/`` is committed,
  so an authored ``tool.bash`` would let a cloned repository stand in for
  something you trust. The refusal carries the reason and the recourse, which
  is Duplicate.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request

from quickcode.kernel.authoring import discovery, store
from quickcode.kernel.authoring.store import AuthoringError


def _fail(exc: AuthoringError) -> HTTPException:
    return HTTPException(exc.status, exc.message + (f" {exc.fix}" if exc.fix else ""))


def register_authoring_routes(app: FastAPI, default_manager, project_manager) -> None:
    """``default_manager()`` -> the hub's default; ``project_manager(pid)`` -> one."""

    # ---- payload builders (shared by both route shapes) ----

    def _list(manager) -> dict[str, Any]:
        found = discovery.discover(manager.cwd)
        return {
            "plugins": [store.plugin_json(p) for p in found.plugins],
            "problems": [p.to_json() for p in found.problems],
            "dirs": {
                "user": str(discovery.user_plugins_dir()),
                "project": str(discovery.project_plugins_dir(manager.cwd)),
            },
        }

    def _problems(manager) -> dict[str, Any]:
        """Every problem this project has, authoring and resolution alike.

        One array, one renderer. A validation problem and a resolution conflict
        are the same thing at two different times: something the user wrote does
        not do what they think.
        """
        from quickcode.kernel import build_registry

        registry = build_registry(
            manager.cwd,
            tools=list(manager.registry_factory().tools.values()),
            active_provider=manager.config.profile.provider,
        )
        return {"problems": [p.to_json() for p in registry.problems]}

    async def _create(manager, request: Request) -> dict[str, Any]:
        body = await _json(request)
        kind = str(body.get("kind", "")).strip().lower()
        name = str(body.get("name", "")).strip()
        scope = str(body.get("scope", "project")).strip().lower()
        text = body.get("text")
        try:
            path, plugin, problems = store.create(
                manager.cwd, kind=kind, name=name, scope=scope,
                title=str(body.get("title", "")),
                text=text if isinstance(text, str) else None,
            )
        except AuthoringError as exc:
            raise _fail(exc) from exc
        except OSError as exc:
            raise HTTPException(500, f"could not write the file: {exc}") from exc
        return {
            "path": str(path),
            "plugin": store.plugin_json(plugin) if plugin else None,
            "problems": [p.to_json() for p in problems],
            "applies_to": "new sessions",
        }

    def _source(manager, plugin_id: str) -> dict[str, Any]:
        try:
            path, text, problems = store.read_source(manager.cwd, plugin_id)
        except AuthoringError as exc:
            raise _fail(exc) from exc
        except OSError as exc:
            raise HTTPException(500, f"could not read the file: {exc}") from exc
        return {"path": str(path), "text": text,
                "problems": [p.to_json() for p in problems]}

    async def _save(manager, plugin_id: str, request: Request) -> dict[str, Any]:
        body = await _json(request)
        text = body.get("text")
        if not isinstance(text, str):
            raise HTTPException(400, "body must be {'text': <file contents>}")
        try:
            path, plugin, problems = store.save_source(manager.cwd, plugin_id, text)
        except AuthoringError as exc:
            raise _fail(exc) from exc
        except OSError as exc:
            raise HTTPException(500, f"could not write the file: {exc}") from exc
        return {
            "path": str(path),
            "plugin": store.plugin_json(plugin) if plugin else None,
            # Advisory: the file is already written. This is what tells you
            # now rather than later.
            "problems": [p.to_json() for p in problems],
            "applies_to": "new sessions",
        }

    def _delete(manager, plugin_id: str) -> dict[str, Any]:
        try:
            was, now = store.delete(manager.cwd, plugin_id)
        except AuthoringError as exc:
            raise _fail(exc) from exc
        except OSError as exc:
            raise HTTPException(500, f"could not move the file: {exc}") from exc
        return {"path": str(was), "trashed_to": str(now)}

    async def _validate(manager, request: Request) -> dict[str, Any]:
        body = await _json(request)
        text = body.get("text")
        if not isinstance(text, str):
            raise HTTPException(400, "body must be {'text': <file contents>}")
        plugin, problems = store.validate_text(
            text,
            kind=str(body.get("kind", "")).strip().lower(),
            scope=str(body.get("scope", "project")).strip().lower(),
            name=str(body.get("name", "draft")).strip() or "draft",
        )
        return {
            "loadable": plugin is not None,
            "plugin": store.plugin_json(plugin) if plugin else None,
            "problems": [p.to_json() for p in problems],
        }

    async def _duplicate(manager, plugin_id: str, request: Request) -> dict[str, Any]:
        body = await _json(request)
        bodies = _section_bodies(manager)
        try:
            path, plugin, problems = store.duplicate(
                manager.cwd, plugin_id,
                scope=str(body.get("scope", "project")).strip().lower(),
                name=str(body.get("name", "")).strip(),
                bodies=bodies,
            )
        except AuthoringError as exc:
            raise _fail(exc) from exc
        except OSError as exc:
            raise HTTPException(500, f"could not write the file: {exc}") from exc
        return {
            "path": str(path),
            "plugin": store.plugin_json(plugin) if plugin else None,
            "problems": [p.to_json() for p in problems],
            "derived_from": plugin_id,
            "applies_to": "new sessions",
        }

    def _section_bodies(manager) -> dict[str, str]:
        """This project's rendered section text, so a duplicated section starts
        from what the agent is actually told rather than from a template."""
        try:
            from quickcode.prompts.system import render_with_sections

            _text, rendered = render_with_sections(manager.env, orchestration=True)
            return {s.id: s.text for s in rendered}
        except Exception:
            return {}

    # ---- default-project routes ----

    @app.get("/api/kernel/authored")
    def authored() -> dict:
        return _list(default_manager())

    @app.post("/api/kernel/authored")
    async def authored_create(request: Request) -> dict:
        return await _create(default_manager(), request)

    # Registered before ``{plugin_id}`` so the literal segment can never be
    # read as an id.
    @app.post("/api/kernel/authored/validate")
    async def authored_validate(request: Request) -> dict:
        return await _validate(default_manager(), request)

    @app.get("/api/kernel/authored/{plugin_id}/source")
    def authored_source(plugin_id: str) -> dict:
        return _source(default_manager(), plugin_id)

    @app.put("/api/kernel/authored/{plugin_id}/source")
    async def authored_save(plugin_id: str, request: Request) -> dict:
        return await _save(default_manager(), plugin_id, request)

    @app.delete("/api/kernel/authored/{plugin_id}")
    def authored_delete(plugin_id: str) -> dict:
        return _delete(default_manager(), plugin_id)

    @app.post("/api/kernel/plugins/{plugin_id}/duplicate")
    async def plugin_duplicate(plugin_id: str, request: Request) -> dict:
        return await _duplicate(default_manager(), plugin_id, request)

    @app.get("/api/kernel/problems")
    def kernel_problems() -> dict:
        return _problems(default_manager())

    # ---- project-scoped twins ----

    @app.get("/api/projects/{pid}/kernel/authored")
    def project_authored(pid: str) -> dict:
        return _list(project_manager(pid))

    @app.post("/api/projects/{pid}/kernel/authored")
    async def project_authored_create(pid: str, request: Request) -> dict:
        return await _create(project_manager(pid), request)

    @app.post("/api/projects/{pid}/kernel/authored/validate")
    async def project_authored_validate(pid: str, request: Request) -> dict:
        return await _validate(project_manager(pid), request)

    @app.get("/api/projects/{pid}/kernel/authored/{plugin_id}/source")
    def project_authored_source(pid: str, plugin_id: str) -> dict:
        return _source(project_manager(pid), plugin_id)

    @app.put("/api/projects/{pid}/kernel/authored/{plugin_id}/source")
    async def project_authored_save(pid: str, plugin_id: str, request: Request) -> dict:
        return await _save(project_manager(pid), plugin_id, request)

    @app.delete("/api/projects/{pid}/kernel/authored/{plugin_id}")
    def project_authored_delete(pid: str, plugin_id: str) -> dict:
        return _delete(project_manager(pid), plugin_id)

    @app.post("/api/projects/{pid}/kernel/plugins/{plugin_id}/duplicate")
    async def project_plugin_duplicate(pid: str, plugin_id: str, request: Request) -> dict:
        return await _duplicate(project_manager(pid), plugin_id, request)

    @app.get("/api/projects/{pid}/kernel/problems")
    def project_kernel_problems(pid: str) -> dict:
        return _problems(project_manager(pid))


async def _json(request: Request) -> dict[str, Any]:
    from quickcode.server.app import _read_json

    body = await _read_json(request)
    if body in (None, ""):
        return {}
    if not isinstance(body, dict):
        raise HTTPException(400, "request body must be a JSON object")
    return body
