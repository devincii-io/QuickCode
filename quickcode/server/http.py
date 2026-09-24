"""Request plumbing every route module shares: bounded JSON bodies, the id
checks a path segment has to pass, and resolving a project id to its manager."""

from __future__ import annotations

import contextlib
import json
import re
from typing import Any

from fastapi import HTTPException, Request

from quickcode.server.manager import ConversationManager
from quickcode.server.projects import ProjectHub

JSON_BODY_CAP = 1024 * 1024
# Conversation ids are generated as hex; anything else in a path segment would
# be a traversal attempt against the sessions directory.
_CONV_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
# A profile id is a key in a settings file and a path segment in these routes,
# so it is held to the shape both can carry losslessly.
_PROFILE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


def valid_conv_id(conv_id: str) -> bool:
    return _CONV_ID_RE.fullmatch(conv_id) is not None


def valid_profile_id(profile_id: str) -> bool:
    return _PROFILE_ID_RE.fullmatch(profile_id) is not None


def project(hub: ProjectHub, pid: str) -> ConversationManager:
    manager = hub.get(pid)
    if manager is None:
        raise HTTPException(404, f"unknown project: {pid}")
    return manager


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
