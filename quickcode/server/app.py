"""FastAPI app: the loopback trust boundary, and the route modules behind it.

Local-only trust boundary (same model as QuickTerm): the API answers the
QuickCode window and nothing else. The Host allowlist defeats DNS-rebinding,
the Origin allowlist defeats cross-origin requests from other sites in the
same browser, and the loopback token (server/auth.py) stops other local
processes. Static frontend files carry no secrets and stay open so the shell
can bootstrap.

The routes live in their own modules and are registered from here. Most come in
two shapes over one handler (``http.scoped``): the legacy single-project paths
(``/api/bootstrap``, ``/ws/conversation/{id}``…), which address the hub's
default project, and the ``/api/projects/{project_id}/…`` paths, which address
any open project. The default project is just the launch directory, so the two
shapes never diverge for a single-project run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response, WebSocket
from fastapi.staticfiles import StaticFiles

from quickcode.server import auth
from quickcode.server.agents_api import register_agent_routes
from quickcode.server.authoring_api import register_authoring_routes
from quickcode.server.checkpoints_api import register_checkpoint_routes
from quickcode.server.config_api import register_config_routes
from quickcode.server.gitinfo import register_git_routes
from quickcode.server.headers import security_headers
from quickcode.server.hooks_api import register_hooks_routes
from quickcode.server.http import DEFAULT, PROJECT, project, register_error_handlers
from quickcode.server.jobs_api import register_job_routes
from quickcode.server.kernel_api import register_kernel_routes
from quickcode.server.manager import ConversationManager
from quickcode.server.paths import register_path_routes
from quickcode.server.permissions_api import register_permission_routes
from quickcode.server.profiles_api import register_profile_routes
from quickcode.server.projects import ProjectHub
from quickcode.server.projects_api import register_project_routes
from quickcode.server.prompt_api import register_prompt_routes
from quickcode.server.sessions_api import register_session_routes
from quickcode.server.terminal import register_terminal_routes
from quickcode.server.update_api import register_update_routes
from quickcode.server.ws import register_client_message as register_client_message
from quickcode.server.ws import register_ws_routes

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
    app = FastAPI(title="QuickCode", docs_url=None, redoc_url=None, openapi_url=None)
    register_error_handlers(app)
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
        from quickcode import __version__

        out: dict[str, Any] = {"app": "quickcode", "version": __version__}
        # Unauthenticated like the rest of this route, and safe to be: the
        # answer is an HMAC under the token, which does not reveal it. See
        # ``auth.instance_proof`` for who asks and why.
        if token and auth.valid_challenge(challenge):
            out["proof"] = auth.instance_proof(token, port, challenge)
        return out

    # Registration order is match order, and these keep the order the routes
    # were declared in when they all lived in this file -- which is why the
    # session routes' two shapes sit on either side of the project registry's.
    register_session_routes(app, hub, DEFAULT)
    register_project_routes(app, hub)
    register_session_routes(app, hub, PROJECT)
    register_kernel_routes(app, hub)
    register_profile_routes(app, hub)
    register_prompt_routes(app, hub)
    register_checkpoint_routes(app, hub)
    register_config_routes(app, hub)
    register_update_routes(app, hub)
    register_ws_routes(app, hub, ws_allowed=_ws_allowed, token=token)
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
    register_hooks_routes(app, lambda: hub.default, _project)
    # The agent workbench: inventory, resolved composition with provenance,
    # preview from an unsaved draft, and the session-scoped switch. Takes the
    # hub rather than the two lambdas because the session routes reach a
    # conversation by id, not only the default project.
    register_agent_routes(app, hub)
    # The permission dry run ("why was I prompted?"), against the real engine.
    register_permission_routes(app, hub)
    # The Jobs tab: a conversation's background shell jobs, read and killed.
    register_job_routes(app, hub)
    # The terminal panel's sockets. Handed `_ws_allowed` and the token rather
    # than re-deriving them, so there is exactly one WebSocket auth rule in
    # this app and the shell socket is behind that one, not a second copy.
    register_terminal_routes(app, hub, ws_allowed=_ws_allowed, token=token)

    # mounted last so /api and /ws routes win; skipped when frontend/ absent (tests)
    if FRONTEND_DIR.is_dir():
        app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
    return app
