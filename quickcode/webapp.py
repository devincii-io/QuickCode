"""Web app launcher: uvicorn on a loopback port + default browser window.

The default ``quickcode`` invocation lands here. It assembles the shared
provider and plugin tools, opens the launch directory as the default project
in a ``ProjectHub`` (further projects are opened on demand by the UI), starts
the FastAPI app on 127.0.0.1, and opens the frontend with the auth token in
the URL fragment (never sent to the server, never logged).
"""

from __future__ import annotations

import asyncio
import logging
import socket
import sys
import webbrowser
from pathlib import Path

import uvicorn

from quickcode.config import Config, Environment
from quickcode.plugins import loader
from quickcode.server import auth
from quickcode.server.app import create_app
from quickcode.server.projects import ProjectHub

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


async def _serve(
    *,
    cwd: Path,
    config: Config,
    env: Environment,
    allow_yolo: bool,
    default_mode: str | None,
    port: int,
    open_browser: bool,
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
    if open_browser:
        # Give uvicorn a beat to bind before the browser asks.
        loop = asyncio.get_running_loop()
        loop.call_later(0.4, webbrowser.open, url)
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
    initial_resume: str | None = None,
) -> None:
    chosen = port if port is not None else (
        DEFAULT_PORT if _port_available(DEFAULT_PORT) else _free_port()
    )
    asyncio.run(
        _serve(
            cwd=cwd,
            config=config,
            env=env,
            allow_yolo=allow_yolo,
            default_mode=default_mode,
            port=chosen,
            open_browser=open_browser,
            initial_resume=initial_resume,
        )
    )
