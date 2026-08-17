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
import contextlib
import json
import logging
import re
import shutil
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketDisconnect

from quickcode.server import auth
from quickcode.server.gitinfo import register_git_routes
from quickcode.server.manager import Client, Conversation, ConversationManager
from quickcode.server.projects import ProjectHub, list_dirs
from quickcode.session.store import SESSIONS_DIRNAME, SessionStore

log = logging.getLogger("quickcode.server")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
JSON_BODY_CAP = 1024 * 1024
# Conversation ids are generated as hex; anything else in a path segment would
# be a traversal attempt against the sessions directory.
_CONV_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


def _valid_conv_id(conv_id: str) -> bool:
    return _CONV_ID_RE.fullmatch(conv_id) is not None


def _session_count(root: Path) -> int:
    sessions_dir = root / SESSIONS_DIRNAME
    try:
        return sum(1 for _ in sessions_dir.glob("*.jsonl"))
    except OSError:
        return 0


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

    def _project(pid: str) -> ConversationManager:
        manager = hub.get(pid)
        if manager is None:
            raise HTTPException(404, f"unknown project: {pid}")
        return manager

    def _token_required(request: Request) -> bool:
        path = request.url.path
        return path.startswith("/api/") and path != "/api/health"

    @app.middleware("http")
    async def _local_guard(request: Request, call_next):
        if request.headers.get("host", "") not in allowed_hosts:
            return Response("forbidden: bad host", status_code=403)
        origin = request.headers.get("origin")
        if origin is not None and origin not in allowed_origins:
            return Response("forbidden: bad origin", status_code=403)
        if token and _token_required(request) and request.headers.get(auth.HEADER) != token:
            return Response("forbidden: bad token", status_code=403)
        response = await call_next(request)
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
            offered = ws.headers.get("sec-websocket-protocol", "")
            wanted = auth.SUBPROTOCOL_PREFIX + token
            if wanted not in [p.strip() for p in offered.split(",")]:
                return False
        return True

    # ---- REST ----

    @app.get("/api/health")
    def health() -> dict:
        from quickcode.cli import __version__

        return {"app": "quickcode", "version": __version__}

    # ---- per-project payload builders (shared by both route shapes) ----

    def _bootstrap(manager: ConversationManager) -> dict:
        from quickcode.cli import __version__
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
        }

    def _sessions(manager: ConversationManager) -> list[dict]:
        live = set(manager.conversations)
        out = []
        for info in SessionStore.list_sessions(manager.cwd):
            out.append(
                {
                    "conv_id": info.conv_id,
                    "title": info.title,
                    "model": info.model,
                    "mtime": info.mtime,
                    "message_count": info.message_count,
                    "live": info.conv_id in live,
                }
            )
        return out

    async def _open_conversation(manager: ConversationManager, request: Request) -> dict:
        body = await _read_json(request)
        conv_id = body.get("resume") if isinstance(body, dict) else None
        if conv_id is not None and not isinstance(conv_id, str):
            raise HTTPException(400, "resume must be a conversation id string")
        if conv_id is not None and not _valid_conv_id(conv_id):
            raise HTTPException(400, "invalid conversation id")
        conv = manager.open(conv_id)
        return {"conv_id": conv.conv_id}

    async def _models(manager: ConversationManager, refresh: bool) -> list[dict]:
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

    def _delete_session(manager: ConversationManager, conv_id: str) -> Response:
        if not _valid_conv_id(conv_id):
            raise HTTPException(404, "unknown conversation")
        if manager.get(conv_id) is not None:
            raise HTTPException(409, "conversation is live; close it first")
        store = SessionStore(manager.cwd, conv_id)
        if not store.path.exists():
            raise HTTPException(404, "unknown conversation")
        try:
            store.path.unlink()
        except OSError as e:
            raise HTTPException(500, f"could not delete session: {e}") from e
        # The task board lives beside the transcript; deleting one orphans the
        # other, so they go together.
        board_dir = manager.cwd / ".quickcode" / "tasks" / conv_id
        if board_dir.is_dir():
            shutil.rmtree(board_dir, ignore_errors=True)
        return Response(status_code=204)

    # ---- default-project routes (the original single-project API) ----

    @app.get("/api/bootstrap")
    def bootstrap() -> dict:
        return _bootstrap(hub.default)

    @app.get("/api/sessions")
    def sessions() -> list[dict]:
        return _sessions(hub.default)

    @app.post("/api/conversations")
    async def open_conversation(request: Request) -> dict:
        return await _open_conversation(hub.default, request)

    @app.get("/api/models")
    async def models(refresh: bool = False) -> list[dict]:
        return await _models(hub.default, refresh)

    @app.get("/api/plugins")
    def plugins() -> dict:
        return hub.default.plugin_inventory()

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
        body = await _read_json(request)
        path = body.get("path") if isinstance(body, dict) else None
        if not isinstance(path, str) or not path.strip():
            raise HTTPException(400, "body must be {'path': <directory>}")
        try:
            manager = await hub.open(path.strip())
        except NotADirectoryError as e:
            raise HTTPException(400, f"not a directory: {e}") from e
        return {
            "id": hub.id_of(manager),
            "path": str(manager.cwd),
            "name": manager.cwd.name,
        }

    @app.get("/api/dir")
    def browse_dir(path: str | None = None) -> dict:
        try:
            return list_dirs(path)
        except NotADirectoryError as e:
            raise HTTPException(400, f"not a directory: {e}") from e
        except OSError as e:
            raise HTTPException(400, f"cannot read directory: {e}") from e

    # ---- project-scoped routes ----

    @app.get("/api/projects/{pid}/bootstrap")
    def project_bootstrap(pid: str) -> dict:
        return {**_bootstrap(_project(pid)), "id": pid}

    @app.get("/api/projects/{pid}/sessions")
    def project_sessions(pid: str) -> list[dict]:
        return _sessions(_project(pid))

    @app.delete("/api/projects/{pid}/sessions/{conv_id}")
    def project_delete_session(pid: str, conv_id: str) -> Response:
        return _delete_session(_project(pid), conv_id)

    @app.post("/api/projects/{pid}/conversations")
    async def project_open_conversation(pid: str, request: Request) -> dict:
        return await _open_conversation(_project(pid), request)

    @app.get("/api/projects/{pid}/models")
    async def project_models(pid: str, refresh: bool = False) -> list[dict]:
        return await _models(_project(pid), refresh)

    @app.get("/api/projects/{pid}/plugins")
    def project_plugins(pid: str) -> dict:
        return _project(pid).plugin_inventory()

    @app.put("/api/config")
    async def put_config(request: Request) -> Response:
        body = await _read_json(request)
        if not isinstance(body, dict):
            raise HTTPException(400, "request body must be a JSON object")
        # Config is per install, not per project: the default manager's handle
        # is the same object every project shares.
        cfg = hub.config
        theme = body.get("theme")
        if isinstance(theme, dict):
            cfg.theme = {
                k: v for k, v in theme.items() if isinstance(k, str) and isinstance(v, str)
            }
        default_mode = body.get("default_mode")
        if isinstance(default_mode, str):
            cfg.default_mode = default_mode
        base_url = body.get("base_url")
        if isinstance(base_url, str) and base_url.strip():
            cfg.profile.base_url = base_url.strip()
        cfg.save()
        return Response(status_code=204)

    @app.post("/api/apikey")
    async def put_api_key(request: Request) -> Response:
        from quickcode import secrets

        body = await _read_json(request)
        key = body.get("key") if isinstance(body, dict) else None
        if not isinstance(key, str) or not key.strip():
            raise HTTPException(400, "body must be {'key': <non-empty string>}")
        secrets.save_api_key(key.strip())
        return Response(status_code=204)

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
            if not _valid_conv_id(conv_id):
                await ws.close(code=4404)
                return
            store = SessionStore(manager.cwd, conv_id)
            if not store.path.exists():
                await ws.close(code=4404)
                return
            conv = manager.open(conv_id)

        client = Client()
        # Attach BEFORE snapshotting the log: anything logged after this point
        # reaches the live queue, and the client dedupes replays by seq.
        conv.clients.add(client)
        try:
            await ws.send_text(json.dumps(conv.state_event(), ensure_ascii=False))
            await ws.send_text('{"type": "replay_start"}')
            for ev in conv.store.load_events():
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


async def _pump_out(ws: WebSocket, client: Client) -> None:
    while True:
        item = await client.queue.get()
        if item is None:  # overflow sentinel: force a clean replay reconnect
            await ws.close(code=1013, reason="client fell behind; reconnect to replay")
            return
        await ws.send_text(item)


async def _pump_in(ws: WebSocket, conv: Conversation) -> None:
    while True:
        raw = await ws.receive_text()
        try:
            msg = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue  # malformed frame: drop it rather than kill the socket
        if not isinstance(msg, dict):
            continue
        _dispatch(conv, msg)


def _dispatch(conv: Conversation, msg: dict[str, Any]) -> None:
    t = msg.get("type")
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


async def _read_json(request: Request, maximum: int = JSON_BODY_CAP) -> Any:
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
