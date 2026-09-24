"""Request plumbing every route module shares: bounded JSON bodies, the id
checks a path segment has to pass, and the two shapes a project route comes in.

``/api{suffix}`` addresses the hub's default project -- the launch directory,
and the whole API before there were projects -- and
``/api/projects/{pid}{suffix}`` addresses any open one. ``scoped`` mounts one
handler under both, so the two shapes cannot drift apart.
"""

from __future__ import annotations

import contextlib
import inspect
import json
import re
from collections.abc import Callable, Sequence
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from quickcode.server.manager import ConversationManager
from quickcode.server.projects import ProjectHub

JSON_BODY_CAP = 1024 * 1024
# Conversation ids are generated as hex; anything else in a path segment would
# be a traversal attempt against the sessions directory.
_CONV_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
# A profile id is a key in a settings file and a path segment in these routes,
# so it is held to the shape both can carry losslessly.
_PROFILE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")

DEFAULT = "default"
PROJECT = "project"


def valid_conv_id(conv_id: str) -> bool:
    return _CONV_ID_RE.fullmatch(conv_id) is not None


def valid_profile_id(profile_id: str) -> bool:
    return _PROFILE_ID_RE.fullmatch(profile_id) is not None


def project(hub: ProjectHub, pid: str) -> ConversationManager:
    manager = hub.get(pid)
    if manager is None:
        raise HTTPException(404, f"unknown project: {pid}")
    return manager


def scoped(
    app: FastAPI,
    hub: ProjectHub,
    method: str,
    suffix: str,
    handler: Callable[..., Any],
    *,
    shapes: Sequence[str] = (DEFAULT, PROJECT),
) -> None:
    """Mount ``handler`` at ``/api{suffix}`` and ``/api/projects/{pid}{suffix}``.

    ``handler`` takes the project's ``ConversationManager`` first; the rest of
    its signature (path and query parameters, ``Request``) is FastAPI's to read
    as usual. The default shape reads ``hub.default`` on each request, as the
    hand-written pairs did. ``shapes`` picks which shapes to mount and in which
    order, since route order is match order. The routes are named after the
    handler, the project one with a ``project_`` prefix.
    """
    signature = inspect.signature(handler, eval_str=True)
    rest = list(signature.parameters.values())[1:]
    pid = inspect.Parameter("pid", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=str)
    for shape in shapes:
        if shape == PROJECT:
            endpoint = _endpoint(handler, lambda params: project(hub, params.pop("pid")))
            endpoint.__signature__ = signature.replace(parameters=[pid, *rest])
            path, name = f"/api/projects/{{pid}}{suffix}", f"project_{handler.__name__}"
        else:
            endpoint = _endpoint(handler, lambda params: hub.default)
            endpoint.__signature__ = signature.replace(parameters=rest)
            path, name = f"/api{suffix}", handler.__name__
        endpoint.__name__ = endpoint.__qualname__ = name
        app.add_api_route(path, endpoint, methods=[method])


def _endpoint(
    handler: Callable[..., Any],
    resolve: Callable[[dict[str, Any]], ConversationManager],
) -> Callable[..., Any]:
    # Sync stays sync: FastAPI runs a plain function in its threadpool and a
    # coroutine on the loop, and a handler that does file IO must not move.
    if inspect.iscoroutinefunction(handler):
        async def endpoint(**params: Any) -> Any:
            manager = resolve(params)
            return await handler(manager, **params)
    else:
        def endpoint(**params: Any) -> Any:
            manager = resolve(params)
            return handler(manager, **params)
    return endpoint


async def read_json(request: Request, maximum: int = JSON_BODY_CAP) -> Any:
    """Read a bounded JSON body without buffering an unbounded request."""
    content_length = request.headers.get("content-length")
    if content_length:
        with contextlib.suppress(ValueError):
            if int(content_length) > maximum:
                raise HTTPException(413, f"request body cannot exceed {maximum} bytes")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > maximum:
            raise HTTPException(413, f"request body cannot exceed {maximum} bytes")
        chunks.append(chunk)
    raw = b"".join(chunks)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(400, "request body must be valid JSON") from exc
