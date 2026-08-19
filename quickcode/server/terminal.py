"""The terminal panel's server side: one interactive shell per WebSocket.

This is the *user's* terminal, not the agent's. That distinction decides
everything about the module:

* **No permission engine.** The permission gate exists because the model
  proposes commands; here a person is typing into their own shell on their own
  machine, and asking them to approve their own keystrokes would be theatre.
  A terminal is an unrestricted shell by definition.
* **Therefore the model must never be able to reach it.** The only way in is
  this WebSocket, and it is protected exactly like the conversation socket:
  Host + Origin allowlist and the loopback token as a subprotocol
  (``server/auth.py``). No tool opens sockets — ``web_fetch`` refuses loopback
  and private addresses outright — and no frame on the conversation socket is
  routed here. The panel's "run this in my terminal" button (frontend) inserts
  text at the prompt *without* a newline, so even a command the model wrote
  needs a human keystroke to execute.
* **One shell per socket.** There is no terminal id to guess and no shared
  session to attach to: a connection to ``/ws/projects/{pid}/terminal`` spawns
  its own shell in that project's directory and owns it. Closing the socket
  kills the process tree.

Rejected: keeping a named, resumable terminal per project that survives the
socket. It would need an id in the URL, a lifetime nobody owns, and a policy
for two windows racing for the same shell — all to save re-opening a prompt.
The panel reconnects instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, WebSocket
from starlette.websockets import WebSocketDisconnect

from quickcode.pty import registry
from quickcode.pty.interactive import InteractivePty, interactive_shell_argv
from quickcode.pty.session import PtyError

log = logging.getLogger("quickcode.server.terminal")

# A single keystroke frame is a keystroke, a paste, or somebody's clipboard
# accident. Anything past this is not input a human produced.
MAX_INPUT_CHARS = 1 << 16
# How much unsent output may pile up before the oldest is dropped. `yes` into a
# terminal produces megabytes a second, and the socket must not become the
# thing that runs the machine out of memory. The browser is showing the tail
# anyway, so the tail is what is kept.
MAX_PENDING_CHARS = 1 << 20
# After the shell exits its last bytes can still be in flight; wait this long
# for them before announcing the exit rather than truncating a goodbye.
EXIT_DRAIN_S = 0.2

DEFAULT_ROWS = 24
DEFAULT_COLS = 80

# The seam tests spawn something cheaper through. Read at call time on purpose:
# a test monkeypatches this name, and the real app never touches it.
shell_argv: Callable[[], list[str]] = interactive_shell_argv


def _shell_env() -> dict[str, str]:
    """The child's environment: the app's, plus the terminal's own promises.

    ``TERM`` is what makes ls, git and grep emit colour at all — without it a
    program in a pty assumes a dumb terminal and helpfully turns everything
    off, which would leave the panel's ANSI renderer with nothing to render.
    """
    env = dict(os.environ)
    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"
    return env


class _Outbox:
    """Bytes from the reader thread, coalesced into as few frames as possible.

    The pty reader runs at whatever speed the shell writes; a WebSocket send is
    an await. Without a buffer between them a build log becomes ten thousand
    tiny frames and the browser spends its time in JSON.parse. This collects
    whatever arrived since the last send and hands it over in one string.
    """

    def __init__(self) -> None:
        self._parts: list[str] = []
        self._size = 0
        self._ready = asyncio.Event()
        self.dropped = 0

    def push(self, text: str) -> None:
        self._parts.append(text)
        self._size += len(text)
        while self._size > MAX_PENDING_CHARS and len(self._parts) > 1:
            self._size -= len(self._parts.pop(0))
            self.dropped += 1
        self._ready.set()

    async def drain(self) -> str:
        await self._ready.wait()
        self._ready.clear()
        text = "".join(self._parts)
        self._parts.clear()
        self._size = 0
        return text

    def take(self) -> str:
        text = "".join(self._parts)
        self._parts.clear()
        self._size = 0
        self._ready.clear()
        return text


async def serve_terminal(
    ws: WebSocket,
    cwd: Any,
    *,
    ws_allowed: Callable[[WebSocket], bool],
    token: str,
) -> None:
    """Accept one terminal socket, run a shell in ``cwd`` until either ends."""
    from quickcode.server import auth

    if not ws_allowed(ws):
        await ws.close(code=4403)
        return
    await ws.accept(subprotocol=(auth.SUBPROTOCOL_PREFIX + token) if token else None)
    if cwd is None:
        await ws.close(code=4404)
        return

    loop = asyncio.get_running_loop()
    outbox = _Outbox()
    exited = asyncio.Event()
    exit_code: list[int | None] = [None]

    def on_output(text: str) -> None:
        # Called on the reader thread; hop to the loop before touching asyncio.
        loop.call_soon_threadsafe(outbox.push, text)

    def on_exit(code: int | None) -> None:
        def mark() -> None:
            exit_code[0] = code
            exited.set()

        loop.call_soon_threadsafe(mark)

    argv = shell_argv()
    pty = InteractivePty(argv, cwd=str(cwd), env=_shell_env(),
                         dimensions=(DEFAULT_ROWS, DEFAULT_COLS))
    try:
        await asyncio.to_thread(pty.start, on_output, on_exit)
    except PtyError as exc:
        # No pty backend, or the shell is not installed. Say which, on the
        # socket, rather than closing with a code the user has to guess at.
        log.warning("terminal: could not start %s: %s", argv, exc)
        await ws.send_text(json.dumps({"type": "terminal_error", "message": str(exc)}))
        await ws.close(code=4500)
        return

    registry.add(cwd, pty)
    await ws.send_text(json.dumps({
        "type": "terminal_ready",
        "cwd": str(cwd),
        "shell": argv[0],
        "pid": pty.pid,
    }, ensure_ascii=False))

    try:
        await _run(ws, pty, outbox, exited, exit_code)
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    finally:
        registry.discard(cwd, pty)
        pty.close()


async def _run(
    ws: WebSocket,
    pty: InteractivePty,
    outbox: _Outbox,
    exited: asyncio.Event,
    exit_code: list[int | None],
) -> None:
    async def pump_out() -> None:
        while True:
            text = await outbox.drain()
            if text:
                await ws.send_text(
                    json.dumps({"type": "output", "data": text}, ensure_ascii=False)
                )

    async def pump_in() -> None:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if not isinstance(msg, dict):
                continue
            kind = msg.get("type")
            if kind == "input":
                data = msg.get("data")
                if isinstance(data, str) and data:
                    pty.write(data[:MAX_INPUT_CHARS])
            elif kind == "resize":
                with contextlib.suppress(TypeError, ValueError):
                    pty.resize(int(msg.get("rows", DEFAULT_ROWS)),
                               int(msg.get("cols", DEFAULT_COLS)))

    tasks = [
        asyncio.ensure_future(pump_out()),
        asyncio.ensure_future(pump_in()),
        asyncio.ensure_future(exited.wait()),
    ]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
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
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    if not exited.is_set():
        return  # the socket went first; nothing left to tell anyone
    # The shell is gone. Its last words may still be arriving on the reader
    # thread, so give them a beat before saying so, then say so.
    await asyncio.sleep(EXIT_DRAIN_S)
    tail = outbox.take()
    with contextlib.suppress(Exception):
        if tail:
            await ws.send_text(json.dumps({"type": "output", "data": tail}, ensure_ascii=False))
        await ws.send_text(json.dumps({"type": "exit", "code": exit_code[0]}))
        await ws.close()


def register_terminal_routes(
    app: FastAPI,
    hub: Any,
    *,
    ws_allowed: Callable[[WebSocket], bool],
    token: str,
) -> None:
    """Mount the terminal sockets beside the conversation ones.

    ``ws_allowed`` and ``token`` are passed in rather than re-derived: there is
    one WebSocket auth rule in this app and this route uses *that* one, not a
    second implementation of it that could drift.
    """

    @app.websocket("/ws/terminal")
    async def ws_terminal(ws: WebSocket) -> None:
        manager = hub.default
        await serve_terminal(
            ws, manager.cwd if manager else None, ws_allowed=ws_allowed, token=token
        )

    @app.websocket("/ws/projects/{pid}/terminal")
    async def ws_project_terminal(ws: WebSocket, pid: str) -> None:
        manager = hub.get(pid)
        await serve_terminal(
            ws, manager.cwd if manager else None, ws_allowed=ws_allowed, token=token
        )

    # Shutdown is not hooked here. A shell outlives the server that spawned it
    # unless somebody kills it, but the place that knows the server is going
    # down is ``ProjectHub.close`` — which also knows about the project being
    # *forgotten*, the other way a terminal can be orphaned. Both call into
    # ``pty.registry``; see the note there on why the dependency runs that way.
