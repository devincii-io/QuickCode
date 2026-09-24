"""Turning a program *name* into something safe to hand ``CreateProcess``.

Used wherever QuickCode starts a program named in a file rather than by its own
code: authored command tools and MCP servers.

**Resolution.** ``CreateProcess`` appends only ``.exe``, so on Windows the
``.cmd`` shims that ``npm``, ``npx``, ``yarn`` and ``pnpm`` install cannot be
started by name. ``shutil.which`` knows ``PATHEXT`` but searches the current
directory first -- the directory a cloned repository is opened from -- so it
would trade "does not start" for "starts the repository's ``npx.cmd``". This
is ``subproc.find_program``, the lookup every spawn uses, with the ``PATHEXT``
batch extensions allowed.

**Batch targets.** A ``.cmd``/``.bat`` file runs under ``cmd.exe``, which
re-parses the command line ``subprocess`` built for the C runtime's rules. An
argument holding ``"`` flips cmd's quoting state and the ``&`` after it starts
a second command; ``%VAR%`` expands even inside quotes. Quoting for cmd
correctly is a known-unsolved problem, so a value carrying one of those
characters is refused rather than escaped.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from quickcode import subproc

_BATCH_SUFFIXES = (".bat", ".cmd")

# What cmd.exe still interprets after subprocess.list2cmdline has quoted an
# argument: quote-state, expansion, escape, command separators, redirection,
# and the line ends that terminate the command outright. Parentheses are left
# out: they only matter inside a block, and "file (1).txt" is common.
_CMD_SPECIAL = frozenset('"%!^&|<>\r\n\x00')


def resolve_program(
    name: str,
    env: Mapping[str, str],
    *,
    cwd: str | os.PathLike[str] | None = None,
    windows: bool | None = None,
    pathext: str | None = None,
    pathsep: str | None = None,
) -> str:
    """The file ``name`` means, on Windows; ``name`` unchanged otherwise.

    Only bare names are resolved -- a path is already what its author meant --
    and a name that resolves to nothing is returned as written, so the spawn
    fails with ``subproc``'s own "not found".
    """
    windows = subproc.IS_WINDOWS if windows is None else windows
    if not windows:
        return name
    found = subproc.find_program(name, env=env, cwd=cwd, windows=True, pathext=pathext,
                                 pathsep=pathsep, batch=True)
    return found or name


def is_batch(program: str) -> bool:
    """Whether Windows would run ``program`` through ``cmd.exe``."""
    return program.lower().endswith(_BATCH_SUFFIXES)


def batch_unsafe(value: str) -> bool:
    """Whether ``cmd.exe`` would read something other than data in ``value``."""
    return any(ch in _CMD_SPECIAL for ch in value)
