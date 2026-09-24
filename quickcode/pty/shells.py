"""Which shell the terminal panel opens, and how it is asked to be a login shell.

On Windows that is Git Bash, found the way ``tools/bash.py`` finds it, so the
panel and the agent land in the same shell. Elsewhere it is the user's own
login shell — ``$SHELL``, then the password database — because that is where
their ``PATH``, prompt and aliases live. Hard-coding ``/bin/bash`` gave a macOS
user (zsh since 10.15) the system's bash 3.2 with none of their profile.

Deliberately not ``-c``: the agent's tool builds ``bash -lc "<command>"``
because it has one command and wants the process to exit afterwards. Here the
shell *is* the session, so it is invoked the way a terminal emulator invokes
it — login and interactive — which reads the profile and prints a prompt.
"""

from __future__ import annotations

import os
import sys

# How each shell spells "login + interactive". csh and tcsh honour ``-l`` only
# when it is the sole argument; a shell not listed gets no flags at all, since
# an unknown flag is an error and a tty on stdin already makes it interactive.
LOGIN_FLAGS: dict[str, list[str]] = {
    "bash": ["-i", "-l"],
    "zsh": ["-i", "-l"],
    "fish": ["-i", "-l"],
    "ksh": ["-i", "-l"],
    "mksh": ["-i", "-l"],
    "dash": ["-i", "-l"],
    "sh": ["-i", "-l"],
    "tcsh": ["-l"],
    "csh": ["-l"],
}

# Account shells that exist to refuse a login. A service account running the
# app should still get a usable prompt rather than "This account is currently
# not available".
_NOT_A_SHELL = {"nologin", "false", "true"}

FALLBACKS = ("/bin/bash", "/bin/sh")


def interactive_shell_argv() -> list[str]:
    """The argv for a shell a person can sit in front of."""
    if sys.platform.startswith("win"):
        # Imported here rather than at module scope: the tools package pulls in
        # pydantic and the tool registry, which a pty has no business needing.
        from quickcode.tools.bash import _find_git_bash

        found = _find_git_bash()
        if found:
            return [found, "-i", "-l"]
        # No Git Bash: PowerShell, minus the -NonInteractive the tool passes.
        return ["powershell", "-NoLogo", "-NoExit"]
    shell = login_shell()
    return [shell, *LOGIN_FLAGS.get(os.path.basename(shell), [])]


def login_shell() -> str:
    """The user's login shell on POSIX, or the first fallback that exists."""
    for candidate in (os.environ.get("SHELL"), _passwd_shell(), *FALLBACKS):
        if candidate and _usable(candidate):
            return candidate
    return FALLBACKS[-1]


def _passwd_shell() -> str | None:
    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_shell or None
    except (ImportError, KeyError, OSError):
        return None


def _usable(path: str) -> bool:
    return (
        os.path.isabs(path)
        and os.path.basename(path) not in _NOT_A_SHELL
        and os.path.isfile(path)
        and os.access(path, os.X_OK)
    )
