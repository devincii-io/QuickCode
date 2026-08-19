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
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

IS_WINDOWS = sys.platform == "win32"

# Zero everywhere else: POSIX rejects any *non-zero* creationflags, so passing
# this unconditionally keeps the call sites free of platform branches.
NO_WINDOW: int = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0


def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
    """``subprocess.run`` that does not put a console on the user's screen."""
    kwargs["creationflags"] = kwargs.pop("creationflags", 0) | NO_WINDOW
    return subprocess.run(argv, **kwargs)  # noqa: S603 - argv is caller-controlled


def popen(argv: list[str], **kwargs: Any) -> subprocess.Popen:
    """``subprocess.Popen`` that does not put a console on the user's screen."""
    kwargs["creationflags"] = kwargs.pop("creationflags", 0) | NO_WINDOW
    return subprocess.Popen(argv, **kwargs)  # noqa: S603 - argv is caller-controlled
