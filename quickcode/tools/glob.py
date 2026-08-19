"""Glob tool: find files by pattern.

Fast file-path matching using glob syntax (supports ``**`` for recursive
matches). Use this to locate files by name or extension when you don't
already know the exact path — for open-ended content search use Grep
instead. Results are sorted newest-modified first and capped at 200 paths.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from quickcode.tools.base import PermissionSpec, Tool, ToolCtx, ToolResult, truncate

MAX_RESULTS = 200
IGNORED_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache"}


class GlobInput(BaseModel):
    pattern: str = Field(..., description='Glob pattern, e.g. "src/**/*.py" or "*.md".')
    path: str | None = Field(
        None, description="Directory to search from (default: current working directory)."
    )


class GlobTool(Tool[GlobInput]):
    name: ClassVar[str] = "glob"
    description: ClassVar[str] = (
        "Finds files matching a glob pattern (supports ** for recursive "
        "matches). Use this to locate files by name or extension when you know "
        "roughly what you're looking for but not the exact path; for searching "
        "file contents use Grep instead. Skips .git, node_modules, "
        "__pycache__, and .venv. Returns up to 200 matches, newest first, one "
        "path per line, after a marker declaring how many came back."
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
            matches = list(root.glob(input.pattern))
        except (ValueError, NotImplementedError) as exc:
            return ToolResult(content=f"Error: invalid pattern {input.pattern!r}: {exc}", is_error=True)

        results = []
        for p in matches:
            if not p.is_file():
                continue
            if any(part in IGNORED_DIRS for part in p.parts):
                continue
            try:
                mtime = p.stat().st_mtime
            except OSError:
                mtime = 0.0
            results.append((mtime, p))

        results.sort(key=lambda t: t[0], reverse=True)

        if not results:
            return ToolResult(content="No files matched.")

        # Paths, one per line, with a count in front. A TOON table was tried
        # here and reverted: a one-column table costs 10% more tokens (measured
        # with o200k_base over 60 paths) and a path list had no ambiguity for
        # the quoting to fix. What the table was actually worth is the count --
        # without it a listing cut at MAX_RESULTS looks exactly like a listing
        # that found that many -- and a marker carries that for eight tokens.
        truncated = len(results) > MAX_RESULTS
        shown = results[:MAX_RESULTS]
        body = "\n".join([
            f'<files count="{len(shown)}"/>',
            *(_norm(p) for _, p in shown),
        ])
        if truncated:
            body += f'\n<truncated shown="{MAX_RESULTS}" total="{len(results)}" hint="narrow the pattern"/>'
        body = truncate(body, 40_000, hint="narrow the pattern")
        return ToolResult(content=body)


def _norm(p: Path) -> str:
    return str(p).replace("\\", "/")
