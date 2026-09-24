"""FastAPI app: REST bootstrap/sessions/models/config, WS attach, static frontend.

Local-only trust boundary (same model as QuickTerm): the API answers the
QuickCode window and nothing else. The Host allowlist defeats DNS-rebinding,
the Origin allowlist defeats cross-origin requests from other sites in the
same browser, and the loopback token (server/auth.py) stops other local
processes. Static frontend files carry no secrets and stay open so the shell
can bootstrap.

Routes come in two shapes over the same handlers: the legacy single-project
paths (``/api/bootstrap``, ``/ws/conversation/{id}``…), which address the hub's
default project, and the ``/api/projects/{project_id}/…`` paths, which address
any open project. The default project is just the launch directory, so the two
shapes never diverge for a single-project run.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response, WebSocket
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketDisconnect

from quickcode.server import auth
from quickcode.server.agents_api import register_agent_routes
from quickcode.server.authoring_api import register_authoring_routes
from quickcode.server.config_api import register_config_routes
from quickcode.server.gitinfo import register_git_routes
from quickcode.server.headers import security_headers
from quickcode.server.http import (
    DEFAULT,
    PROJECT,
    project,
    valid_conv_id,
)
from quickcode.server.kernel_api import register_kernel_routes
from quickcode.server.manager import Client, Conversation, ConversationManager
from quickcode.server.paths import register_path_routes
from quickcode.server.profiles_api import register_profile_routes
from quickcode.server.projects import ProjectHub
from quickcode.server.projects_api import register_project_routes
from quickcode.server.prompt_api import register_prompt_routes
from quickcode.server.sessions_api import register_session_routes, revive
from quickcode.server.terminal import register_terminal_routes
from quickcode.server.update_api import register_update_routes
from quickcode.session.store import (
    SessionStore,
)

log = logging.getLogger("quickcode.server")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


def _allowed_origins(host: str, port: int) -> tuple[set[str], set[str]]:
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
    if host not in ("127.0.0.1", "localhost", "0.0.0.0", "::"):
        hosts.add(f"{host}:{port}")
    return hosts, {f"http://{h}" for h in hosts}


def create_app(
    target: ConversationManager | ProjectHub,
    *,
    host: str = "127.0.0.1",
    port: int = 8642,
    token: str = "",
) -> FastAPI:
    # A bare manager is the single-project shape; wrap it so every handler has
    # exactly one code path.
    hub = target if isinstance(target, ProjectHub) else ProjectHub.from_manager(target)
    app = FastAPI(title="QuickCode", docs_url=None, redoc_url=None)
    allowed_hosts, allowed_origins = _allowed_origins(host, port)
    hardening = security_headers(allowed_hosts)

    def _project(pid: str) -> ConversationManager:
        return project(hub, pid)

    def _token_required(request: Request) -> bool:
        path = request.url.path
        return path.startswith("/api/") and path != "/api/health"

    def _refuse(reason: str) -> Response:
        return Response(f"forbidden: {reason}", status_code=403, headers=hardening)

    @app.middleware("http")
    async def _local_guard(request: Request, call_next):
        if request.headers.get("host", "") not in allowed_hosts:
            return _refuse("bad host")
        origin = request.headers.get("origin")
        if origin is not None and origin not in allowed_origins:
            return _refuse("bad origin")
        if (token and _token_required(request)
                and not auth.matches(request.headers.get(auth.HEADER), token)):
            return _refuse("bad token")
        response = await call_next(request)
        for name, value in hardening.items():
            response.headers.setdefault(name, value)
        path = request.url.path
        if path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        elif not path.startswith("/ws"):
            # Force revalidation for the shell so a stale UI never survives an
            # app update.
            response.headers.setdefault("Cache-Control", "no-cache")
        return response

    def _ws_allowed(ws: WebSocket) -> bool:
        if ws.headers.get("host", "") not in allowed_hosts:
            return False
        origin = ws.headers.get("origin")
        # browsers always send Origin on WS; absent means a native local client
        if not (origin is None or origin in allowed_origins):
            return False
        if token:
            prefix = auth.SUBPROTOCOL_PREFIX
            offered = [p.strip() for p in ws.headers.get("sec-websocket-protocol", "").split(",")]
            if not any(
                auth.matches(p[len(prefix):], token) for p in offered if p.startswith(prefix)
            ):
                return False
        return True

    # ---- REST ----

    @app.get("/api/health")
    def health(challenge: str = "") -> dict:
        from quickcode.cli import __version__

        out: dict[str, Any] = {"app": "quickcode", "version": __version__}
        # Unauthenticated like the rest of this route, and safe to be: the
        # answer is an HMAC under the token, which does not reveal it. See
        # ``auth.instance_proof`` for who asks and why.
        if token and auth.valid_challenge(challenge):
            out["proof"] = auth.instance_proof(token, port, challenge)
        return out

    register_session_routes(app, hub, DEFAULT)
    register_project_routes(app, hub)
    register_session_routes(app, hub, PROJECT)
    register_kernel_routes(app, hub)
    register_profile_routes(app, hub)

    register_prompt_routes(app, hub)
    register_config_routes(app, hub)
    register_update_routes(app, hub)

    # ---- WebSocket ----

    async def _attach(ws: WebSocket, manager: ConversationManager | None, conv_id: str) -> None:
        if not _ws_allowed(ws):
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

    # Both git shapes resolve their manager lazily, so the hub's default is
    # read per request and a project opened after startup is addressable.
    register_git_routes(app, lambda: hub.default, _project)
    register_path_routes(app, lambda: hub.default, _project)
    # Authored plugins: list, create, read, save, delete, validate, duplicate,
    # and the problems array. Registered from their own module for the same
    # reason the git routes are -- app.py's diff for a whole feature is two
    # lines. Declared after the kernel routes so the literal ``authored``
    # segment cannot be read as a plugin id.
    register_authoring_routes(app, lambda: hub.default, _project)
    # The agent workbench: inventory, resolved composition with provenance,
    # preview from an unsaved draft, and the session-scoped switch. Takes the
    # hub rather than the two lambdas because the session routes reach a
    # conversation by id, not only the default project.
    register_agent_routes(app, hub)
    # The terminal panel's sockets. Handed `_ws_allowed` and the token rather
    # than re-deriving them, so there is exactly one WebSocket auth rule in
    # this app and the shell socket is behind that one, not a second copy.
    register_terminal_routes(app, hub, ws_allowed=_ws_allowed, token=token)

    # mounted last so /api and /ws routes win; skipped when frontend/ absent (tests)
    if FRONTEND_DIR.is_dir():
        app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
    return app


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

