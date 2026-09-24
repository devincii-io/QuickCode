"""MCP servers for one headless turn: ``quickcode -p``.

The app starts a project's servers when the project opens and stops them when
it closes; ``-p`` is a whole session in one process, so it does both around
its one turn. Everything the app guarantees holds here too, and three things
are added because nobody is watching a window:

* **The same gate.** Which servers start is ``mcp.configured_servers``: the
  user's own always, the project's only when the project is trusted.
* **A bounded start.** Servers start side by side, and the turn waits at most
  ``START_TIMEOUT_S`` for them. One that is not up by then is stopped and the
  run goes on without its tools -- a hung ``npx`` must not become a hung
  script.
* **Said, not logged.** Every server that did not start, and why, comes back
  as a note for stderr, including the project servers the trust gate held.

``stop()`` ends every server this run created, whether or not it came up, so
an interrupt during start-up leaves no process behind.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from pathlib import Path

from quickcode.plugins import mcp
from quickcode.tools.base import Tool

START_TIMEOUT_S = 30.0


@dataclass
class TurnServers:
    servers: list[mcp.MCPServer]
    held: list[str] = field(default_factory=list)
    tools: list[Tool] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    async def start(self, timeout: float | None = None) -> None:
        """Start every server, wait up to ``timeout`` seconds, keep what came up."""
        timeout = START_TIMEOUT_S if timeout is None else timeout
        if self.held:
            self.notes.append(
                "this project is not trusted, so its MCP server"
                f"{'s' if len(self.held) != 1 else ''} {', '.join(self.held)} did not "
                "start; trust the project in the app to use them")
        if not self.servers:
            return
        tasks = {asyncio.create_task(s.start()): s for s in self.servers}
        try:
            await asyncio.wait(tasks, timeout=timeout)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        for task, server in tasks.items():
            if task.cancelled():
                why = f"did not start within {timeout:g}s"
            elif task.exception() is not None:
                why = f"did not start ({task.exception()})"
            else:
                self.tools.extend(mcp.adapters(server))
                continue
            self.notes.append(f"MCP server {server.name} {why}; running without its tools")
            # Not left running beside the turn it is no part of.
            await self._stop(server)

    async def stop(self) -> None:
        """End every server this run created and has not ended. Never raises."""
        for server in list(self.servers):
            await self._stop(server)

    async def _stop(self, server: mcp.MCPServer) -> None:
        with contextlib.suppress(Exception):
            await server.stop()
        self.servers.remove(server)


def plan(cwd: Path, *, store=None) -> TurnServers:
    """The servers the trust gate lets this project start, none started yet."""
    servers, held = mcp.configured_servers(cwd, store=store)
    return TurnServers(servers=servers, held=held)
