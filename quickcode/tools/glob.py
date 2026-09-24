"""Glob tool: find files by pattern.

Fast file-path matching using glob syntax (supports ``**`` for recursive
matches and ``{a,b}`` alternatives). Use this to locate files by name or
extension when you don't already know the exact path — for open-ended content
search use Grep instead. Results are sorted newest-modified first and capped
at 200 paths.

The matching is ``fs/patterns.py`` and the walk is ``fs/walk.py``, the same
walk grep's pure-Python search uses: excluded directories are pruned rather
than walked and filtered, ``.gitignore`` is honoured, a symlinked directory is
never entered (so a link cycle cannot loop) and a pattern without ``**`` does
not descend further than it has segments.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path, PurePath
from typing import ClassVar

from pydantic import BaseModel, Field

from quickcode.tools.base import PermissionSpec, Tool, ToolCtx, ToolResult, truncate
from quickcode.tools.fs.patterns import PatternError, compile_glob, expand_braces, has_magic
from quickcode.tools.fs.walk import IGNORED_DIRS, walk_files

__all__ = ["IGNORED_DIRS", "MAX_RESULTS", "GlobTool"]

MAX_RESULTS = 200


class GlobInput(BaseModel):
    pattern: str = Field(..., description='Glob pattern, e.g. "src/**/*.py" or "*.md".')
    path: str | None = Field(
        None, description="Directory to search from (default: current working directory)."
    )


class GlobTool(Tool[GlobInput]):
    name: ClassVar[str] = "glob"
    description: ClassVar[str] = (
        "Finds files matching a glob pattern (supports ** for recursive "
        "matches and {a,b} alternatives, e.g. \"src/**/*.{ts,tsx}\"). Use this "
        "to locate files by name or extension when you know roughly what "
        "you're looking for but not the exact path; for searching file "
        "contents use Grep instead. Skips .git, node_modules, __pycache__, "
        ".venv and whatever .gitignore excludes. Returns up to 200 matches, "
        "newest first, one path per line, after a marker declaring how many "
        "came back."
    )
    is_read_only: ClassVar[bool] = True
    # A path target, gated as ``read`` is: listing a directory is a smaller
    # disclosure than reading it, but it is the same boundary and filenames
    # alone are worth prompting for outside the project.
    permission = PermissionSpec(mutates=False, target_field="path", path_target=True)
    Input = GlobInput

    def permission_target(self, args: dict) -> str:
        """Where this call actually looks: the root joined with the pattern.

        Gating ``path`` alone left the required argument ungated, so
        ``glob(pattern="../*/*.txt")`` offered the engine an empty string,
        matched nothing, and enumerated files outside the project with no
        prompt in every mode. The pattern is part of the location, so it is
        part of what gets checked.
        """
        pattern = str(args.get("pattern") or "")
        path = str(args.get("path") or "")
        if not pattern:
            return path
        if not path:
            return pattern
        return f"{path.rstrip('/').rstrip(chr(92))}/{pattern}"

    def render_call(self, input: GlobInput) -> str:  # noqa: A002
        where = f" in {input.path}" if input.path else ""
        return f'⏺ Glob "{input.pattern}"{where}'

    async def run(self, input: GlobInput, ctx: ToolCtx) -> ToolResult:  # noqa: A002
        root = Path(input.path) if input.path else ctx.cwd
        if not root.is_absolute():
            root = ctx.cwd / root

        if not root.exists():
            return ToolResult(content=f"Error: path not found: {root}", is_error=True)
        if not root.is_dir():
            return ToolResult(content=f"Error: not a directory: {root}", is_error=True)

        try:
            start, rest = split_pattern(root, input.pattern)
            # Off the event loop: walking a large tree is seconds of blocking
            # I/O, and the loop it would block serves every session.
            matches = await asyncio.to_thread(_find, start, rest)
        except PatternError as exc:
            return ToolResult(content=f"Error: invalid pattern {input.pattern!r}: {exc}",
                              is_error=True)

        if not matches:
            return ToolResult(content="No files matched.")

        # Paths, one per line, with a count in front. A TOON table was tried
        # here and reverted: a one-column table costs 10% more tokens (measured
        # with o200k_base over 60 paths) and a path list had no ambiguity for
        # the quoting to fix. What the table was actually worth is the count --
        # without it a listing cut at MAX_RESULTS looks exactly like a listing
        # that found that many -- and a marker carries that for eight tokens.
        truncated = len(matches) > MAX_RESULTS
        shown = matches[:MAX_RESULTS]
        body = "\n".join([
            f'<files count="{len(shown)}"/>',
            *(_norm(p) for p in shown),
        ])
        if truncated:
            body += f'\n<truncated shown="{MAX_RESULTS}" total="{len(matches)}" hint="narrow the pattern"/>'
        body = truncate(body, 40_000, hint="narrow the pattern")
        return ToolResult(content=body)


def split_pattern(root: Path, pattern: str) -> tuple[Path, str]:
    """The directory to walk from, and what is left of the pattern to match.

    The leading segments with no wildcard in them are a place, not a pattern:
    ``src/app/**/*.ts`` walks ``src/app`` rather than the whole project, and an
    absolute pattern is honoured instead of refused as ``Path.glob`` did.
    """
    if os.sep == "\\":
        pattern = pattern.replace("\\", "/")
    parts = PurePath(pattern).parts
    literal: list[str] = []
    for part in parts:
        if has_magic(part):
            break
        literal.append(part)
    rest = parts[len(literal):]
    if ".." in rest:
        raise PatternError("'..' may only appear before the first wildcard")
    return root.joinpath(*literal), "/".join(rest)


def _find(start: Path, rest: str) -> list[Path]:
    """Matching files, newest first; ties in path order so the list is stable."""
    if not rest:
        return [start] if start.is_file() else []
    if not start.is_dir():
        return []
    regex = compile_glob(rest)
    expansions = expand_braces(rest)
    unbounded = any("**" in e for e in expansions)
    depth = None if unbounded else max(e.count("/") + 1 for e in expansions)
    found: list[tuple[float, str, Path]] = []
    for rel, path in walk_files(start, exclude=_pruned, follow_file_links=True, max_depth=depth):
        if not regex.match(rel):
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        found.append((mtime, rel, path))
    found.sort(key=lambda t: (-t[0], tuple(t[1].split("/"))))
    return [path for _, _, path in found]


def _pruned(name: str, is_dir: bool) -> bool:
    return is_dir and name in IGNORED_DIRS


def _norm(p: Path) -> str:
    return str(p).replace("\\", "/")
