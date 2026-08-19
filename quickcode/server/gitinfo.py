"""Read-only git introspection: working-tree status and per-file diffs.

The panel that consumes this is a viewer, never a driver: no command here
mutates the repository. Paths always travel after ``--`` and never through a
shell, and every request is clamped to the project root so a crafted ``path``
cannot read outside it. Git failures degrade to an empty answer rather than a
500 — a missing git binary or a non-repo directory is a normal state for the
UI, not an error.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException

from quickcode import subproc
from quickcode.server.manager import ConversationManager

log = logging.getLogger("quickcode.server.gitinfo")

GIT_TIMEOUT = 5.0
DIFF_CAP = 200_000


def _run(cwd: Path, *args: str) -> tuple[bool, str]:
    """Run one git command; return (ok, stdout). Never raises."""
    try:
        proc = subproc.run(
            ["git", "-C", str(cwd), "-c", "core.quotepath=off", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("git %s failed: %s", args[:1], exc)
        return False, ""
    return proc.returncode == 0, proc.stdout or ""


def _unquote(path: str) -> str:
    return path[1:-1] if len(path) >= 2 and path[0] == '"' and path[-1] == '"' else path


def _parse_status(out: str) -> list[dict[str, str]]:
    files: list[dict[str, str]] = []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        code, rest = line[:2].strip(), line[3:]
        # Renames/copies read "old -> new"; the new name is what the user sees.
        if " -> " in rest:
            rest = rest.split(" -> ", 1)[1]
        files.append({"path": _unquote(rest), "status": code or "?"})
    return files


def _status(cwd: Path) -> dict[str, Any]:
    ok, out = _run(cwd, "status", "--porcelain")
    if not ok:
        return {"is_repo": False, "branch": "", "files": []}
    branch_ok, branch = _run(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    if not branch_ok or not branch.strip():
        # A repo with no commits yet has no resolvable HEAD.
        branch_ok, branch = _run(cwd, "symbolic-ref", "--short", "HEAD")
    return {
        "is_repo": True,
        "branch": branch.strip() if branch_ok else "",
        "files": _parse_status(out),
    }


def _diff(cwd: Path, rel: str) -> str:
    ok, out = _run(cwd, "diff", "HEAD", "--", rel)
    if ok and out.strip():
        return out
    tracked_ok, tracked = _run(cwd, "ls-files", "--", rel)
    if tracked_ok and tracked.strip():
        return ""  # tracked and unchanged — an empty diff is the honest answer
    # Untracked file: synthesize an all-added diff against the null device.
    for null in (os.devnull, "/dev/null"):
        # --no-index exits 1 when the files differ, which is the success case.
        _, out = _run(cwd, "diff", "--no-index", "--", null, rel)
        if out.strip():
            return out
    return ""


def _safe_rel(root: Path, raw: str) -> str:
    """Resolve ``raw`` against the project root, refusing anything outside."""
    candidate = (raw or "").strip().replace("\\", "/")
    if not candidate:
        raise HTTPException(400, "path is required")
    base = os.path.realpath(root)
    target = os.path.realpath(os.path.join(base, candidate))
    if target != base and not target.startswith(base + os.sep):
        raise HTTPException(400, "path escapes the project root")
    rel = os.path.relpath(target, base).replace("\\", "/")
    if rel.startswith("-"):  # never let a path be read as an option
        rel = "./" + rel
    return rel


async def _status_payload(root: Path) -> dict:
    return await asyncio.to_thread(_status, root)


async def _diff_payload(root: Path, path: str) -> dict:
    rel = _safe_rel(root, path)
    text = await asyncio.to_thread(_diff, root, rel)
    return {"path": rel, "diff": text[:DIFF_CAP], "truncated": len(text) > DIFF_CAP}


def register_git_routes(
    app: FastAPI,
    get_manager: Callable[[], ConversationManager],
    get_project: Callable[[str], ConversationManager] | None = None,
) -> None:
    """Mount the read-only git routes (token-gated by the app middleware).

    Two shapes over one pair of handlers, mirroring the rest of the API: the
    unscoped ``/api/git/…`` paths read the hub's default project, and the
    ``/api/projects/{pid}/git/…`` aliases read whichever project the UI is
    currently showing. ``get_project`` resolves a project id to its manager and
    is expected to raise for unknown ids; omit it for a single-project app.
    """

    @app.get("/api/git/status")
    async def git_status() -> dict:
        return await _status_payload(Path(get_manager().cwd))

    @app.get("/api/git/diff")
    async def git_diff(path: str) -> dict:
        return await _diff_payload(Path(get_manager().cwd), path)

    if get_project is None:
        return

    @app.get("/api/projects/{pid}/git/status")
    async def project_git_status(pid: str) -> dict:
        return await _status_payload(Path(get_project(pid).cwd))

    @app.get("/api/projects/{pid}/git/diff")
    async def project_git_diff(pid: str, path: str) -> dict:
        return await _diff_payload(Path(get_project(pid).cwd), path)
