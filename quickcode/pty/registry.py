"""Every live interactive terminal, indexed by the project it runs in.

A terminal outlives the request that created it, and **two** unrelated things
can legitimately end one: the WebSocket going away (``server/terminal.py``) and
the project being closed or forgotten (``server/projects.py``). Neither holds a
reference to the other, and an orphaned login shell is not a leak you notice —
it is a `bash.exe` sitting in the task list after the window is gone.

So the two meet here instead. This module deliberately imports nothing from
``quickcode.server``: the dependency runs one way only (server -> registry), or
``projects.py`` importing it would close a cycle.

Keyed by the resolved project directory, because that is what both callers
have and what ``project_id`` is derived from — two managers cannot share a cwd,
so the key is as unique as the project is.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Protocol


class Closable(Protocol):
    def close(self) -> None: ...


_lock = threading.Lock()
_live: dict[str, set[Closable]] = {}


def _key(path: str | os.PathLike[str]) -> str:
    return str(Path(path).resolve()).casefold()


def add(project_path: str | os.PathLike[str], session: Closable) -> None:
    with _lock:
        _live.setdefault(_key(project_path), set()).add(session)


def discard(project_path: str | os.PathLike[str], session: Closable) -> None:
    with _lock:
        bucket = _live.get(_key(project_path))
        if bucket is None:
            return
        bucket.discard(session)
        if not bucket:
            _live.pop(_key(project_path), None)


def count(project_path: str | os.PathLike[str] | None = None) -> int:
    with _lock:
        if project_path is None:
            return sum(len(b) for b in _live.values())
        return len(_live.get(_key(project_path), ()))


def close_for(project_path: str | os.PathLike[str]) -> int:
    """End every terminal open on one project. Returns how many were closed."""
    with _lock:
        bucket = _live.pop(_key(project_path), set())
    for session in bucket:
        session.close()
    return len(bucket)


def close_all() -> int:
    """End every terminal, everywhere. The shutdown path."""
    with _lock:
        buckets = list(_live.values())
        _live.clear()
    closed = 0
    for bucket in buckets:
        for session in bucket:
            session.close()
            closed += 1
    return closed
