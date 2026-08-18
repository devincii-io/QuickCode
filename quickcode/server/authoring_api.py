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
* **The dry run resolves and never executes.** ``POST .../dry-run`` calls the
  same ``argv.render_argv`` that ``CommandTool.resolve_argv`` calls, so the
  array the panel shows is the array the permission prompt will show. There is
  no subprocess anywhere in this module and there must never be one: a
  configuration page that could run a command would be a second path around the
  permission gate, which is the one path the design keeps single.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request

from quickcode.kernel.authoring import argv as argv_rules
from quickcode.kernel.authoring import discovery, store
from quickcode.kernel.authoring.model import Param
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

    async def _dry_run(manager, request: Request) -> dict[str, Any]:
        """The resolved argv for a template, from ``argv.py`` itself.

        Three ways in, one resolver. ``{"text": …}`` is a draft in the editor
        that may never have been saved; ``{"id": …}`` is a file on disk;
        ``{"argv": [...], "params": [...]}`` is a caller that already holds a
        template the validator has passed. All three end in the same
        ``render_argv`` the runtime calls, which is the whole point -- a preview
        that reimplements the substitution rules agrees with what runs only by
        coincidence, and a preview that agrees by coincidence is worse than
        none.

        It resolves and never executes. No subprocess is started here under any
        input; see the module docstring.
        """
        body = await _json(request)
        values = body.get("values", {})
        if not isinstance(values, dict):
            raise HTTPException(400, "values must be an object of name -> value")

        text = body.get("text")
        plugin_id = str(body.get("id") or "").strip()
        if not isinstance(text, str) and plugin_id:
            try:
                _path, text, _problems = store.read_source(manager.cwd, plugin_id)
            except AuthoringError as exc:
                raise _fail(exc) from exc
            except OSError as exc:
                raise HTTPException(500, f"could not read the file: {exc}") from exc

        problems: list[Any] = []
        if isinstance(text, str):
            plugin, problems = store.validate_text(
                text,
                kind="tool",
                scope=str(body.get("scope", "project")).strip().lower(),
                name=str(body.get("name", "draft")).strip() or "draft",
            )
            if plugin is not None and plugin.kind != "tool":
                raise HTTPException(
                    400,
                    "a dry run resolves an argv template, and this file "
                    f"declares 'kind: {plugin.kind}'")
            declared = list(plugin.params) if plugin else []
            template = list(plugin.argv) if plugin else []
            loadable = plugin is not None
        else:
            raw = body.get("argv")
            if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
                raise HTTPException(
                    400,
                    "body must carry {'text': <file contents>}, {'id': <plugin "
                    "id>} or {'argv': [...], 'params': [...]}, plus 'values'")
            declared = _params_from_json(body.get("params"))
            template = raw
            loadable = True

        # A file the validator rejected resolves to nothing: its template is
        # exactly what is in doubt, and guessing at one would put an array on
        # screen that no tool will ever run.
        resolved = (
            argv_rules.render_argv(
                template, {p.name: p for p in declared}, _with_defaults(declared, values))
            if loadable else []
        )
        return {
            "loadable": loadable,
            "argv": resolved,
            "params": [_param_json(p) for p in declared],
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

    @app.post("/api/kernel/authored/dry-run")
    async def authored_dry_run(request: Request) -> dict:
        return await _dry_run(default_manager(), request)

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

    @app.post("/api/projects/{pid}/kernel/authored/dry-run")
    async def project_authored_dry_run(pid: str, request: Request) -> dict:
        return await _dry_run(project_manager(pid), request)

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


def _params_from_json(raw: Any) -> list[Param]:
    """The parameter declarations a caller sent inline, as ``Param`` objects.

    Only the keys the substitution rules read are taken -- ``type`` decides
    which rule an element falls under, ``flag`` is what a true bool emits,
    ``default`` is what the tool's input model would have filled in. Nothing
    here validates: this mode is for a template that has already been through
    the validator, and the ``text`` mode is the one that checks.
    """
    out: list[Param] = []
    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "")).strip()
        if not name:
            continue
        out.append(Param(
            name=name,
            type=str(entry.get("type") or "string").strip().lower(),
            flag=str(entry.get("flag") or ""),
            default=entry.get("default"),
        ))
    return out


def _with_defaults(params: list[Param], values: dict[str, Any]) -> dict[str, Any]:
    """Absent means "not sent", which is what a default is for; present-and-empty
    is left exactly as it came. The difference is load-bearing: an empty
    whole-element placeholder is rule 3 (the element drops), not a value the
    caller forgot to fill in."""
    out = dict(values)
    for param in params:
        if param.name not in out and param.default is not None:
            out[param.name] = param.default
    return out


def _param_json(param: Param) -> dict[str, Any]:
    return {
        "name": param.name,
        "type": param.type,
        "description": param.description,
        "required": param.required,
        "default": param.default,
        "choices": list(param.choices),
        "flag": param.flag,
    }


async def _json(request: Request) -> dict[str, Any]:
    from quickcode.server.app import _read_json

    body = await _read_json(request)
    if body in (None, ""):
        return {}
    if not isinstance(body, dict):
        raise HTTPException(400, "request body must be a JSON object")
    return body
