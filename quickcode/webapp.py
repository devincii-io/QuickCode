"""Web app launcher: uvicorn on a loopback port + a native app window.

The default ``quickcode`` invocation lands here. It assembles the shared
provider and plugin tools, opens the launch directory as the default project
in a ``ProjectHub`` (further projects are opened on demand by the UI), starts
the FastAPI app on 127.0.0.1, and opens the frontend with the auth token in
the URL fragment (never sent to the server, never logged).

The UI shows up in a native window (:mod:`quickcode.ui.window`); that window
owns the main thread, so the server moves to a background one. Without
pywebview we fall back to the default browser and the server keeps the main
thread, as before.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import socket
import sys
import threading
from collections.abc import Callable
from pathlib import Path

import uvicorn

from quickcode.config import Config, Environment
from quickcode.plugins import loader
from quickcode.server import auth
from quickcode.server.app import create_app
from quickcode.server.projects import ProjectHub
from quickcode.ui import window

log = logging.getLogger("quickcode.webapp")

DEFAULT_PORT = 8642


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _port_available(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _running_instance_health(port: int) -> dict | None:
    """The ``/api/health`` payload if a QuickCode instance answers on `port`, else None."""
    import json
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=0.6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) and data.get("app") == "quickcode" else None


def _hand_off_to_running_instance(port: int, cwd: Path) -> bool:
    """Ask an already-running QuickCode to open `cwd` instead of starting a
    second server and window on it. One instance already juggles multiple
    projects through its ProjectHub, so a second process on the same port
    only duplicates provider clients and risks racing the same on-disk
    session files. Best-effort: any failure here just falls through to
    starting our own instance, exactly as before this existed.
    """
    import json
    import urllib.request

    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/projects/open",
        data=json.dumps({"path": str(cwd)}).encode("utf-8"),
        headers={"Content-Type": "application/json", auth.HEADER: auth.get_or_create_token()},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=2.0) as resp:
            if resp.status != 200:
                return False
    except Exception:
        log.warning("running-instance project hand-off failed", exc_info=True)
        return False
    window.focus_existing()
    if sys.stdout is not None:
        print(f"QuickCode is already running; opened {cwd} there.")
    return True


async def _serve(
    *,
    cwd: Path,
    config: Config,
    env: Environment,
    allow_yolo: bool,
    default_mode: str | None,
    port: int,
    on_ready: Callable[[str, uvicorn.Server], None] | None,
    initial_resume: str | None,
) -> None:
    profile = config.profile
    provider = loader.make_provider(profile.provider, profile.base_url, profile.api_key)

    hub = ProjectHub(
        config=config,
        provider=provider,
        allow_yolo=allow_yolo,
        default_mode=default_mode,
        plugin_tools=loader.load_tool_plugins(),
    )
    # The launch directory is the default project: registered, opened, and its
    # model catalog warmed so context lengths are known at first open.
    await hub.open(cwd, make_default=True, env=env)

    token = auth.get_or_create_token()
    app = create_app(hub, host="127.0.0.1", port=port, token=token)

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )

    url = f"http://127.0.0.1:{port}/#token={token}&project={hub.default_id}"
    if initial_resume:
        url += f"&resume={initial_resume}"
    if on_ready is not None:
        # Give uvicorn a beat to bind before the window asks.
        loop = asyncio.get_running_loop()
        loop.call_later(0.4, on_ready, url, server)
    # `quickcode-app` runs under pythonw, where a GUI process has no console
    # and sys.stdout is None; the URL only goes to a console that exists.
    if sys.stdout is not None:
        print(f"QuickCode running at {url}")

    try:
        await server.serve()
    finally:
        await hub.close()


def run_webapp(
    *,
    cwd: Path,
    config: Config,
    env: Environment,
    allow_yolo: bool = False,
    default_mode: str | None = None,
    port: int | None = None,
    open_browser: bool = True,
    native: bool = True,
    initial_resume: str | None = None,
) -> None:
    # A launch that didn't ask for a specific port hands off to an already-
    # running instance instead of quietly starting a second one (see
    # _hand_off_to_running_instance). An explicit --port is left alone: that
    # is someone deliberately running a second, independent instance.
    default_port_free = port is None and _port_available(DEFAULT_PORT)
    if port is None and not default_port_free:
        if _running_instance_health(DEFAULT_PORT) is not None:
            if _hand_off_to_running_instance(DEFAULT_PORT, cwd):
                return
    chosen = port if port is not None else (
        DEFAULT_PORT if default_port_free else _free_port()
    )
    serve = functools.partial(
        _serve,
        cwd=cwd,
        config=config,
        env=env,
        allow_yolo=allow_yolo,
        default_mode=default_mode,
        port=chosen,
        initial_resume=initial_resume,
    )

    if open_browser and native and window.available():
        _run_windowed(serve)
        return

    def _open(url: str, _server: uvicorn.Server) -> None:
        window.open_in_browser(url)

    asyncio.run(serve(on_ready=_open if open_browser else None))


def _run_windowed(serve: Callable[..., object]) -> None:
    """Serve on a background thread and give the main thread to the window."""
    ready = threading.Event()
    state: dict[str, object] = {}

    def _on_ready(url: str, server: uvicorn.Server) -> None:
        state["url"] = url
        state["server"] = server
        ready.set()

    def _serve_forever() -> None:
        try:
            asyncio.run(serve(on_ready=_on_ready))
        except Exception:
            log.exception("server stopped")
        finally:
            # Unblock a start-up that never got its URL, so the launch fails
            # loudly instead of hanging on a window that will never open.
            ready.set()

    thread = threading.Thread(target=_serve_forever, name="quickcode-server", daemon=True)
    thread.start()
    if not ready.wait(30) or "url" not in state:
        raise RuntimeError("QuickCode server did not start")

    def _shutdown() -> None:
        server = state.get("server")
        if server is not None:
            server.should_exit = True  # polled by uvicorn's loop

    window.run(str(state["url"]), on_close=_shutdown)
    # Closing the window shuts the agent down: wait for the server to unwind
    # its sessions rather than killing them with the process.
    thread.join(timeout=10)
