"""Web app launcher: uvicorn on a loopback port + default browser window.

The default ``quickcode`` invocation lands here. It assembles the provider,
plugin tools, and MCP servers, starts the FastAPI app on 127.0.0.1, and opens
the frontend with the auth token in the URL fragment (never sent to the
server, never logged).
"""

from __future__ import annotations

import asyncio
import logging
import socket
import webbrowser
from pathlib import Path

import uvicorn

from quickcode.config import Config, Environment
from quickcode.plugins import loader, mcp
from quickcode.server import auth
from quickcode.server.app import create_app
from quickcode.server.manager import ConversationManager
from quickcode.tools.registry import ToolRegistry, default_registry

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

    plugin_tools = loader.load_tool_plugins()
    servers, mcp_tools = await mcp.connect_servers(cwd)
    extra = [*plugin_tools, *mcp_tools]

    def registry_factory() -> ToolRegistry:
        reg = default_registry()
        for t in extra:
            reg.tools[t.name] = t
        return reg

    manager = ConversationManager(
        cwd=cwd,
        config=config,
        env=env,
        provider=provider,
        allow_yolo=allow_yolo,
        default_mode=default_mode,
        registry_factory=registry_factory,
    )
    manager.mcp_servers = [s.name for s in servers]
    # Warm the model catalog so context lengths are known at first open.
    await manager.models()

    token = auth.get_or_create_token()
    app = create_app(manager, host="127.0.0.1", port=port, token=token)

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )

    url = f"http://127.0.0.1:{port}/#token={token}"
    if initial_resume:
        url += f"&resume={initial_resume}"
    if open_browser:
        # Give uvicorn a beat to bind before the browser asks.
        loop = asyncio.get_running_loop()
        loop.call_later(0.4, webbrowser.open, url)
    print(f"QuickCode running at {url}")

    try:
        await server.serve()
    finally:
        await manager.close()
        for s in servers:
            await s.stop()


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
