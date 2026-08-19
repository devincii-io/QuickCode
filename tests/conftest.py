"""Suite-wide test infrastructure: the socketpair hang guard and wait helpers.

Nothing here is imported by the application. It exists to make the release gate
finish in seconds and to make the one failure mode that could not fail fast --
a blocked ``accept()`` -- impossible rather than merely loud.
"""

from __future__ import annotations

import asyncio
import socket
import sys
import time
from collections.abc import Callable

# ---------------------------------------------------------------------------
# The socketpair hang
# ---------------------------------------------------------------------------
#
# Windows has no AF_UNIX socketpair, so `socket.socketpair()` falls back to a
# loopback TCP pair: bind a listener on 127.0.0.1:0, connect a non-blocking
# client to it, then `accept()`. That `accept()` has no timeout and no retry,
# and on this platform it does not always return -- a dropped (rather than
# refused) loopback SYN, or a security product intercepting the connect, leaves
# it blocking forever. asyncio builds its event-loop self-pipe with exactly this
# call, so a suite that creates event loops rolls the dice on every one of them,
# and a lost roll used to stall the gate until someone noticed.
#
# The stdlib offers no hook to bound that accept, so this rebuilds the fallback
# with a deadline and a retry. It is the same handshake -- same family, same
# blocking semantics for the returned pair -- only it gives up on a listener
# that has gone deaf and builds a fresh one instead of waiting out the heat
# death of the release.
#
# Test-process only: `conftest.py` is imported before any loop exists, and the
# shipped application is untouched.

# Both ends of this handshake are the same process on the loopback interface,
# so it completes in microseconds when it completes at all. The first attempt is
# therefore given a quarter second, which is already enormous, and a bad roll
# costs only that.
#
# From there the budget doubles rather than repeating. A flat 0.25s x 20 was
# seen failing outright on a machine running the suite beside a load of ConPTY
# spawns: twenty consecutive handshakes missed their quarter-second, not because
# the listener was deaf but because the accepting thread was not being
# scheduled. Escalating gives a busy machine a window wide enough eventually,
# while a genuinely deaf listener still gives up in about fifteen seconds rather
# than hanging until somebody notices.
_ACCEPT_TIMEOUT_S = 0.25
_ACCEPT_TIMEOUT_MAX_S = 2.0
_ACCEPT_ATTEMPTS = 20

_stdlib_socketpair = socket.socketpair


def _socketpair_that_cannot_hang(
    family: int = socket.AF_INET,
    type: int = socket.SOCK_STREAM,  # matches the stdlib signature we replace
    proto: int = 0,
) -> tuple[socket.socket, socket.socket]:
    if not sys.platform.startswith("win") or family not in (socket.AF_INET, socket.AF_INET6):
        # Everywhere else this is a real socketpair(2) syscall, which cannot hang.
        return _stdlib_socketpair(family, type, proto)

    host = "127.0.0.1" if family == socket.AF_INET else "::1"
    last_timeout: BaseException | None = None
    budget = _ACCEPT_TIMEOUT_S
    for _ in range(_ACCEPT_ATTEMPTS):
        lsock = socket.socket(family, type, proto)
        try:
            lsock.bind((host, 0))
            lsock.listen()
            lsock.settimeout(budget)
            addr, port = lsock.getsockname()[:2]
            csock = socket.socket(family, type, proto)
            try:
                # Non-blocking connect so one thread can drive both ends, as in
                # the stdlib fallback this replaces.
                csock.setblocking(False)
                try:
                    csock.connect((addr, port))
                except (BlockingIOError, InterruptedError):
                    pass
                csock.setblocking(True)
                ssock, _peer = lsock.accept()
            except BaseException:
                csock.close()
                raise
        except TimeoutError as exc:
            # The listener never saw the connect. Throw both ends away and try a
            # different ephemeral port rather than waiting on a dead one.
            last_timeout = exc
            budget = min(budget * 2, _ACCEPT_TIMEOUT_MAX_S)
            continue
        finally:
            lsock.close()
        # `accept()` on a socket with a timeout can hand back a non-blocking
        # peer; asyncio's self-pipe requires a blocking one.
        ssock.setblocking(True)
        return ssock, csock

    raise TimeoutError(
        f"socket.socketpair() could not complete its loopback handshake in "
        f"{_ACCEPT_ATTEMPTS} attempts, the last given {budget:g}s"
    ) from last_timeout


socket.socketpair = _socketpair_that_cannot_hang


# ---------------------------------------------------------------------------
# Waiting on the thing itself, not on the clock
# ---------------------------------------------------------------------------
#
# A `sleep(2)` that means "long enough for a subprocess to start" costs two
# seconds on every run forever, and is still a guess. These poll the actual
# condition on a tight interval with a generous ceiling: fast when it works,
# and the failure message still arrives if it never does.

POLL_INTERVAL_S = 0.01


def wait_until(predicate: Callable[[], bool], *, timeout_s: float = 10.0) -> bool:
    """Block until `predicate` is true. Returns whether it became true."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(POLL_INTERVAL_S)
    return predicate()


async def await_until(predicate: Callable[[], bool], *, timeout_s: float = 10.0) -> bool:
    """`wait_until` for a running event loop -- yields instead of blocking it."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(POLL_INTERVAL_S)
    return predicate()
