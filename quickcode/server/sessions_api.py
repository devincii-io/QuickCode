"""One project's conversation surface: what a pane boots from, and its sessions.

Bootstrap, the session list and what can be done to it (rename, archive,
delete, bulk delete, the empty-session sweep), opening a conversation, the
model catalog and the plugin inventory. Each handler takes the project's
manager and is mounted in both path shapes by ``http.scoped``; bootstrap is
written out per shape because its project shape also answers with the id.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response

from quickcode.server import provider_settings
from quickcode.server.config_api import search_payload
from quickcode.server.http import PROJECT, project, read_json, scoped, valid_conv_id
from quickcode.server.manager import ConversationManager
from quickcode.server.projects import ProjectHub
from quickcode.session.store import MAX_TITLE, SessionStore, purge_sessions


def bootstrap_payload(manager: ConversationManager) -> dict:
    from quickcode import __version__
    from quickcode.config import THEME_PRESETS

    cfg = manager.config
    profile = cfg.profile
    return {
        # The presets ride along so Settings can offer them without
        # duplicating eleven hex values per palette in the frontend.
        "theme_presets": THEME_PRESETS,
        "version": __version__,
        "cwd": str(manager.cwd),
        "project": manager.cwd.name,
        "git_branch": manager.env.git_branch,
        "provider": manager.provider_name,
        "base_url": profile.base_url,
        "default_model": cfg.last_model or profile.resolve("orchestrator"),
        "default_mode": manager.default_mode or cfg.default_mode,
        "allow_yolo": manager.allow_yolo,
        "theme": cfg.theme_colors(),
        "has_api_key": bool(profile.api_key),
        "api_key_env": profile.api_key_env,
        # The backend plugin (``provider`` above is its display name) and
        # every one Settings can switch to.
        "model_provider": profile.provider,
        "model_providers": provider_settings.payload(cfg),
        "max_tokens": cfg.max_tokens,
        "temperature": cfg.temperature,
        "search": search_payload(cfg),
    }


def revive(manager: ConversationManager, conv_id: str) -> None:
    """Bring an archived session back into the list before opening it.

    Working in a session is the opposite of having filed it away, and a
    live-but-hidden conversation would be the worst of both.
    """
    if valid_conv_id(conv_id):
        SessionStore(manager.cwd, conv_id).unarchive()


def sessions(manager: ConversationManager, archived: bool = False) -> list[dict]:
    # "Live" means in use -- attached, running, or holding a job -- not
    # "was opened at some point in this process". The dot in the UI and
    # the 409 from delete now agree, and both are true.
    live = set(manager.live_conversations())
    out = []
    for info in SessionStore.list_sessions(manager.cwd, include_archived=archived):
        out.append(
            {
                "conv_id": info.conv_id,
                "title": info.title,
                "model": info.model,
                "mtime": info.mtime,
                "message_count": info.message_count,
                "live": info.conv_id in live,
                "archived": info.archived,
            }
        )
    return out


async def open_conversation(manager: ConversationManager, request: Request) -> dict:
    body = await read_json(request)
    conv_id = body.get("resume") if isinstance(body, dict) else None
    if conv_id is not None and not isinstance(conv_id, str):
        raise HTTPException(400, "resume must be a conversation id string")
    if conv_id is not None and not valid_conv_id(conv_id):
        raise HTTPException(400, "invalid conversation id")
    if conv_id:
        revive(manager, conv_id)
    conv = manager.open(conv_id)
    return {"conv_id": conv.conv_id}


async def models(manager: ConversationManager, refresh: bool = False) -> list[dict]:
    out = []
    for m in await manager.models(refresh=refresh):
        out.append(
            {
                "id": m.id,
                "name": m.name,
                "context_length": m.context_length,
                "prompt_price": m.prompt_price,
                "completion_price": m.completion_price,
                "supports_tools": m.supports_tools,
            }
        )
    return out


def plugins(manager: ConversationManager) -> dict:
    return manager.plugin_inventory()


# ---- session management: delete, archive, sweep ----


async def delete_session(manager: ConversationManager, conv_id: str) -> Response:
    if not valid_conv_id(conv_id):
        raise HTTPException(404, "unknown conversation")
    # A conversation opened earlier in this run is not "live" -- nothing is
    # attached to it and nothing is running in it. It used to be refused
    # anyway, for the life of the process, with a message telling the user
    # to close something that was not open. Idle ones are closed here;
    # busy ones still say so, and say *why*.
    busy = await manager.release(conv_id)
    if busy:
        raise HTTPException(409, f"conversation is in use ({busy})")
    if not SessionStore(manager.cwd, conv_id).path.exists():
        raise HTTPException(404, "unknown conversation")
    # Everything the session owned goes with it: the transcript, the task
    # board beside it, and any subagent artifact nothing else references.
    try:
        result = purge_sessions(manager.cwd, [conv_id])
    except OSError as e:
        raise HTTPException(500, f"could not delete session: {e}") from e
    if conv_id in result.failed:
        raise HTTPException(500, f"could not delete session: {result.failed[conv_id]}")
    return Response(status_code=204)


async def rename_session(
    manager: ConversationManager, conv_id: str, request: Request
) -> dict:
    """Give a session a name of its own.

    Deliberately allowed on a *live* conversation, where deleting and
    archiving are refused. Those two move or unlink the log out from under
    its own writer; this appends one ``meta`` record to it, which is what
    every other write to a session log already is — the model change at
    manager.py's ``append_meta(model=…)`` does it mid-conversation too. So
    the answer is 200 and the new name is in effect immediately, for the
    list and for the session that is open.

    A blank title is not an error: it clears a name that was chosen and
    hands the session back to the one derived from its first message. The
    response therefore carries the title the listings will now show, not
    the string that was sent.
    """
    if not valid_conv_id(conv_id):
        raise HTTPException(404, "unknown conversation")
    # An open conversation is renamed through its own store. One nobody
    # has spoken in yet has no log on disk -- its opening records are held
    # -- and naming it is an act that writes them, where a second store
    # would find no file and answer 404 for a pane the user can see.
    conv = manager.get(conv_id)
    store = conv.store if conv is not None else SessionStore(manager.cwd, conv_id)
    if conv is None and not store.path.exists():
        raise HTTPException(404, "unknown conversation")
    body = await read_json(request)
    title = body.get("title") if isinstance(body, dict) else None
    if not isinstance(title, str):
        raise HTTPException(
            400,
            "body must be {'title': <string>}; an empty title clears the "
            "name and the session goes back to the one taken from its "
            "first message",
        )
    if len(title.strip()) > MAX_TITLE:
        raise HTTPException(400, f"title is longer than {MAX_TITLE} characters")
    try:
        effective = store.rename(title)
    except OSError as e:
        raise HTTPException(500, f"could not rename session: {e}") from e
    return {"conv_id": conv_id, "title": effective}


async def archive_session(manager: ConversationManager, conv_id: str) -> dict:
    return await _set_archived(manager, conv_id, True)


async def unarchive_session(manager: ConversationManager, conv_id: str) -> dict:
    return await _set_archived(manager, conv_id, False)


async def _set_archived(
    manager: ConversationManager, conv_id: str, archived: bool
) -> dict:
    if not valid_conv_id(conv_id):
        raise HTTPException(404, "unknown conversation")
    store = SessionStore(manager.cwd, conv_id)
    if not store.path.exists():
        raise HTTPException(404, "unknown conversation")
    # Archiving moves the file; doing that under a *running* conversation
    # would pull the log out from beneath its own writer. An idle one is
    # not writing, so it is closed and archived rather than refused for
    # the rest of the process's life.
    if archived:
        busy = await manager.release(conv_id)
        if busy:
            raise HTTPException(409, f"conversation is in use ({busy})")
    try:
        store.archive() if archived else store.unarchive()
    except OSError as e:
        raise HTTPException(500, f"could not move session: {e}") from e
    return {"conv_id": conv_id, "archived": store.archived}


def _selection(body: Any) -> list[str]:
    ids = body.get("conv_ids") if isinstance(body, dict) else None
    if not isinstance(ids, list) or not ids:
        raise HTTPException(400, "body must be {'conv_ids': [<id>, …]}")
    if len(ids) > 500:
        raise HTTPException(400, "too many conversations in one request")
    out = []
    for raw in ids:
        if not isinstance(raw, str) or not valid_conv_id(raw):
            raise HTTPException(400, f"invalid conversation id: {raw!r}")
        out.append(raw)
    return out


async def _purge_many(manager: ConversationManager, conv_ids: list[str]) -> dict:
    """Delete what can be deleted; report the rest instead of failing whole.

    A bulk delete that aborted on the first live session would leave the
    user guessing which of twenty rows went through.

    "Live" is the same test the single delete applies: an idle conversation
    opened earlier in this run is closed and deleted, not refused for the
    rest of the process's life.
    """
    skipped: list[dict] = []
    targets: list[str] = []
    for conv_id in conv_ids:
        if await manager.release(conv_id):
            skipped.append({"conv_id": conv_id, "reason": "live"})
        elif not SessionStore(manager.cwd, conv_id).path.exists():
            skipped.append({"conv_id": conv_id, "reason": "missing"})
        else:
            targets.append(conv_id)
    result = purge_sessions(manager.cwd, targets)
    for conv_id in result.missing:
        skipped.append({"conv_id": conv_id, "reason": "missing"})
    for conv_id, why in result.failed.items():
        skipped.append({"conv_id": conv_id, "reason": "failed", "detail": why})
    return {
        "deleted": result.sessions,
        "boards": result.boards,
        "artifacts": result.artifacts,
        "skipped": skipped,
    }


async def bulk_delete_sessions(manager: ConversationManager, request: Request) -> dict:
    body = await read_json(request)
    return await _purge_many(manager, _selection(body))


async def cleanup_sessions(manager: ConversationManager, request: Request) -> dict:
    """Sweep abandoned sessions: no messages *and* no transcript events.

    A launch that was never typed into leaves one of these behind, and
    they bury the real conversations. An interrupted turn does not qualify
    — its event log is the transcript — so it is never swept.
    """
    body = await read_json(request)
    dry_run = bool(body.get("dry_run")) if isinstance(body, dict) else False
    live = set(manager.live_conversations())
    candidates = [c for c in SessionStore.empty_sessions(manager.cwd) if c not in live]
    if dry_run:
        return {"candidates": candidates, "deleted": [], "skipped": []}
    return {"candidates": candidates, **await _purge_many(manager, candidates)}


_ROUTES = (
    ("GET", "/sessions", sessions),
    # Before the ``{conv_id}`` routes, so the literal path segments can never
    # be read as a conversation id.
    ("POST", "/sessions/delete", bulk_delete_sessions),
    ("POST", "/sessions/cleanup", cleanup_sessions),
    ("DELETE", "/sessions/{conv_id}", delete_session),
    ("PATCH", "/sessions/{conv_id}", rename_session),
    ("POST", "/sessions/{conv_id}/archive", archive_session),
    ("POST", "/sessions/{conv_id}/unarchive", unarchive_session),
    ("POST", "/conversations", open_conversation),
    ("GET", "/models", models),
    ("GET", "/plugins", plugins),
)


def register_session_routes(app: FastAPI, hub: ProjectHub, shape: str) -> None:
    """Mount one shape of this surface, ``http.DEFAULT`` or ``http.PROJECT``.

    One shape per call because ``create_app`` has always declared the default
    shape before the project registry's routes and the project shape after
    them, and the split keeps the route table exactly as it was.
    """
    if shape == PROJECT:
        @app.get("/api/projects/{pid}/bootstrap")
        def project_bootstrap(pid: str) -> dict:
            return {**bootstrap_payload(project(hub, pid)), "id": pid}
    else:
        @app.get("/api/bootstrap")
        def bootstrap() -> dict:
            return bootstrap_payload(hub.default)

    for method, suffix, handler in _ROUTES:
        scoped(app, hub, method, suffix, handler, shapes=(shape,))
