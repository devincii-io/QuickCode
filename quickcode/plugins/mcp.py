"""Minimal Model Context Protocol client (stdio transport).

Speaks JSON-RPC 2.0 over a child process's stdin/stdout, newline-delimited.
Each configured server is spawned once per QuickCode run; its tools are
wrapped as ordinary ``Tool`` instances named ``mcp__<server>__<tool>`` so they
flow through the same registry, permission gate, and trace log as built-ins.

Server config (Claude-compatible shape) lives in the project's
``.quickcode/settings.json`` or the user config:

    {"mcpServers": {"docs": {"command": "npx", "args": ["-y", "some-mcp"],
                             "env": {"KEY": "..."}}}}
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

from quickcode.providers.base import ToolSchema
from quickcode.tools.base import Tool, ToolCtx, ToolResult, truncate

log = logging.getLogger("quickcode.mcp")

PROTOCOL_VERSION = "2025-06-18"
CALL_TIMEOUT_S = 120
INIT_TIMEOUT_S = 30
RESULT_LIMIT = 50_000


class MCPError(RuntimeError):
    pass


class MCPServer:
    """One running MCP server process and its JSON-RPC session."""

    def __init__(self, name: str, command: str, args: list[str], env: dict[str, str]) -> None:
        self.name = name
        self.command = command
        self.args = args
        self.env = env
        self.proc: asyncio.subprocess.Process | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self.tools: list[dict[str, Any]] = []

    async def start(self) -> None:
        env = dict(os.environ)
        env.update(self.env)
        self.proc = await asyncio.create_subprocess_exec(
            self.command,
            *self.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        await asyncio.wait_for(self._initialize(), INIT_TIMEOUT_S)
        result = await asyncio.wait_for(self._request("tools/list", {}), INIT_TIMEOUT_S)
        self.tools = result.get("tools", [])

    async def _initialize(self) -> None:
        await self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "quickcode", "version": "2.0"},
            },
        )
        self._notify("notifications/initialized", {})

    async def stop(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except TimeoutError:
                self.proc.kill()

    # ---- JSON-RPC plumbing ----
    def _send(self, obj: dict[str, Any]) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write(json.dumps(obj, ensure_ascii=False).encode() + b"\n")

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        rid = self._next_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        try:
            return await fut
        finally:
            self._pending.pop(rid, None)

    async def _read_loop(self) -> None:
        assert self.proc and self.proc.stdout
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(MCPError(f"MCP server {self.name} exited"))
                return
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            rid = msg.get("id")
            fut = self._pending.get(rid) if rid is not None else None
            if fut is None or fut.done():
                continue  # notification or stale reply
            if "error" in msg:
                err = msg["error"]
                fut.set_exception(MCPError(err.get("message", str(err))))
            else:
                fut.set_result(msg.get("result", {}))

    # ---- tool invocation ----
    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        result = await asyncio.wait_for(
            self._request("tools/call", {"name": tool_name, "arguments": arguments}),
            CALL_TIMEOUT_S,
        )
        parts: list[str] = []
        for item in result.get("content", []):
            kind = item.get("type")
            if kind == "text":
                parts.append(item.get("text", ""))
            else:
                parts.append(f"[{kind} content omitted]")
        return "\n".join(parts), bool(result.get("isError"))


class _PassthroughInput(BaseModel):
    """MCP tools carry their own JSON schema; accept anything and let the
    server validate."""

    model_config = ConfigDict(extra="allow")


class MCPToolAdapter(Tool[_PassthroughInput]):
    """Wraps one server-side tool as a registry ``Tool``."""

    Input = _PassthroughInput
    is_read_only: ClassVar[bool] = False  # unknown side effects → permission-gated

    def __init__(self, server: MCPServer, spec: dict[str, Any]) -> None:
        self._server = server
        self._tool = spec.get("name", "")
        self.name = f"mcp__{server.name}__{self._tool}"
        self.description = spec.get("description", "") or f"MCP tool {self._tool}"
        self._schema = spec.get("inputSchema") or {"type": "object", "properties": {}}
        annotations = spec.get("annotations") or {}
        if annotations.get("readOnlyHint"):
            self.is_read_only = True

    def schema(self) -> ToolSchema:
        return ToolSchema(name=self.name, description=self.description, parameters=self._schema)

    def render_call(self, input: _PassthroughInput) -> str:  # noqa: A002
        args = input.model_dump(exclude_none=True)
        return f"⏺ {self.name} {json.dumps(args, ensure_ascii=False)[:200]}"

    async def run(self, input: _PassthroughInput, ctx: ToolCtx) -> ToolResult:  # noqa: A002
        try:
            content, is_error = await self._server.call_tool(
                self._tool, input.model_dump(exclude_none=True)
            )
        except (MCPError, TimeoutError) as e:
            return ToolResult(content=f"MCP call failed: {e}", is_error=True)
        return ToolResult(content=truncate(content, RESULT_LIMIT), is_error=is_error)


def load_server_configs(cwd) -> dict[str, dict[str, Any]]:
    """Merge mcpServers from user config dir and project settings files."""
    from pathlib import Path

    from quickcode.config import CONFIG_DIR

    merged: dict[str, dict[str, Any]] = {}
    candidates = [
        CONFIG_DIR / "settings.json",
        Path(cwd) / ".quickcode" / "settings.json",
        Path(cwd) / ".quickcode" / "settings.local.json",
    ]
    for p in candidates:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        servers = data.get("mcpServers")
        if isinstance(servers, dict):
            for name, spec in servers.items():
                if isinstance(spec, dict) and isinstance(spec.get("command"), str):
                    merged[str(name)] = spec
    return merged


async def connect_servers(cwd) -> tuple[list[MCPServer], list[Tool]]:
    """Spawn every configured server; a failing server logs and is skipped."""
    servers: list[MCPServer] = []
    tools: list[Tool] = []
    for name, spec in load_server_configs(cwd).items():
        server = MCPServer(
            name=name,
            command=spec["command"],
            args=[str(a) for a in spec.get("args", [])],
            env={str(k): str(v) for k, v in (spec.get("env") or {}).items()},
        )
        try:
            await server.start()
        except Exception as e:
            log.warning("MCP server %s failed to start: %s", name, e)
            await server.stop()
            continue
        servers.append(server)
        tools.extend(MCPToolAdapter(server, t) for t in server.tools)
    return servers, tools
