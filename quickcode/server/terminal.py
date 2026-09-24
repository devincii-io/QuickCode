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
import math
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, WebSocket
from starlette.websockets import WebSocketDisconnect

from quickcode import subproc
from quickcode.pty import registry
from quickcode.pty.interactive import InteractivePty
from quickcode.pty.session import PtyError
from quickcode.pty.shells import interactive_shell_argv

log = logging.getLogger("quickcode.server.terminal")

# A single keystroke frame is a keystroke, a paste, or somebody's clipboard
# accident. Anything past this is not input a human produced.
MAX_INPUT_CHARS = 1 << 16
# How much output may be on its way to the browser before the pty stops being
# read. The browser acknowledges what it has drawn (`ack` frames); until it
# does, `yes` blocks on its own write instead of the server reading megabytes
# a second into memory the tab then has to swallow. Small enough that Ctrl+C
# lands on a screen that is nearly caught up, large enough that a build log
# never waits on a round trip.
OUTPUT_WINDOW = 1 << 18
# After the shell exits its last bytes can still be in flight. They are
# forwarded until the pty has been quiet this long, and for no longer than
# EXIT_DRAIN_MAX_S in all, before the exit is announced.
EXIT_DRAIN_S = 0.2
EXIT_DRAIN_MAX_S = 3.0

DEFAULT_ROWS = 24
DEFAULT_COLS = 80

# The seam tests spawn something cheaper through. Read at call time on purpose:
# a test monkeypatches this name, and the real app never touches it.
shell_argv: Callable[[], list[str]] = interactive_shell_argv


# The pty's size is the truth about the terminal's size. A COLUMNS inherited
# from wherever QuickCode was launched would override it for every program
# that checks the environment first, and they would wrap at the wrong width.
_STALE = ("COLUMNS", "LINES")


def _shell_env() -> dict[str, str]:
    """The child's environment: every child's (``subproc.child_env``, so none of
    the app's credentials), plus the terminal's own promises.

    ``TERM`` is what makes ls, git and grep emit colour at all — without it a
    program in a pty assumes a dumb terminal and helpfully turns everything
    off, which would leave the panel's ANSI renderer with nothing to render.
    """
    env = subproc.child_env()
    for name in _STALE:
        env.pop(name, None)
    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"
    return env


def _dimension(value: Any, default: int) -> int:
    """A size from the client, or ``default`` if it is not a finite number.

    ``json.loads`` accepts ``Infinity``, and ``int(inf)`` raises
    ``OverflowError`` — which nothing caught, so one resize frame ended the
    whole terminal session.
    """
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return default
    try:
        number = float(value)
    except ValueError:
        return default
    return int(number) if math.isfinite(number) else default


class _Outbox:
    """Text from the pty's thread, coalesced into as few frames as possible.

    The pty is read at whatever speed the shell writes; a WebSocket send is
    an await. Without a buffer between them a build log becomes ten thousand
    tiny frames and the browser spends its time in JSON.parse. This collects
    whatever arrived since the last send and hands it over in one string. It
    is bounded by the pty's output window, not here: the pty stops being read
    once ``OUTPUT_WINDOW`` characters are unacknowledged.
    """

    def __init__(self) -> None:
        self._parts: list[str] = []
        self._ready = asyncio.Event()

    def push(self, text: str) -> None:
        self._parts.append(text)
        self._ready.set()

    def nudge(self) -> None:
        """Wake a waiting ``drain`` with nothing, so it can notice an exit."""
        self._ready.set()

    async def drain(self, timeout: float | None = None) -> str:
        """Everything pushed so far, once there is something ("" on timeout)."""
        if timeout is None:
            await self._ready.wait()
        else:
            try:
                await asyncio.wait_for(self._ready.wait(), timeout)
            except TimeoutError:
                return ""
        return self.take()

    def take(self) -> str:
        text = "".join(self._parts)
        self._parts.clear()
        self._ready.clear()
        return text


