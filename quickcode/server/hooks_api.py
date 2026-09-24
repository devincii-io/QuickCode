"""HTTP surface for command hooks: list, add, change, remove, and test-run one.

Registered from one call, like ``authoring_api.py``: both route shapes are
declared here and ``app.py`` gains a single line.

The settings files stay the only place a hook lives; these routes are an
editor for them (``hooks/store.py``) and a way to try one
(``hooks/trial.py``). Two rules come from the trust gate and are enforced here,
not in the page:

* **A project write keeps trust and grants none.** Saving a hook in a trusted
  project leaves it trusted; saving one in an untrusted project stores it as
  refused, and it runs only once the project is trusted.
* **A test run is a run.** A project hook in an untrusted project is refused
  here exactly as the loop refuses it -- clicking "Test" is not a way around
  the trust prompt.

Every write answers with the whole listing again, so the page never has to
reconstruct what the files now say.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from quickcode.hooks import store as hook_store
from quickcode.hooks import trial
from quickcode.hooks.config import DEFAULT_TIMEOUT_S, EVENTS, MAX_TIMEOUT_S, TOOL_EVENTS
from quickcode.hooks.store import Declared, HookEditError
from quickcode.security import trust

APPLIES_TO = "new sessions"


def _fail(exc: HookEditError | trial.TrialError) -> HTTPException:
    if isinstance(exc, HookEditError):
        return HTTPException(exc.status, exc.message + (f" {exc.fix}" if exc.fix else ""))
    return HTTPException(400, str(exc))


def _row(row: Declared) -> dict[str, Any]:
    hook = row.hook
    return {
        "id": hook.id,
        "event": hook.event,
        "matcher": hook.matcher,
        "command": hook.command,
        "timeout": hook.timeout_s,
        "scope": hook.scope,
        "file": row.file,
        "path": hook.source,
        "status": row.status,
    }


def _listing(cwd: Path) -> dict[str, Any]:
    status = trust.status(cwd)
    rows, config = hook_store.declared(cwd, trusted=status.trusted)
    return {
        "hooks": [_row(r) for r in rows],
        "events": [{"name": e, "tool": e in TOOL_EVENTS} for e in EVENTS],
        "timeout": {"default": DEFAULT_TIMEOUT_S, "max": MAX_TIMEOUT_S},
        "trust": {"trusted": status.trusted, "reason": status.reason},
        "files": {
            "user": str(hook_store.settings_path(cwd, "user")),
            **{f"project:{name}": str(hook_store.settings_path(cwd, "project", name))
               for name in hook_store.PROJECT_FILES},
        },
        "problems": [p.to_json() for p in config.problems],
    }


def _mode(manager: Any) -> str:
    """The mode a new session would start in, for the payload's permission_mode."""
    from quickcode.kernel.resolve import default_mode

    return manager.default_mode or default_mode(Path(manager.cwd), manager.config.default_mode)


def register_hooks_routes(app: FastAPI, default_manager, project_manager) -> None:
    """``default_manager()`` -> the hub's default; ``project_manager(pid)`` -> one."""

    def _list(manager) -> dict[str, Any]:
        return _listing(Path(manager.cwd))

    def _written(manager, hook) -> dict[str, Any]:
        out = _list(manager)
        row = next((h for h in out["hooks"]
                    if h["id"] == hook.id and h["path"] == hook.source), None)
        return {**out, "hook": row, "applies_to": APPLIES_TO}

    async def _create(manager, request: Request) -> dict[str, Any]:
        body = await _json(request)
        cwd = Path(manager.cwd)
        try:
            draft = hook_store.draft_from(body)
            hook = hook_store.add(cwd, draft, scope=str(body.get("scope", "")),
                                  file=str(body.get("file") or ""))
        except HookEditError as exc:
            raise _fail(exc) from exc
        except OSError as exc:
            raise HTTPException(500, f"could not write the settings file: {exc}") from exc
        return _written(manager, hook)

    async def _update(manager, hook_id: str, request: Request) -> dict[str, Any]:
        body = await _json(request)
        cwd = Path(manager.cwd)
        try:
            draft = hook_store.draft_from(body)
            hook = hook_store.update(cwd, hook_id, draft, file=str(body.get("file") or ""))
        except HookEditError as exc:
            raise _fail(exc) from exc
        except OSError as exc:
            raise HTTPException(500, f"could not write the settings file: {exc}") from exc
        return _written(manager, hook)

    def _delete(manager, hook_id: str, file: str) -> dict[str, Any]:
        try:
            removed = hook_store.remove(Path(manager.cwd), hook_id, file=file)
        except HookEditError as exc:
            raise _fail(exc) from exc
        except OSError as exc:
            raise HTTPException(500, f"could not write the settings file: {exc}") from exc
        return {**_list(manager), "removed": removed, "applies_to": APPLIES_TO}

    async def _test(manager, hook_id: str, request: Request) -> dict[str, Any]:
        body = await _json(request)
        cwd = Path(manager.cwd)
        try:
            row = hook_store.find(cwd, hook_id, str(body.get("file") or ""))
            sample = trial.sample_from(row.hook, body)
        except (HookEditError, trial.TrialError) as exc:
            raise _fail(exc) from exc
        if row.status == "refused":
            raise HTTPException(409, (
                "this project is not trusted, so its hooks do not run, and a test run "
                "is a run. Read the commands in the trust review, then trust the "
                "project to let them run -- here and in sessions."))
        result = await trial.run_trial(
            row.hook, sample, cwd=cwd, mode=_mode(manager),
            platform=manager.env.platform, shell_name=manager.env.shell_name)
        return {"hook": _row(row), **result}

    # ---- default-project routes ----

    @app.get("/api/hooks")
    def hooks_list() -> dict:
        return _list(default_manager())

    @app.post("/api/hooks")
    async def hooks_create(request: Request) -> dict:
        return await _create(default_manager(), request)

    @app.put("/api/hooks/{hook_id}")
    async def hooks_update(hook_id: str, request: Request) -> dict:
        return await _update(default_manager(), hook_id, request)

    @app.delete("/api/hooks/{hook_id}")
    def hooks_delete(hook_id: str, file: str = "") -> dict:
        return _delete(default_manager(), hook_id, file)

    @app.post("/api/hooks/{hook_id}/test")
    async def hooks_test(hook_id: str, request: Request) -> dict:
        return await _test(default_manager(), hook_id, request)

    # ---- project-scoped twins ----

    @app.get("/api/projects/{pid}/hooks")
    def project_hooks_list(pid: str) -> dict:
        return _list(project_manager(pid))

    @app.post("/api/projects/{pid}/hooks")
    async def project_hooks_create(pid: str, request: Request) -> dict:
        return await _create(project_manager(pid), request)

    @app.put("/api/projects/{pid}/hooks/{hook_id}")
    async def project_hooks_update(pid: str, hook_id: str, request: Request) -> dict:
        return await _update(project_manager(pid), hook_id, request)

    @app.delete("/api/projects/{pid}/hooks/{hook_id}")
    def project_hooks_delete(pid: str, hook_id: str, file: str = "") -> dict:
        return _delete(project_manager(pid), hook_id, file)

    @app.post("/api/projects/{pid}/hooks/{hook_id}/test")
    async def project_hooks_test(pid: str, hook_id: str, request: Request) -> dict:
        return await _test(project_manager(pid), hook_id, request)


async def _json(request: Request) -> dict[str, Any]:
    from quickcode.server.app import _read_json

    body = await _read_json(request)
    if body in (None, ""):
        return {}
    if not isinstance(body, dict):
        raise HTTPException(400, "request body must be a JSON object")
    return body
