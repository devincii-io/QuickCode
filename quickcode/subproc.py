"""Running a console program from a window that has no console.

``QuickCodeApp.exe`` is built windowed (``console=False`` in ``quickcode.spec``)
so no console flashes up behind the app. The consequence is that every console
program the app then spawns -- git, ripgrep, bash, taskkill -- has no console to
inherit, so Windows **allocates a new one**, and it appears on screen for as
long as the command runs. The git panel refreshes on a timer, so the user saw a
terminal blink at them every few seconds; switching the bash tool to plain pipes
(ConPTY hosts its own pseudoconsole and never had this problem) made it one per
command as well.

``CREATE_NO_WINDOW`` is the fix, and it has to be passed at every spawn site --
there is no process-wide setting for it. Hence this module: one import, and a
site that forgets it is visible as a bare ``subprocess.run`` in review.

Not for a program the user is meant to see. Nothing here is used to launch the
installer, which has its own window and wants one (see ``update.py``).

The same one import is where a child's environment is decided: ``child_env`` is
QuickCode's own without its API keys, and the default for every spawn here. The
model can ask for ``echo $QUICKCODE_OPENROUTER_API_KEY`` as easily as for ``ls``.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Mapping
from typing import Any

IS_WINDOWS = sys.platform == "win32"

# Zero everywhere else: POSIX rejects any *non-zero* creationflags, so passing
# this unconditionally keeps the call sites free of platform branches.
NO_WINDOW: int = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0


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


def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
    """``subprocess.run`` that does not put a console on the user's screen."""
    return subprocess.run(argv, **_no_window(kwargs))  # noqa: S603 - argv is caller-controlled


def popen(argv: list[str], **kwargs: Any) -> subprocess.Popen:
    """``subprocess.Popen`` that does not put a console on the user's screen."""
    return subprocess.Popen(argv, **_no_window(kwargs))  # noqa: S603 - argv is caller-controlled


def kill_tree(pid: int | None) -> None:
    """Kill a process and everything it started. Never raises.

    On POSIX this reaches the whole tree only when the process was started
    with ``start_new_session=True``, which makes it the leader of its own
    process group; otherwise it kills the one process. Killing just the top
    of a tree is not enough for a caller holding its pipes: a grandchild that
    inherited them keeps them open, and asyncio's ``Process.wait()`` does not
    return until every pipe has closed.
    """
    if not pid:
        return
    if IS_WINDOWS:
        try:
            run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, timeout=10)
        except Exception:  # noqa: BLE001 - already gone, or taskkill missing
            pass
        return
    import os
    import signal

    try:
        group = os.getpgid(pid)
        if group != os.getpgrp():
            os.killpg(group, signal.SIGKILL)
            return
    except OSError:
        pass
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
