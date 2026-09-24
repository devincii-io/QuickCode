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
* **A program from ``PATH``, never from the current directory.** Handed a bare
  name, ``CreateProcess`` -- and ``shutil.which`` -- look in the current
  directory first on Windows, and QuickCode's is wherever it was started: the
  repository, for ``qc`` run in one. A program committed there would run in
  place of the real one before the project was trusted. So every spawn here
  resolves a bare name to an absolute path first (``resolve_program``).

``tests/test_no_console_window.py`` fails on a spawn anywhere else, and
``tests/test_program_lookup.py`` on a lookup that goes around the resolver. The
one exception is a program the user is meant to see: the installer has its own
window and wants one (see ``update.py``).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
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

# What CreateProcess itself can start. A .bat or .cmd is handed to cmd.exe,
# which re-parses the arguments (see security/launch.py), so the programs
# QuickCode names itself never resolve to one.
_EXECUTABLE_EXTS = (".com", ".exe")
_DEFAULT_PATHEXT = ".COM;.EXE;.BAT;.CMD"


class ProgramNotFound(FileNotFoundError):
    """A program named without a path that no trusted ``PATH`` directory holds."""


def find_program(
    name: str, *, env: Mapping[str, str] | None = None,
    cwd: str | os.PathLike[str] | None = None, windows: bool | None = None,
    pathext: str | None = None, pathsep: str | None = None, batch: bool = False,
) -> str | None:
    """Where the bare program ``name`` is, from ``PATH`` alone; None if nowhere.

    Unlike ``shutil.which`` and ``CreateProcess`` on Windows, never the current
    directory: a relative ``PATH`` entry is skipped everywhere (``.``, or the
    empty one a POSIX shell reads as ``.``), and on Windows so is an entry that
    *is* the current directory or ``cwd``. On Windows only a ``.exe`` or
    ``.com`` is accepted unless ``batch``. A name with a path in it is not
    looked up at all (None).

    ``env`` is the environment the child will get, for its ``PATH`` and
    ``PATHEXT``. ``windows``, ``pathext`` and ``pathsep`` default to this
    machine's; tests pass Windows' to exercise its rules anywhere.
    """
    windows = IS_WINDOWS if windows is None else windows
    if not _is_bare(name, windows):
        return None
    env = os.environ if env is None else env
    search = _env_value(env, "PATH", windows)
    if search is None:
        search = "" if windows else os.defpath
    if windows:
        candidates = _windows_candidates(
            name, _env_value(env, "PATHEXT", True) if pathext is None else pathext, batch)
        untrusted = _untrusted_dirs(cwd)
    else:
        candidates, untrusted = [name], set()
    for entry in search.split(pathsep or (";" if windows else ":")):
        directory = entry.strip().strip('"') if windows else entry
        if not directory or not os.path.isabs(directory):
            continue
        for candidate in candidates:
            full = os.path.join(directory, candidate)
            if os.path.isfile(full) and (windows or os.access(full, os.X_OK)):
                if untrusted and _canonical(directory) in untrusted:
                    break  # the rest of this directory is no more trusted
                return full
    return None


def resolve_program(
    name: str, *, env: Mapping[str, str] | None = None,
    cwd: str | os.PathLike[str] | None = None, windows: bool | None = None,
    pathext: str | None = None, pathsep: str | None = None, batch: bool = False,
) -> str:
    """``name`` as the absolute path that will run; a path is returned as written.

    Raises :class:`ProgramNotFound` -- an ``OSError``, as a failed spawn is --
    when ``find_program`` cannot place a bare name.
    """
    windows = IS_WINDOWS if windows is None else windows
    if not _is_bare(name, windows):
        return name
    found = find_program(name, env=env, cwd=cwd, windows=windows, pathext=pathext,
                         pathsep=pathsep, batch=batch)
    if found is None:
        kind = " as a .exe or .com" if windows and not batch else ""
        raise ProgramNotFound(
            f"{name!r} was not found on PATH{kind} (the current directory and "
            "relative PATH entries are never searched)")
    return found


