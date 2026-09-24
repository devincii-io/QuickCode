"""Whether a recursive read would reach a protected file.

``grep -r KEY .`` names ``.``, which is not protected, and prints the contents
of ``.env`` and ``.git/config`` on the way through -- the same sweep the
``grep`` tool was fixed to skip. The name on the command line is not the
question; what the walk reaches is. So this looks at the disk, the way the
command is about to.

The walk is bounded. Past ``LIMIT`` entries it stops and answers yes: an
unknown answer is not a safe one, and a tree that large is better searched with
a narrower root or the ``grep`` tool, which skips secrets by itself.
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path

from quickcode.security.protected import is_protected, is_protected_name

LIMIT = 20_000


def _glob_hit(rel: str, name: str, globs: tuple[str, ...]) -> bool:
    """Whether a ripgrep whitelist glob could select this entry or anything
    under it. Case is folded: ``--iglob`` exists, and Windows folds anyway."""
    rel, name = rel.lower(), name.lower()
    for glob in globs:
        g = glob.lower().lstrip("/")
        if any(fnmatch.fnmatchcase(v, g) for v in (name, rel, f"{rel}/x")):
            return True
    return False


def reaches_protected(
    roots: list[Path], project: Path, *, hidden: bool, globs: tuple[str, ...] = (),
    follow: bool = False, limit: int | None = None,
) -> bool:
    """True when walking ``roots`` would read something protected.

    ``hidden`` says whether dot-entries are read at all (``grep -r``: yes;
    ``rg``: only with ``--hidden``); ``globs`` are ripgrep whitelist globs,
    which select hidden entries even without it. A symlink counts by its
    target when the command follows links (``grep -R``, ``rg -L``, ``diff``).
    """
    budget = LIMIT if limit is None else limit
    stack = [(root, "") for root in roots]
    while stack:
        directory, prefix = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            budget -= 1
            if budget < 0:
                return True
            rel = f"{prefix}{entry.name}"
            selected = hidden or not entry.name.startswith(".") or _glob_hit(rel, entry.name, globs)
            if not selected:
                continue
            if is_protected_name(entry.name):
                return True
            try:
                if entry.is_symlink():
                    if follow and is_protected(entry.path, project):
                        return True
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append((entry.path, f"{rel}/"))
            except OSError:
                return True
    return False
