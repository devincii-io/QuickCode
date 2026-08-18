"""Grep tool: search file contents with a regex.

Searches file contents for a regular expression. Use this instead of a bash
``grep``/``rg`` invocation — it prefers ripgrep when available on PATH and
transparently falls back to a pure-Python walk otherwise, so behavior is
consistent either way. Supports three output modes (files_with_matches,
content, count), an optional glob filter, and case-insensitive matching.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, Field

from quickcode.tools.base import PermissionSpec, Tool, ToolCtx, ToolResult

IGNORED_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache"}
BINARY_SNIFF_BYTES = 8192
MAX_FILE_BYTES = 5_000_000
MAX_OUTPUT_CHARS = 40_000


class GrepInput(BaseModel):
    pattern: str = Field(..., description="Regular expression to search for.")
    path: str | None = Field(None, description="File or directory to search in (default: cwd).")
    glob: str | None = Field(None, description='Only search files matching this glob, e.g. "*.py".')
    output_mode: Literal["content", "files_with_matches", "count"] = Field(
        "files_with_matches", description="What to return: matching lines, file paths, or counts."
    )
    context: int | None = Field(
        None, description="Lines of context to show before/after each match (content mode only)."
    )
    ignore_case: bool = Field(False, description="Case-insensitive match.")
    head_limit: int = Field(100, description="Cap the number of result lines/entries returned.")


class GrepTool(Tool[GrepInput]):
    name: ClassVar[str] = "grep"
    description: ClassVar[str] = (
        "Searches file contents for a regular expression pattern. Use this "
        "instead of running grep/rg via Bash. Prefers ripgrep when installed, "
        "falling back to an equivalent pure-Python search otherwise. "
        "output_mode='files_with_matches' (default) lists matching file paths; "
        "'content' prints path:line:text; 'count' prints path:count. Filter "
        "with glob (e.g. '*.py'), narrow with path, and cap results with "
        "head_limit (default 100)."
    )
    is_read_only: ClassVar[bool] = True
    permission = PermissionSpec(mutates=False, target_field="path")
    Input = GrepInput

    def render_call(self, input: GrepInput) -> str:  # noqa: A002
        where = f" in {input.path}" if input.path else ""
        return f'⏺ Grep "{input.pattern}"{where}'

    async def run(self, input: GrepInput, ctx: ToolCtx) -> ToolResult:  # noqa: A002
        root = Path(input.path) if input.path else ctx.cwd
        if not root.is_absolute():
            root = ctx.cwd / root

        if not root.exists():
            return ToolResult(content=f"Error: path not found: {root}", is_error=True)

        try:
            re.compile(input.pattern, re.IGNORECASE if input.ignore_case else 0)
        except re.error as exc:
            return ToolResult(content=f"Error: invalid regex {input.pattern!r}: {exc}", is_error=True)

        rg = shutil.which("rg")
        if rg:
            try:
                body = _run_ripgrep(rg, input, root)
                return ToolResult(content=body)
            except Exception:
                pass  # fall through to pure-python fallback

        body = _run_fallback(input, root)
        return ToolResult(content=body)


def _run_ripgrep(rg: str, input: GrepInput, root: Path) -> str:
    args = [rg, "--no-heading", "--line-number", "--color=never"]
    if input.ignore_case:
        args.append("-i")
    if input.glob:
        args += ["--glob", input.glob]
    if input.output_mode == "files_with_matches":
        args.append("-l")
    elif input.output_mode == "count":
        args.append("-c")
    elif input.context:
        args += ["-C", str(input.context)]
    args += ["--", input.pattern, str(root)]

    proc = subprocess.run(
        args,
        capture_output=True,
        timeout=30,
        text=False,
    )
    if proc.returncode not in (0, 1):
        raise RuntimeError(proc.stderr.decode("utf-8", errors="replace"))

    out = proc.stdout.decode("utf-8", errors="replace")
    lines = out.splitlines()
    if not lines:
        return "No matches found."

    truncated = len(lines) > input.head_limit
    shown = lines[: input.head_limit]
    body = "\n".join(_norm_line(root, line) for line in shown)
    if truncated:
        body += f'\n<truncated shown="{input.head_limit}" total="{len(lines)}" hint="narrow the pattern or path"/>'
    return _cap(body)


def _norm_line(root: Path, line: str) -> str:
    return line.replace("\\", "/")


def _run_fallback(input: GrepInput, root: Path) -> str:
    flags = re.IGNORECASE if input.ignore_case else 0
    try:
        rx = re.compile(input.pattern, flags)
    except re.error as exc:
        return f"Error: invalid regex: {exc}"

    files = _iter_files(root, input.glob)

    if input.output_mode == "files_with_matches":
        return _collect_files_with_matches(files, rx, input.head_limit)
    if input.output_mode == "count":
        return _collect_counts(files, rx, input.head_limit)
    return _collect_content(files, rx, input.head_limit, input.context or 0)


def _iter_files(root: Path, glob_pat: str | None):
    if root.is_file():
        if glob_pat is None or root.match(glob_pat):
            yield root
        return
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in IGNORED_DIRS for part in p.parts):
            continue
        if glob_pat and not p.match(glob_pat):
            continue
        yield p


def _is_binary(data: bytes) -> bool:
    return b"\x00" in data


def _read_text_lines(path: Path) -> list[str] | None:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if _is_binary(data[:BINARY_SNIFF_BYTES]):
        return None
    return data.decode("utf-8", errors="replace").splitlines()


def _collect_files_with_matches(files, rx: re.Pattern, head_limit: int) -> str:
    results = []
    for p in files:
        lines = _read_text_lines(p)
        if lines is None:
            continue
        if any(rx.search(line) for line in lines):
            results.append(_norm(p))
        if len(results) > head_limit:
            break
    if not results:
        return "No matches found."
    truncated = len(results) > head_limit
    shown = results[:head_limit]
    body = "\n".join(shown)
    if truncated:
        body += f'\n<truncated shown="{head_limit}" total="{len(results)}+" hint="narrow the pattern or path"/>'
    return _cap(body)


def _collect_counts(files, rx: re.Pattern, head_limit: int) -> str:
    results = []
    for p in files:
        lines = _read_text_lines(p)
        if lines is None:
            continue
        n = sum(1 for line in lines if rx.search(line))
        if n:
            results.append((p, n))
    if not results:
        return "No matches found."
    truncated = len(results) > head_limit
    shown = results[:head_limit]
    body = "\n".join(f"{_norm(p)}:{n}" for p, n in shown)
    if truncated:
        body += f'\n<truncated shown="{head_limit}" total="{len(results)}" hint="narrow the pattern or path"/>'
    return _cap(body)


def _collect_content(files, rx: re.Pattern, head_limit: int, context: int) -> str:
    out_lines: list[str] = []
    total_matches = 0
    for p in files:
        lines = _read_text_lines(p)
        if lines is None:
            continue
        for i, line in enumerate(lines):
            if rx.search(line):
                total_matches += 1
                if len(out_lines) < head_limit:
                    if context:
                        lo = max(0, i - context)
                        hi = min(len(lines), i + context + 1)
                        for j in range(lo, hi):
                            out_lines.append(f"{_norm(p)}:{j + 1}:{lines[j]}")
                    else:
                        out_lines.append(f"{_norm(p)}:{i + 1}:{line}")
        if total_matches > head_limit and len(out_lines) >= head_limit:
            break
    if not out_lines:
        return "No matches found."
    truncated = total_matches > head_limit
    shown = out_lines[:head_limit]
    body = "\n".join(shown)
    if truncated:
        body += f'\n<truncated shown="{len(shown)}" total="{total_matches}+" hint="narrow the pattern or path"/>'
    return _cap(body)


def _norm(p: Path) -> str:
    return str(p).replace("\\", "/")


def _cap(body: str) -> str:
    if len(body) <= MAX_OUTPUT_CHARS:
        return body
    shown = body[:MAX_OUTPUT_CHARS]
    return f'{shown}\n<truncated shown="{MAX_OUTPUT_CHARS}" total="{len(body)}" hint="narrow the pattern or path"/>'