def resolve_argv(
    argv: Sequence[Any], *, env: Mapping[str, str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
) -> list[Any]:
    """``argv`` with a bare program name replaced by ``resolve_program``'s path."""
    argv = list(argv)
    if argv:
        program = os.fspath(argv[0]) if isinstance(argv[0], os.PathLike) else argv[0]
        if isinstance(program, str):
            argv[0] = resolve_program(program, env=env, cwd=cwd)
    return argv


def _is_bare(name: str, windows: bool) -> bool:
    if not name:
        return False
    return not any(ch in name for ch in "/\\:") if windows else "/" not in name


def _env_value(env: Mapping[str, str], key: str, windows: bool) -> str | None:
    if not windows:
        return env.get(key)
    # Windows names are case-insensitive, and a hand-built env may say "Path".
    return next((value for name, value in env.items() if name.upper() == key), None)


def _windows_candidates(name: str, pathext: str | None, batch: bool) -> list[str]:
    exts = [e.strip().lower() for e in (pathext or _DEFAULT_PATHEXT).split(";") if e.strip()]
    allowed = exts if batch else [e for e in exts if e in _EXECUTABLE_EXTS] or [".exe"]
    suffix = os.path.splitext(name)[1].lower()
    if suffix in allowed:
        return [name]
    if suffix in exts or suffix in (".bat", ".cmd"):
        return []  # named as a kind of file this lookup does not start
    return [name + ext for ext in allowed]


def _untrusted_dirs(cwd: str | os.PathLike[str] | None) -> set[str]:
    dirs: list[str | os.PathLike[str]] = [cwd] if cwd else []
    with contextlib.suppress(OSError):  # the working directory was deleted
        dirs.append(os.getcwd())
    return {_canonical(d) for d in dirs}


def _canonical(directory: str | os.PathLike[str]) -> str:
    # Real paths, so a junction, a symlink or an 8.3 short name for the
    # directory is still recognised as the directory.
    return os.path.normcase(os.path.realpath(directory))


def child_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """QuickCode's environment as a child may see it, with ``extra`` on top.

    A key the user exported in their own shell profile is still theirs: a login
    shell re-reads it. What this stops is the app handing its copy to every
    program it starts. ``extra`` is what the caller means this child to have --
    an MCP server's configured ``env``, a hook's project directory -- so it wins
    over what was inherited and is not filtered.
    """
    from quickcode.secrets import credential_env_names, is_credential_env

    known = credential_env_names()
    env = {
        name: value
        for name, value in os.environ.items()
        if not is_credential_env(name, known)
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


def _resolved(argv: Sequence[Any], kwargs: dict[str, Any]) -> list[Any]:
    return resolve_argv(argv, env=kwargs.get("env"), cwd=kwargs.get("cwd"))


def run(argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess:
    """``subprocess.run`` for a one-shot program: git, ripgrep, taskkill."""
    kwargs = _no_window(kwargs)
    return subprocess.run(_resolved(argv, kwargs), **kwargs)  # noqa: S603 - argv is caller-controlled


def spawn(
    argv: Sequence[str], *, cwd: str | None = None, env: Mapping[str, str] | None = None,
    stdin: Any = DEVNULL, stdout: Any = PIPE, stderr: Any = PIPE, **kwargs: Any,
) -> subprocess.Popen:
    """Start a child QuickCode manages: it reads the output and may kill the tree."""
    kwargs = _managed({"cwd": cwd, "env": env, "stdin": stdin, "stdout": stdout,
                       "stderr": stderr, **kwargs})
    return subprocess.Popen(_resolved(argv, kwargs), **kwargs)  # noqa: S603 - argv is caller-controlled


async def spawn_async(
    argv: Sequence[str], *, cwd: str | None = None, env: Mapping[str, str] | None = None,
    stdin: Any = DEVNULL, stdout: Any = PIPE, stderr: Any = PIPE, **kwargs: Any,
) -> asyncio.subprocess.Process:
    """``spawn`` for a caller on the event loop."""
    kwargs = _managed({"cwd": cwd, "env": env, "stdin": stdin, "stdout": stdout,
                       "stderr": stderr, **kwargs})
    return await asyncio.create_subprocess_exec(*_resolved(argv, kwargs), **kwargs)


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