async def serve_terminal(
    ws: WebSocket,
    cwd: Any,
    *,
    ws_allowed: Callable[[WebSocket], bool],
    token: str,
) -> None:
    """Accept one terminal socket, run a shell in ``cwd`` until either ends.

    An app built without a token serves no terminal at all. Everywhere else a
    missing token means a local-only convenience; here it would mean any
    process on the machine that can forge a Host header gets a shell.
    """
    from quickcode.server import auth

    if not token or not ws_allowed(ws):
        await ws.close(code=4403)
        return
    await ws.accept(subprotocol=auth.SUBPROTOCOL_PREFIX + token)
    if cwd is None:
        await ws.close(code=4404)
        return

    loop = asyncio.get_running_loop()
    outbox = _Outbox()
    exited = asyncio.Event()
    exit_code: list[int | None] = [None]

    def on_output(text: str) -> None:
        # Called on the pty's thread; hop to the loop before touching asyncio.
        loop.call_soon_threadsafe(outbox.push, text)

    def on_exit(code: int | None) -> None:
        def mark() -> None:
            exit_code[0] = code
            exited.set()
            outbox.nudge()

        loop.call_soon_threadsafe(mark)

    argv = shell_argv()
    pty = InteractivePty(argv, cwd=str(cwd), env=_shell_env(),
                         dimensions=(DEFAULT_ROWS, DEFAULT_COLS), window=OUTPUT_WINDOW)
    # Registered and owned by the `finally` from before the spawn: a client
    # that left before `terminal_ready`, or a shutdown during the spawn, used
    # to leave a shell nothing would ever close.
    registry.add(cwd, pty)
    try:
        try:
            await asyncio.to_thread(pty.start, on_output, on_exit)
        except PtyError as exc:
            # No pty backend, or the shell is not installed. Say which, on the
            # socket, rather than closing with a code the user has to guess at.
            log.warning("terminal: could not start %s: %s", argv, exc)
            await ws.send_text(json.dumps({"type": "terminal_error", "message": str(exc)}))
            await ws.close(code=4500)
            return
        await ws.send_text(json.dumps({
            "type": "terminal_ready",
            "cwd": str(cwd),
            "shell": argv[0],
            "pid": pty.pid,
        }, ensure_ascii=False))
        await _run(ws, pty, outbox, exited, exit_code)
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    finally:
        registry.discard(cwd, pty)
        # Off the loop: ending a session gives it a moment to hang up, and
        # every other project's socket is served by this same loop.
        await asyncio.shield(asyncio.to_thread(pty.close))


async def _run(
    ws: WebSocket,
    pty: InteractivePty,
    outbox: _Outbox,
    exited: asyncio.Event,
    exit_code: list[int | None],
) -> None:
    loop = asyncio.get_running_loop()

    async def send_output(text: str) -> None:
        await ws.send_text(json.dumps({"type": "output", "data": text}, ensure_ascii=False))

    async def pump_out() -> None:
        while not exited.is_set():
            text = await outbox.drain()
            if text:
                await send_output(text)
        # The shell is gone, but what it said last may still be on its way
        # through the pty — and, with the window, may be waiting on an ack.
        # Keep forwarding until the pty goes quiet, then say so.
        deadline = loop.time() + EXIT_DRAIN_MAX_S
        while loop.time() < deadline:
            text = await outbox.drain(EXIT_DRAIN_S)
            if not text:
                break
            await send_output(text)
        with contextlib.suppress(Exception):
            await ws.send_text(json.dumps({"type": "exit", "code": exit_code[0]}))
            await ws.close()

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
            elif kind == "ack":
                count = msg.get("chars")
                if isinstance(count, int) and not isinstance(count, bool) and count > 0:
                    pty.ack(min(count, OUTPUT_WINDOW))
            elif kind == "resize":
                pty.resize(_dimension(msg.get("rows"), DEFAULT_ROWS),
                           _dimension(msg.get("cols"), DEFAULT_COLS))

    tasks = [asyncio.ensure_future(pump_out()), asyncio.ensure_future(pump_in())]
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
