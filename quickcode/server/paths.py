"""Read-only path completion for the composer's ``@`` token.

The project picker's ``/api/dir`` is deliberately directory-only and unscoped —
it browses the whole machine so a project can be opened — and git status only
knows about files that changed. Neither answers "which files in *this* project
match what I just typed", which is the only question the composer asks.

So: one route, clamped to a single project root by the same containment check
the git routes use, returning relative paths and nothing else. It never reads a
file's contents, it never leaves the root, and four names are invisible to it
whatever the query says — ``.git``, ``.quickcode``, ``.ssh`` and ``.env*`` are
where a repository keeps the things that are nobody's autocomplete.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from pathlib import Path

from fastapi import FastAPI

from quickcode.server.gitinfo import _safe_rel
from quickcode.server.manager import ConversationManager
from quickcode.tools.glob import IGNORED_DIRS, MAX_RESULTS

# The walk is bounded twice: by how many results it returns, and by how many
# entries it is willing to look at to find them. A monorepo with a hundred
# thousand files still has to answer a keystroke promptly, and a partial answer
# that admits it is partial beats a complete one that arrives too late.
SCAN_BUDGET = 20_000
MAX_DEPTH = 12

# Never listed, never descended into, whatever the query asks for.
BLOCKED_NAMES = {".git", ".quickcode", ".ssh"}


def _blocked(name: str) -> bool:
    return name in BLOCKED_NAMES or name.startswith(".env")


def _hidden(name: str, typed: str) -> bool:
    """Dotfiles stay out of the way until the query reaches for one."""
    return name.startswith(".") and not typed.startswith(".")


def _split(query: str) -> tuple[str, str]:
    """Split ``dir/partial-name`` into the directory half and the typed name."""
    q = (query or "").strip().replace("\\", "/")
    head, sep, tail = q.rpartition("/")
    return (head if sep else ""), tail


def _base_dir(root: Path, head: str) -> tuple[Path, str]:
    """Resolve the directory half of the query. Raises 400 on an escape."""
    if not head:
        return root, ""
    rel = _safe_rel(root, head)
    if rel == ".":
        return root, ""
    return root / rel, rel


def _empty(root: Path, query: str) -> dict:
    return {"root": str(root), "query": query, "paths": [], "truncated": False}


def _search(root: Path, query: str, limit: int) -> dict:
    head, typed = _split(query)
    base, rel_head = _base_dir(root, head)
    # A query aimed into a blocked directory gets the same empty answer a
    # non-existent one gets; refusing loudly would confirm what is there.
    if any(_blocked(part) for part in rel_head.split("/") if part):
        return _empty(root, query)
    if not base.is_dir():
        return _empty(root, query)

    needle = typed.casefold()
    hits: list[tuple[int, int, str, str, bool]] = []
    scanned = 0
    truncated = False

    for dirpath, dirnames, filenames in os.walk(base):
        rel_dir = os.path.relpath(dirpath, base).replace("\\", "/")
        depth = 0 if rel_dir == "." else rel_dir.count("/") + 1
        kept = sorted(
            d for d in dirnames
            if d not in IGNORED_DIRS and not _blocked(d) and not _hidden(d, typed)
        )
        # A directory is still offered as a completion when it is not descended
        # into: an empty query is a listing, not a search, and a listing that
        # dropped its subdirectories could never be walked down.
        entries = [(d, True) for d in kept] + [(f, False) for f in sorted(filenames)]
        dirnames[:] = [] if (not needle or depth >= MAX_DEPTH) else kept
        for name, is_dir in entries:
            scanned += 1
            if scanned > SCAN_BUDGET:
                truncated = True
                break
            if not is_dir and (_blocked(name) or _hidden(name, typed)):
                continue
            sub = name if rel_dir == "." else f"{rel_dir}/{name}"
            low = sub.casefold()
            if needle and not low.startswith(needle):
                # A basename match still counts — "compo" ought to find
                # frontend/js/composer.js — it just ranks below a prefix.
                if needle not in name.casefold():
                    continue
                score = 1
            else:
                score = 0
            full = f"{rel_head}/{sub}" if rel_head else sub
            hits.append((score, sub.count("/"), low, full, is_dir))
        if truncated or len(hits) > MAX_RESULTS * 4:
            truncated = True
            break

    hits.sort(key=lambda h: (h[0], h[1], h[2]))
    shown = hits[:limit]
    return {
        "root": str(root),
        "query": query,
        "paths": [{"path": full, "is_dir": is_dir} for _, _, _, full, is_dir in shown],
        "truncated": truncated or len(hits) > limit,
    }


async def _paths_payload(root: Path, query: str, limit: int) -> dict:
    capped = max(1, min(int(limit or MAX_RESULTS), MAX_RESULTS))
    return await asyncio.to_thread(_search, root, query or "", capped)


def register_path_routes(
    app: FastAPI,
    get_manager: Callable[[], ConversationManager],
    get_project: Callable[[str], ConversationManager] | None = None,
) -> None:
    """Mount ``GET /api/paths`` and its project-scoped twin.

    Same two shapes over one helper as the git routes: the unscoped path reads
    the hub's default project, and the ``/api/projects/{pid}/paths`` alias
    reads whichever project the UI is currently showing.
    """

    @app.get("/api/paths")
    async def complete_paths(q: str = "", limit: int = MAX_RESULTS) -> dict:
        return await _paths_payload(Path(get_manager().cwd), q, limit)

    if get_project is None:
        return

    @app.get("/api/projects/{pid}/paths")
    async def project_complete_paths(pid: str, q: str = "", limit: int = MAX_RESULTS) -> dict:
        return await _paths_payload(Path(get_project(pid).cwd), q, limit)
