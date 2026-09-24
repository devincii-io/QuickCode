"""Where a checkpointed file lives: one spelling, inside the project, no links.

A checkpoint names a file by its path relative to the project root's *real*
location, with forward slashes -- the same file however the tool call spelled
it (``./src/a.py``, ``C:\\Proj\\SRC\\a.py``, a symlink inside the project that
points at it). Recording the resolved path is what lets a rewind refuse to
follow a link: the path it writes to must still resolve to exactly itself.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

# Not the project's files. The store's own directory: a tool writing in here
# would be checkpointing the checkpoints, and a rewind would be restoring blobs
# into the store it reads. And the scratch checkouts isolated subagents edit
# (subagents/worktree.py): each is removed when its run ends and its work comes
# back as a branch, so a rewind would recreate files in a checkout that is gone.
_NOT_PROJECT = ((".quickcode", "checkpoints"), (".quickcode", "worktrees"))


def real_root(root: Path | str) -> Path:
    return Path(os.path.realpath(root))


def relative(root: Path, path: Path | str) -> str | None:
    """``path``'s real location relative to ``root`` (already real), or None.

    None for anything that resolves outside the project, onto the root itself,
    or into the checkpoint store or a subagent's worktree.
    """
    try:
        rel = os.path.relpath(os.path.realpath(path), root)
    except ValueError:  # another drive
        return None
    parts = Path(rel).parts
    if not parts or rel == os.curdir or parts[0] == os.pardir or os.path.isabs(rel):
        return None
    if tuple(p.lower() for p in parts[:2]) in _NOT_PROJECT:
        return None
    return PurePosixPath(*parts).as_posix()


def target(root: Path, rel: str) -> Path | None:
    """The file ``rel`` names under ``root``, or None if it may not be written.

    Refused: anything that is not a plain relative path (absolute, a drive,
    ``..``, empty segments), and any path that no longer resolves to itself --
    a directory along it, or the file, has become a symlink or a junction since
    the checkpoint was taken. A rewind writes where it recorded, or nowhere.
    """
    if not isinstance(rel, str) or not rel or "\\" in rel or "\x00" in rel:
        return None
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return None
    if os.name == "nt" and any(":" in p for p in parts):
        return None
    candidate = root.joinpath(*parts)
    if relative(root, candidate) is None:
        return None
    if os.path.normcase(os.path.realpath(candidate)) != os.path.normcase(str(candidate)):
        return None
    return candidate


def fold(rel: str) -> str:
    """The key two spellings of one file share, on this platform."""
    return os.path.normcase(rel)
