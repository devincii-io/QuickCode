"""The conversation WebSocket: attach, replay the log, then stream both ways.

A socket attaches to one conversation, is sent its state and every logged
event since the start (``replay_start`` … ``replay_done``), and then carries
live events out and client messages in until either side goes away. Client
messages are a small built-in protocol that plugins may extend with
``register_client_message``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, WebSocket
from starlette.websockets import WebSocketDisconnect

from quickcode.server import auth
from quickcode.server.http import valid_conv_id
from quickcode.server.manager import Client, Conversation, ConversationManager
from quickcode.server.projects import ProjectHub
from quickcode.server.sessions_api import revive
from quickcode.session.store import SessionStore

log = logging.getLogger("quickcode.server")


def register_ws_routes(
    app: FastAPI,
    hub: ProjectHub,
    *,
    ws_allowed: Callable[[WebSocket], bool],
    token: str,
) -> None:
    """Mount the conversation socket in both shapes, behind the app's one
    WebSocket auth rule (``ws_allowed``) and its loopback token."""

    async def _attach(ws: WebSocket, manager: ConversationManager | None, conv_id: str) -> None:
        if not ws_allowed(ws):
            await ws.close(code=4403)
            return
        await ws.accept(subprotocol=(auth.SUBPROTOCOL_PREFIX + token) if token else None)
        if manager is None:
            await ws.close(code=4404)
            return
        conv = manager.get(conv_id)
        if conv is None:
            # Attaching to an on-disk session revives it; unknown ids 404.
            if not valid_conv_id(conv_id):
                await ws.close(code=4404)
                return
            store = SessionStore(manager.cwd, conv_id)
            if not store.path.exists():
                await ws.close(code=4404)
                return
            revive(manager, conv_id)
            conv = manager.open(conv_id)

        client = Client()
        # Attach BEFORE snapshotting the log: anything logged after this point
        # reaches the live queue, and the client dedupes replays by seq.
        conv.clients.add(client)
        try:
            await ws.send_text(json.dumps(conv.state_event(), ensure_ascii=False))
            await ws.send_text('{"type": "replay_start"}')
            for ev in conv.store.replay_events():
                await ws.send_text(json.dumps(ev, ensure_ascii=False))
            await ws.send_text('{"type": "replay_done"}')
            await _live_phase(ws, conv, client)
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        finally:
            conv.clients.discard(client)

    @app.websocket("/ws/conversation/{conv_id}")
    async def ws_conversation(ws: WebSocket, conv_id: str) -> None:
        await _attach(ws, hub.default, conv_id)

    @app.websocket("/ws/projects/{pid}/conversation/{conv_id}")
    async def ws_project_conversation(ws: WebSocket, pid: str, conv_id: str) -> None:
        await _attach(ws, hub.get(pid), conv_id)


async def _live_phase(ws: WebSocket, conv: Conversation, client: Client) -> None:
    out = asyncio.ensure_future(_pump_out(ws, client))
    inp = asyncio.ensure_future(_pump_in(ws, conv))
    try:
        done, pending = await asyncio.wait({out, inp}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            if task.cancelled():
                continue
            exc = task.exception()
            if exc is not None and not isinstance(exc, WebSocketDisconnect):
                raise exc
    finally:
        for task in (out, inp):
            if not task.done():
                task.cancel()
        await asyncio.gather(out, inp, return_exceptions=True)


# A frame every so often, so silence means something. After a laptop sleeps and
# wakes, a socket can sit in OPEN with nothing alive behind it: sends succeed
# into the void and, on an idle conversation, no frame ever arrives to disprove
# it. The client cannot tell that apart from "the agent is thinking" without a
# beat to miss, so this is that beat — and a send that raises here is how this
# side learns the peer is gone, too.
HEARTBEAT_S = 15.0


async def _pump_out(ws: WebSocket, client: Client) -> None:
    while True:
        try:
            item = await asyncio.wait_for(client.queue.get(), timeout=HEARTBEAT_S)
        except TimeoutError:
            await ws.send_text('{"type":"heartbeat"}')
            continue
        if item is None:  # overflow sentinel: force a clean replay reconnect
            await ws.close(code=1013, reason="client fell behind; reconnect to replay")
            return
        await ws.send_text(item)


async def _pump_in(ws: WebSocket, conv: Conversation) -> None:
    while True:
        message = await ws.receive()
        if message["type"] == "websocket.disconnect":
            raise WebSocketDisconnect(message.get("code", 1000), message.get("reason"))
        raw = message.get("text")
        if raw is None:
            continue  # a binary frame: the protocol is JSON text, so drop it
        try:
            msg = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue  # malformed frame: drop it rather than kill the socket
        if not isinstance(msg, dict):
            continue
        _dispatch(conv, msg)


# Client message types registered by plugins. Kept separate from the built-in
# handlers below and checked first, but a plugin cannot claim a built-in type:
# the frontend's own protocol must keep meaning what it says.
_CLIENT_HANDLERS: dict[str, Any] = {}

BUILTIN_CLIENT_TYPES = frozenset({
    "user_message", "interrupt", "set_mode", "set_model", "compact",
    "permission_decision", "plan_decision",
})


def register_client_message(kind: str, handler) -> None:
    """Accept a new client → server message type.

    ``handler`` takes ``(conversation, message)``. Registering is additive:
    an unknown type is ignored rather than an error, so an older build simply
    does nothing with a message it has never heard of.
    """
    if kind in BUILTIN_CLIENT_TYPES:
        raise ValueError(f"{kind!r} is a built-in client message type")
    _CLIENT_HANDLERS[kind] = handler


def _dispatch(conv: Conversation, msg: dict[str, Any]) -> None:
    t = msg.get("type")
    handler = _CLIENT_HANDLERS.get(t) if isinstance(t, str) else None
    if handler is not None:
        try:
            handler(conv, msg)
        except Exception:
            log.warning("client message handler for %r failed", t, exc_info=True)
        return
    if t == "user_message":
        text = msg.get("text")
        if isinstance(text, str):
            conv.submit(text)
    elif t == "interrupt":
        conv.interrupt()
    elif t == "set_mode":
        mode = msg.get("mode")
        if isinstance(mode, str):
            conv.set_mode(mode)
    elif t == "set_model":
        model = msg.get("model")
        if isinstance(model, str) and model.strip():
            conv.set_model(model.strip())
    elif t == "compact":
        conv.request_compact()
    elif t == "permission_decision":
        req_id = msg.get("req_id")
        if isinstance(req_id, str):
            conv.resolve_permission(
                req_id,
                allow=bool(msg.get("allow")),
                persist=bool(msg.get("persist")),
                deny_message=str(msg.get("deny_message") or ""),
            )
    elif t == "plan_decision":
        req_id = msg.get("req_id")
        if isinstance(req_id, str):
            mode_after = msg.get("mode_after")
            conv.resolve_plan(
                req_id,
                approved=bool(msg.get("approved")),
                mode_after=mode_after if isinstance(mode_after, str) else None,
                feedback=str(msg.get("feedback") or ""),
            )
