"""Minimal Model Context Protocol client (stdio transport).

Speaks JSON-RPC 2.0 over a child process's stdin/stdout, newline-delimited.
Each configured server is spawned once per open project; its tools are wrapped
as ordinary ``Tool`` instances named ``mcp__<server>__<tool>`` (see
``mcp_wire.tool_name`` for names that do not fit a provider) so they flow
through the same registry, permission gate, and trace log as built-ins.

Server config (Claude-compatible shape) lives in the project's
``.quickcode/settings.json`` or the user config:

    {"mcpServers": {"docs": {"command": "npx", "args": ["-y", "some-mcp"],
                             "env": {"KEY": "..."}}}}

What the session guarantees, because a server is someone else's program:

* every request ends -- in a result, the server's error, a timeout, or
  ``MCPError`` the moment the process is gone -- rather than waiting out the
  timeout on a server that has already exited;
* a message from the server that carries a ``method`` is a request or a
  notification, never a reply, even when its id matches one we are waiting on;
  ``ping`` is answered and anything else we did not advertise is refused;
* a server that dies is started again on the next call, a bounded number of
  times, and never after ``stop()`` -- which is what revoking trust calls.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

from quickcode import jsonfile, subproc
from quickcode.plugins import mcp_process, mcp_wire
from quickcode.providers.base import ToolSchema
from quickcode.tools.base import PermissionSpec, Tool, ToolCtx, ToolResult, truncate

log = logging.getLogger("quickcode.mcp")

PROTOCOL_VERSION = "2025-06-18"
# Older revisions whose tools/* surface this client speaks unchanged.
KNOWN_PROTOCOL_VERSIONS = frozenset({"2024-11-05", "2025-03-26", "2025-06-18"})
CALL_TIMEOUT_S = 120
INIT_TIMEOUT_S = 30
RESULT_LIMIT = 50_000
MAX_RESTARTS = 3


class MCPError(RuntimeError):
    pass


def _client_version() -> str:
    try:
        from importlib.metadata import version

        return version("quickcode")
    except Exception:
        return "0"


class MCPServer:
    """One MCP server process and its JSON-RPC session."""

    def __init__(self, name: str, command: str, args: list[str], env: dict[str, str],
                 *, scope: str = "user", cwd: Path | None = None,
                 may_restart: Callable[[], bool] | None = None) -> None:
        self.name = name
        # "project" when the project's own settings declared it: what its trust
        # grant covers, and what revoking that grant stops.
        self.scope = scope
        self.command = command
        self.args = args
        self.env = env
        self.cwd = cwd
        self._may_restart = may_restart or (lambda: True)
        self.proc: asyncio.subprocess.Process | None = None
        self.tools: list[dict[str, Any]] = []
        self.protocol_version = ""
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stderr = mcp_process.StderrTail(list(env.values()))
        self._alive = False
        self._closed = False
        self._restarts = 0
        self._lock = asyncio.Lock()

    # ---- lifecycle ----
    async def start(self) -> None:
        # What its config declares, on top of what every child gets -- which is
        # the app's environment without QuickCode's own credentials.
        env = subproc.child_env(self.env)
        self._stderr = mcp_process.StderrTail(list(self.env.values()))
        self.proc = await mcp_process.spawn(self.command, self.args, env, self.cwd)
        self._reader_task = asyncio.create_task(self._read_loop(self.proc))
        self._stderr_task = asyncio.create_task(self._stderr.drain(self.proc.stderr))
        self._alive = True
        try:
            await self._initialize()
            result = await self._request("tools/list", {}, INIT_TIMEOUT_S)
        except TimeoutError as exc:
            raise MCPError(self._why("did not answer in time")) from exc
        tools = result.get("tools") if isinstance(result, dict) else None
        self.tools = [t for t in tools or [] if isinstance(t, dict) and t.get("name")]

    async def _initialize(self) -> None:
        result = await self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "quickcode", "version": _client_version()},
            },
            INIT_TIMEOUT_S,
        )
        version = result.get("protocolVersion", "") if isinstance(result, dict) else ""
        self.protocol_version = str(version)
        if self.protocol_version not in KNOWN_PROTOCOL_VERSIONS:
            log.warning("MCP server %s speaks protocol %r; continuing as %s",
                        self.name, self.protocol_version, PROTOCOL_VERSION)
        await self._notify("notifications/initialized", {})

    async def stop(self) -> None:
        """End the server for good: no call after this restarts it."""
        self._closed = True
        await self._teardown(MCPError(f"MCP server {self.name} was stopped"))

    async def _teardown(self, reason: Exception) -> None:
        self._alive = False
        self._fail_pending(reason)
        await mcp_process.shutdown(self.proc)
        for task in (self._reader_task, self._stderr_task):
            if task is not None and task is not asyncio.current_task():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    async def _ensure_running(self) -> None:
        if self._alive:
            return
        if self._closed:
            raise MCPError(f"MCP server {self.name} was stopped")
        async with self._lock:
            if self._alive:
                return
            if self._restarts >= MAX_RESTARTS:
                raise MCPError(self._why(
                    f"exited and has been restarted {MAX_RESTARTS} times; not again"))
            if not self._may_restart():
                raise MCPError(f"MCP server {self.name} exited, and its project is no "
                               "longer trusted, so it was not restarted")
            self._restarts += 1
            log.warning("MCP server %s exited; restarting (%d/%d)",
                        self.name, self._restarts, MAX_RESTARTS)
            await self._teardown(MCPError(f"MCP server {self.name} is restarting"))
            try:
                await self.start()
            except Exception as exc:
                await self._teardown(MCPError(f"MCP server {self.name} failed to restart"))
                raise MCPError(self._why(f"could not be restarted: {exc}")) from exc

    def _why(self, what: str) -> str:
        tail = self._stderr.text()
        return f"MCP server {self.name} {what}" + (f"; stderr: {tail}" if tail else "")

    # ---- JSON-RPC plumbing ----
    async def _send(self, obj: dict[str, Any]) -> None:
        proc = self.proc
        if proc is None or proc.stdin is None or not self._alive:
            raise MCPError(self._why("is not running"))
        try:
            proc.stdin.write(json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n")
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, RuntimeError, OSError) as exc:
            raise MCPError(self._why(f"stopped reading its input ({exc})")) from exc

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def _request(self, method: str, params: dict[str, Any], timeout: float) -> Any:
        self._next_id += 1
        rid = self._next_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            return await asyncio.wait_for(fut, timeout)
        except TimeoutError:
            with contextlib.suppress(Exception):
                await self._notify("notifications/cancelled",
                                   {"requestId": rid, "reason": "timed out"})
            raise
        finally:
            self._pending.pop(rid, None)

    def _fail_pending(self, reason: Exception) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(reason)

    async def _read_loop(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        while True:
            try:
                line = await proc.stdout.readline()
            except ValueError:
                # Over the line limit: the message is gone and its id with it,
                # so every waiter is told rather than left to time out.
                self._fail_pending(MCPError(self._why("sent a message too large to read")))
                continue
            except Exception:
                line = b""
            if not line:
                break
            try:
                msg = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue  # a stray log line on stdout
            for item in msg if isinstance(msg, list) else [msg]:
                if isinstance(item, dict):
                    await self._dispatch(item)
        if proc is self.proc:
            self._alive = False
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), 1)
            code = proc.returncode
            self._fail_pending(MCPError(self._why(
                "exited" + (f" with code {code}" if code is not None else ""))))

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        if "method" in msg:
            if "id" in msg:
                await self._answer(msg)
            return  # a notification: nothing here subscribes to any
        rid = msg.get("id")
        if isinstance(rid, str) and rid.isdigit():
            rid = int(rid)
        fut = self._pending.get(rid) if isinstance(rid, int) else None
        if fut is None or fut.done():
            return  # a reply to something we stopped waiting for
        if "error" in msg:
            err = msg["error"]
            text = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            fut.set_exception(MCPError(str(text)))
        else:
            fut.set_result(msg.get("result", {}))

    async def _answer(self, msg: dict[str, Any]) -> None:
        """A request *from* the server. We advertise no client capabilities, so
        only ``ping`` has an answer; the rest are refused rather than ignored,
        which would leave the server waiting on us."""
        if msg.get("method") == "ping":
            reply: dict[str, Any] = {"jsonrpc": "2.0", "id": msg["id"], "result": {}}
        else:
            reply = {"jsonrpc": "2.0", "id": msg["id"],
                     "error": {"code": -32601, "message": "Method not found"}}
        with contextlib.suppress(MCPError):
            await self._send(reply)

    # ---- tool invocation ----
    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        await self._ensure_running()
        result = await self._request(
            "tools/call", {"name": tool_name, "arguments": arguments}, CALL_TIMEOUT_S)
        return mcp_wire.render_result(result)


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
        self._tool = str(spec.get("name", ""))
        self.name = mcp_wire.tool_name(server.name, self._tool)
        self.description = spec.get("description", "") or f"MCP tool {self._tool}"
        self._schema = mcp_wire.input_schema(spec.get("inputSchema"))
        annotations = spec.get("annotations") or {}
        if isinstance(annotations, dict) and annotations.get("readOnlyHint"):
            self.is_read_only = True
        # An MCP server's arguments are its own; there is no field we can point
        # a path or command rule at, so gating is by tool name alone -- and a
        # tool that has not declared itself read-only is prompted for.
        self.permission = PermissionSpec(mutates=not self.is_read_only, executes=True)
        self.source = "config"

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
            detail = str(e) or f"no answer within {CALL_TIMEOUT_S}s"
            return ToolResult(content=f"MCP call failed: {detail}", is_error=True)
        return ToolResult(content=truncate(content, RESULT_LIMIT), is_error=is_error)


def adapters(server: MCPServer) -> list[Tool]:
    """One adapter per distinct tool the server listed."""
    seen: set[str] = set()
    out: list[Tool] = []
    for spec in server.tools:
        adapter = MCPToolAdapter(server, spec)
        if adapter.name not in seen:
            seen.add(adapter.name)
            out.append(adapter)
    return out


def _read_mcp_servers(path) -> dict[str, dict[str, Any]]:
    """The ``mcpServers`` block of one settings file, or ``{}``."""
    out: dict[str, dict[str, Any]] = {}
    try:
        data = jsonfile.load(path)
    except (OSError, ValueError):
        return out
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if isinstance(servers, dict):
        for name, spec in servers.items():
            if isinstance(spec, dict) and isinstance(spec.get("command"), str):
                out[str(name)] = spec
    return out


def user_server_configs() -> dict[str, dict[str, Any]]:
    """mcpServers declared in the user's own config dir. Never gated: these are
    the user's files, not a cloned repository's."""
    from quickcode.config import CONFIG_DIR

    return _read_mcp_servers(CONFIG_DIR / "settings.json")


def project_server_configs(cwd) -> dict[str, dict[str, Any]]:
    """mcpServers declared by the project itself. Executable-bearing config that
    the trust gate governs — see :mod:`quickcode.security.trust`."""
    from quickcode.security import trust

    return trust.project_mcp_servers(cwd)


def load_server_configs(cwd) -> dict[str, dict[str, Any]]:
    """Merge mcpServers from user config dir and project settings files.

    This is the *display* view (what the Settings/kernel page lists), and is
    intentionally scope-blind: it reports every declared server whether or not
    the trust gate will actually spawn it. Spawning is decided in
    :func:`connect_servers`, which honours the gate.
    """
    return {**user_server_configs(), **project_server_configs(cwd)}


def _server(name: str, spec: dict[str, Any], scope: str, cwd, may_restart) -> MCPServer:
    args = spec.get("args", [])
    env = spec.get("env")
    return MCPServer(
        name=name,
        command=spec["command"],
        # A string here would otherwise be spread one character per argument.
        args=[str(a) for a in args] if isinstance(args, list) else [],
        env={str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else {},
        scope=scope,
        # A project's server runs in that project, so "./server.js" means the
        # project's file however many projects this process has open.
        cwd=Path(cwd) if scope == "project" and cwd is not None else None,
        may_restart=may_restart,
    )


async def _spawn(server: MCPServer) -> MCPServer | None:
    try:
        await server.start()
    except Exception as e:
        log.warning("MCP server %s failed to start: %s", server.name, e)
        await server.stop()
        return None
    return server


async def _spawn_all(servers: list[MCPServer]) -> tuple[list[MCPServer], list[Tool]]:
    """Start them side by side: an ``npx`` server can take seconds to come up,
    and opening a project waited for each one in turn."""
    started = [s for s in await asyncio.gather(*(_spawn(s) for s in servers)) if s]
    tools: list[Tool] = []
    for server in started:
        tools.extend(adapters(server))
    return started, tools


def configured_servers(cwd, *, store=None) -> tuple[list[MCPServer], list[str]]:
    """The servers the trust gate lets this project start, not yet started,
    and the names of the project-scope servers it held back.

    User-scope servers always start. Project-scope servers start **only if the
    project is trusted** (:mod:`quickcode.security.trust`); until then they are
    inert and are reported -- never spawned -- so that opening an untrusted
    repository can never be code execution. A trusted project's server shadows
    a user server of the same name, as it does in the display view.

    ``store`` is injectable so tests can point the gate at a temp trust file.
    """
    from quickcode.security import trust

    store = store if store is not None else trust.default_store()

    project = project_server_configs(cwd)
    trusted = bool(project) and store.is_trusted(cwd)
    held = [] if trusted else sorted(project)
    pending = [_server(name, spec, "user", cwd, None)
               for name, spec in user_server_configs().items()
               if not (trusted and name in project)]
    if trusted:
        pending += _project_servers(cwd, project, store)
    return pending, held


async def connect_servers(cwd, *, store=None) -> tuple[list[MCPServer], list[Tool]]:
    """Spawn the servers :func:`configured_servers` lets this project start.
    A failing server logs and is skipped."""
    pending, held = configured_servers(cwd, store=store)
    if held:
        log.warning(
            "project %s is not trusted; %d MCP server(s) left inert: %s",
            cwd, len(held), ", ".join(held),
        )
    return await _spawn_all(pending)


async def connect_project_servers(cwd, *, store=None) -> tuple[list[MCPServer], list[Tool]]:
    """Spawn only the project-scope servers, unconditionally.

    The caller has already decided the project is trusted (e.g. trust was just
    granted for an open project). Used to activate MCP servers live without a
    reopen; user-scope servers are untouched because they are already running.
    """
    from quickcode.security import trust

    store = store if store is not None else trust.default_store()
    return await _spawn_all(_project_servers(cwd, project_server_configs(cwd), store))


def _project_servers(cwd, configs: dict[str, dict[str, Any]], store) -> list[MCPServer]:
    def still_trusted() -> bool:
        try:
            return bool(store.is_trusted(cwd))
        except Exception:
            return False

    return [_server(name, spec, "project", cwd, still_trusted)
            for name, spec in configs.items()]
