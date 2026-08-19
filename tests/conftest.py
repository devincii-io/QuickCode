"""Suite-wide test infrastructure: the socketpair hang guard, the sandboxed
``~/.quickcode``, and wait helpers.

Nothing here is imported by the application. It exists to make the release gate
finish in seconds, to make the one failure mode that could not fail fast --
a blocked ``accept()`` -- impossible rather than merely loud, and to make it
impossible for a test to write into the developer's own QuickCode state.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

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


# ---------------------------------------------------------------------------
# The developer's ~/.quickcode is not a test fixture
# ---------------------------------------------------------------------------
#
# `ProjectHub` used to default its registry to the real
# `~/.quickcode/projects.json`, so every test that opened a project registered
# that project for real. It was found when the home screen listed 258 pytest
# temp directories among 264 "recent projects" -- unusable, and only noticed
# because the number got absurd.
#
# Fixing that one default fixes that one leak. This exists because the next
# such default will be written by somebody who did not know this happened.
#
# The hard part is that `CONFIG_DIR` is a *value*, not a lookup:
# `config.py` computes `Path.home() / ".quickcode"` at import, and at least
# seven modules copy it into their own module namespace
# (`kernel.state`, `security.trust`, `server.projects`, `server.auth`,
# `update` -- which further derives `CACHE_PATH` and `DOWNLOAD_DIR` --
# `secrets.SECRETS_DIR`, `subagents.definitions._USER_DIR`). Patching
# `quickcode.config.CONFIG_DIR` alone therefore redirects almost nothing, which
# is why the several tests that already patch it have to name two or three
# modules by hand and still only cover the ones their own test touches.
#
# So the redirect is found by value rather than by name: every loaded
# `quickcode.*` module is swept for module-level `Path` attributes that point at
# the real directory, and each one is rebound at a sandbox for the duration of
# the test. A module added tomorrow that captures `CONFIG_DIR` the same way is
# covered the day it is written, with nothing to remember -- a hand-maintained
# list of names is exactly the thing this guard exists to not depend on.
#
# Rejected: patching `Path.home()`. It would catch paths derived lazily too, but
# it lies to every test that legitimately asks where home is (`test_app_entrypoint`
# asserts the launcher passes `--cwd $HOME`; `/api/dir` reports it), and it would
# not move a single one of the constants above, which were computed before any
# test ran. The redirect below is the narrower and more complete instrument.
#
# Known gap: only module-level names are swept. A real path stored in a class
# attribute, a dict or a default argument would survive -- the session snapshot
# below is what catches that, one run late.

REAL_CONFIG_DIR = Path.home() / ".quickcode"
"""The developer's actual QuickCode directory.

Exported so a test that needs the real *value* -- to assert where the default
lives, or that nothing was written there -- can read it. Reading is fine; it is
writing that this file exists to prevent.
"""

# `webview` is Chromium's own profile cache, handed to the embedded browser as a
# storage path and written by it, not by any Python the suite exercises. It
# churns whenever the developer has QuickCode open, so including it in the
# snapshot would mean a guard that cries wolf on a normal working machine.
_SNAPSHOT_SKIP_TOPLEVEL = {"webview"}


def _snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """Every file under `root` as path -> (size, mtime_ns). Missing root: empty."""
    out: dict[str, tuple[int, int]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        if Path(dirpath) == root:
            dirnames[:] = [d for d in dirnames if d not in _SNAPSHOT_SKIP_TOPLEVEL]
        for name in filenames:
            p = Path(dirpath, name)
            try:
                st = p.stat()
            except OSError:
                continue  # vanished mid-walk; the other end of the diff will show it
            out[str(p)] = (st.st_size, st.st_mtime_ns)
    return out


def _captured_real_paths() -> list[tuple[object, str, Path]]:
    """Every `quickcode.*` module-level `Path` that points into the real dir."""
    found: list[tuple[object, str, Path]] = []
    for name, module in list(sys.modules.items()):
        if module is None or not name.startswith("quickcode"):
            continue
        try:
            members = list(vars(module).items())
        except TypeError:
            continue
        for attr, value in members:
            if not isinstance(value, Path):
                continue
            if value == REAL_CONFIG_DIR or REAL_CONFIG_DIR in value.parents:
                found.append((module, attr, value))
    return found


@pytest.fixture(scope="session")
def sandbox_config_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The throwaway `~/.quickcode` every test gets instead of the real one.

    One directory for the whole session rather than a fresh one per test,
    because constants derived from it at import time (`update.CACHE_PATH`) are
    computed once and must keep naming a directory that still exists. It is
    emptied before each test instead, which buys the same isolation.
    """
    return tmp_path_factory.mktemp("quickcode-home")


@pytest.fixture(autouse=True)
def _user_state_is_sandboxed(
    monkeypatch: pytest.MonkeyPatch, sandbox_config_dir: Path
) -> None:
    for child in sandbox_config_dir.iterdir():
        # Tolerant: a test that left a handle open on Windows must not take the
        # next test down with it, and a stale file in the sandbox is harmless.
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            try:
                child.unlink()
            except OSError:
                pass
    for module, attr, value in _captured_real_paths():
        rel = value.relative_to(REAL_CONFIG_DIR)
        redirected = sandbox_config_dir if rel == Path(".") else sandbox_config_dir / rel
        monkeypatch.setattr(module, attr, redirected)


@pytest.fixture(scope="session", autouse=True)
def _the_real_config_dir_is_never_written() -> Iterator[None]:
    """Prove the redirect above worked, rather than trusting that it did.

    A redirect nobody checks is a redirect that quietly stops covering the one
    module somebody added since. This compares the real directory before and
    after the run and fails the session if a single byte or mtime moved.

    It can also fire because the developer had QuickCode itself open while the
    suite ran, which touches `sessions/` and `projects.json` legitimately. The
    message says so; the file list says which.
    """
    before = _snapshot(REAL_CONFIG_DIR)
    yield
    after = _snapshot(REAL_CONFIG_DIR)
    if before == after:
        return
    changed = sorted(
        set(before) ^ set(after)
        | {k for k in before.keys() & after.keys() if before[k] != after[k]}
    )
    raise AssertionError(
        "the test run changed the developer's real "
        f"{REAL_CONFIG_DIR}:\n  " + "\n  ".join(changed)
        + "\n\nSomething reached past the sandbox in tests/conftest.py -- most "
        "likely a real path captured somewhere the module-level sweep cannot "
        "see. (If QuickCode itself was running during this suite, it wrote "
        "these and the guard is innocent; re-run with the app closed.)"
    )
