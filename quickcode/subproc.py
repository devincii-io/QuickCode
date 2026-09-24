"""How QuickCode starts a child process, and how it ends one.

Every program QuickCode runs goes through here -- the agent's shell commands
and background jobs, authored command tools, hooks, MCP servers, the terminal
panel, git and ripgrep -- so that these hold for all of them without each call
site having to remember:

* **No console window.** ``QuickCodeApp.exe`` is built windowed
  (``console=False`` in ``quickcode.spec``) so no console flashes up behind the
  app. The consequence is that every console program it spawns has no console
  to inherit, so Windows **allocates a new one**, and it appears on screen for
  as long as the command runs. The git panel refreshes on a timer, so the user
  saw a terminal blink at them every few seconds. ``CREATE_NO_WINDOW`` is the
  fix, and there is no process-wide setting for it.
* **None of QuickCode's credentials.** The app's environment may hold its model
  and search API keys, and the model can ask for ``echo
  $QUICKCODE_OPENROUTER_API_KEY`` as easily as for ``ls``. ``child_env`` is that
  environment without them -- and, in a frozen build, without the loader path
  PyInstaller pointed at the bundle.
* **Nothing to read but what it is given.** A managed child's stdin is the null
  device unless the caller feeds it. QuickCode's own stdin is the console it was
  started from, if anything; a command that read it (``git commit`` without
  ``-m``, a prompt) sat there until its timeout.
* **A tree that can be ended.** ``spawn`` and ``spawn_async`` start the child
  as the leader of a process group of its own (POSIX), which is what lets
  ``kill_tree`` reach what it started; on Windows ``taskkill /T`` walks the
  tree instead.

``tests/test_no_console_window.py`` fails on a spawn anywhere else. The one
exception is a program the user is meant to see: the installer has its own
window and wants one (see ``update.py``).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import signal
import subprocess
import sys
from collections.abc import Mapping, Sequence
from typing import Any

IS_WINDOWS = sys.platform == "win32"

# Zero everywhere else: POSIX rejects any *non-zero* creationflags, so passing
# this unconditionally keeps the call sites free of platform branches.
NO_WINDOW: int = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0

PIPE = subprocess.PIPE
STDOUT = subprocess.STDOUT
DEVNULL = subprocess.DEVNULL

# How long a killed tree's pipes may take to close. A grandchild that escaped
# the process group keeps them open for as long as it lives, so this is a bound
# rather than a wait for EOF.
DRAIN_S = 5.0
_READ_SIZE = 65536

# A credential by the shape of its name: ``_secret_names`` lists the ones
# QuickCode reads today, this catches the next one before anybody lists it.
_SECRET_NAME = re.compile(r"^QUICKCODE_\w*(KEY|TOKEN|SECRET|PASSWORD)$")


def _secret_names() -> set[str]:
    from quickcode.search.resolve import provider_infos
    from quickcode.secrets import API_KEY_ENV, PROVIDER_KEY_ENV

    return ({API_KEY_ENV, *PROVIDER_KEY_ENV.values()}
            | {i.api_key_env for i in provider_infos() if i.api_key_env})


def child_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """QuickCode's environment as a child may see it, with ``extra`` on top.

    A key the user exported in their own shell profile is still theirs: a login
    shell re-reads it. What this stops is the app handing its copy to every
    program it starts. ``extra`` is what the caller means this child to have --
    an MCP server's configured ``env``, a hook's project directory -- so it wins
    over what was inherited and is not filtered.
    """
    secret = _secret_names()
    env = {
        name: value
        for name, value in os.environ.items()
        if name.upper() not in secret and not _SECRET_NAME.match(name.upper())
    }
    if getattr(sys, "frozen", False):
        _unfreeze(env)
    if extra:
        env.update(extra)
    return env


def _unfreeze(env: dict[str, str]) -> None:
    """Undo what the PyInstaller bootloader did to the app's own environment.

    It points the loader path at the bundle and leaves ``_PYI_*`` markers for
    its own children. What QuickCode starts is not one: a system binary must
    not load the bundled libssl, and another frozen program must not take the
    markers as its own.
    """
    for name in [n for n in env if n.startswith("_PYI_") or n == "_MEIPASS2"]:
        del env[name]
    for name in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"):
        original = env.pop(name + "_ORIG", None)
        if original is not None:
            env[name] = original
        else:
            env.pop(name, None)


def _no_window(kwargs: dict[str, Any]) -> dict[str, Any]:
    kwargs["creationflags"] = kwargs.pop("creationflags", 0) | NO_WINDOW
    if kwargs.get("env") is None:
        kwargs["env"] = child_env()
    return kwargs


def _managed(kwargs: dict[str, Any]) -> dict[str, Any]:
    kwargs = _no_window(kwargs)
    if not IS_WINDOWS:
        # Its own session, so its own process group (pgid == pid): see kill_tree.
        kwargs["start_new_session"] = True
    return kwargs


def run(argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess:
    """``subprocess.run`` for a one-shot program: git, ripgrep, taskkill."""
    return subprocess.run(list(argv), **_no_window(kwargs))  # noqa: S603 - argv is caller-controlled


def spawn(
    argv: Sequence[str], *, cwd: str | None = None, env: Mapping[str, str] | None = None,
    stdin: Any = DEVNULL, stdout: Any = PIPE, stderr: Any = PIPE, **kwargs: Any,
) -> subprocess.Popen:
    """Start a child QuickCode manages: it reads the output and may kill the tree."""
    return subprocess.Popen(  # noqa: S603 - argv is caller-controlled
        list(argv),
        **_managed({"cwd": cwd, "env": env, "stdin": stdin, "stdout": stdout,
                    "stderr": stderr, **kwargs}),
    )


async def spawn_async(
    argv: Sequence[str], *, cwd: str | None = None, env: Mapping[str, str] | None = None,
    stdin: Any = DEVNULL, stdout: Any = PIPE, stderr: Any = PIPE, **kwargs: Any,
) -> asyncio.subprocess.Process:
    """``spawn`` for a caller on the event loop."""
    program, *args = argv
    return await asyncio.create_subprocess_exec(
        program, *args,
        **_managed({"cwd": cwd, "env": env, "stdin": stdin, "stdout": stdout,
                    "stderr": stderr, **kwargs}),
    )


async def communicate(
    proc: asyncio.subprocess.Process, data: bytes | None = None, *,
    timeout: float, drain_s: float = DRAIN_S,
) -> tuple[bytes, bytes, bool]:
    """Feed ``data``, read both streams, wait for the exit: ``(out, err, timed_out)``.

    Unlike ``Process.communicate``, a timeout keeps what had already arrived:
    the tree is killed, and read until its pipes close or ``drain_s`` runs out.
    Being cancelled kills the tree too, before the cancellation goes on -- the
    child would otherwise run on with nobody reading its pipes.
    """
    out, err = bytearray(), bytearray()
    work = asyncio.gather(
        _feed(proc.stdin, data), _collect(proc.stdout, out), _collect(proc.stderr, err),
        proc.wait(),
    )
    timed_out = False
    try:
        done, _ = await asyncio.wait({work}, timeout=timeout)
        if not done:
            timed_out = True
            await kill_tree_async(proc.pid)
            await asyncio.wait({work}, timeout=drain_s)
    except asyncio.CancelledError:
        await kill_tree_async(proc.pid)
        raise
    finally:
        if not work.done():
            work.cancel()
    if work.done() and not work.cancelled():
        work.result()
    return bytes(out), bytes(err), timed_out


async def _feed(stream: asyncio.StreamWriter | None, data: bytes | None) -> None:
    if stream is None:
        return
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        if data:
            stream.write(data)
            await stream.drain()
    stream.close()


async def _collect(stream: asyncio.StreamReader | None, into: bytearray) -> None:
    if stream is None:
        return
    while chunk := await stream.read(_READ_SIZE):
        into += chunk


def kill_tree(pid: int | None, *, graceful: bool = False) -> None:
    """End a child ``spawn`` started and everything it started. Never raises.

    POSIX: the child leads a process group of its own, and the group is
    signalled by that id rather than looked up from the pid. Once the child
    has exited and been reaped the lookup finds nothing -- while what it
    started (``cmd &``) lives on in the group -- or finds whoever has the pid
    now. The group's id cannot be reused while any member of it lives.
    ``graceful`` sends SIGTERM rather than SIGKILL.

    Windows: ``taskkill /T /F`` walks the tree. There is no gentler form that
    reaches a console program with no window, so ``graceful`` changes nothing.
    """
    if not pid:
        return
    if IS_WINDOWS:
        with contextlib.suppress(Exception):
            run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, timeout=10)
        return
    with contextlib.suppress(OSError):
        os.killpg(pid, signal.SIGTERM if graceful else signal.SIGKILL)


async def kill_tree_async(pid: int | None, *, graceful: bool = False) -> None:
    """``kill_tree`` for a caller on the event loop. Never raises either."""
    # taskkill is a process of its own and can take seconds; a signal cannot.
    if IS_WINDOWS:
        with contextlib.suppress(RuntimeError):  # the executor is shutting down
            await asyncio.to_thread(kill_tree, pid, graceful=graceful)
    else:
        kill_tree(pid, graceful=graceful)
