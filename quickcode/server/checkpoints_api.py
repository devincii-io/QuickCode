"""File checkpoints over HTTP: list them, preview a rewind, rewind.

    GET  /sessions/{conv_id}/checkpoints           every turn that changed files
    POST /sessions/{conv_id}/checkpoints/preview   {turn, paths?}         diffs, conflicts
    POST /sessions/{conv_id}/checkpoints/rewind    {turn, paths?, force?} put them back

Each is mounted under ``/api`` and ``/api/projects/{pid}`` (``http.scoped``).
A rewind is the user's action, not a tool call: it goes through no permission
gate because the person pressing the button is the one the gate asks. It is
refused while the conversation is working, since a turn running alongside it
could be writing the very files it restores. docs/CHECKPOINTS.md has the
shapes.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from quickcode.checkpoints import rewind
from quickcode.checkpoints.events import FilesRewound
from quickcode.checkpoints.store import CheckpointStore
from quickcode.server.http import read_json, scoped, valid_conv_id
from quickcode.server.manager import ConversationManager
from quickcode.server.projects import ProjectHub
from quickcode.session.store import SessionStore


def _store(manager: ConversationManager, conv_id: str) -> CheckpointStore:
    if not valid_conv_id(conv_id):
        raise HTTPException(404, "unknown conversation")
    store = CheckpointStore(manager.cwd, conv_id)
    if not (store.exists() or manager.get(conv_id) is not None
            or SessionStore(manager.cwd, conv_id).path.exists()):
        raise HTTPException(404, "unknown conversation")
    return store


def _request(body: Any) -> tuple[int, list[str] | None, bool]:
    if not isinstance(body, dict):
        raise HTTPException(400, "body must be {'turn': <n>, 'paths'?: [...], 'force'?: bool}")
    turn = body.get("turn")
    if isinstance(turn, bool) or not isinstance(turn, int) or turn < 1:
        raise HTTPException(400, "turn must be a turn number, 1 or more")
    selected = body.get("paths")
    if selected is not None:
        if (not isinstance(selected, list) or not selected
                or len(selected) > rewind.MAX_SELECTED
                or not all(isinstance(p, str) and 0 < len(p) <= 4096 for p in selected)):
            raise HTTPException(
                400, f"paths must be a non-empty list of at most {rewind.MAX_SELECTED} "
                     "paths, as the checkpoint listing gives them")
    force = body.get("force", False)
    if not isinstance(force, bool):
        raise HTTPException(400, "force must be true or false")
    return turn, selected, force


def _working(manager: ConversationManager, conv_id: str) -> str | None:
    """Why a rewind must wait: something in the conversation may be writing."""
    conv = manager.get(conv_id)
    if conv is None:
        return None
    if conv.agent.busy:
        return "a turn is running"
    if conv.pending:
        return "a permission prompt is waiting for an answer"
    if any(not t.done() for t in getattr(conv, "_jobs", ())):
        return "a background subagent job is running"
    return None


def list_checkpoints(manager: ConversationManager, conv_id: str) -> dict:
    return rewind.listing(_store(manager, conv_id))


async def preview_rewind(manager: ConversationManager, conv_id: str,
                         request: Request) -> dict:
    store = _store(manager, conv_id)
    turn, selected, _ = _request(await read_json(request))
    try:
        return await asyncio.to_thread(rewind.preview, store, turn, selected)
    except rewind.RewindError as exc:
        raise HTTPException(exc.status, exc.detail) from exc


async def rewind_files(manager: ConversationManager, conv_id: str,
                       request: Request) -> dict:
    store = _store(manager, conv_id)
    turn, selected, force = _request(await read_json(request))
    busy = _working(manager, conv_id)
    if busy:
        raise HTTPException(409, f"conversation is busy ({busy}); rewind once it is idle")
    try:
        result = await asyncio.to_thread(rewind.apply, store, turn, selected, force=force)
    except rewind.RewindError as exc:
        raise HTTPException(exc.status, exc.detail) from exc
    if result.record is not None:
        _log(manager, conv_id, FilesRewound(
            rewind_id=result.record.id, to_turn=turn, forced=result.record.forced,
            files=[{"path": f.path, "action": f.action, "from_turn": f.from_turn}
                   for f in result.record.files],
            skipped=result.skipped,
        ).to_json())
    return result.to_json()


def _log(manager: ConversationManager, conv_id: str, ev: dict[str, Any]) -> None:
    """Into the session log: through the open conversation, so attached windows
    hear it, or straight onto the file when nothing has it open."""
    conv = manager.get(conv_id)
    if conv is not None:
        conv.emit(ev)
        return
    store = SessionStore(manager.cwd, conv_id)
    if store.path.exists():
        store.append_event({**ev, "turn": store.last_turn()})


_ROUTES = (
    ("GET", "/sessions/{conv_id}/checkpoints", list_checkpoints),
    ("POST", "/sessions/{conv_id}/checkpoints/preview", preview_rewind),
    ("POST", "/sessions/{conv_id}/checkpoints/rewind", rewind_files),
)


def register_checkpoint_routes(app: FastAPI, hub: ProjectHub) -> None:
    for method, suffix, handler in _ROUTES:
        scoped(app, hub, method, suffix, handler)
