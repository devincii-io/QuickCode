"""Read tool: view a text file with numbered lines.

Reads a file from disk and returns its content as 1-indexed, arrow-prefixed
lines (``   123→text``), the same shape a human sees in an editor gutter.
Records the file's mtime in ``ctx.read_registry`` so Edit/Write can later
verify the file hasn't changed underneath the agent.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from quickcode.tools.base import Tool, ToolCtx, ToolResult, truncate

DEFAULT_LIMIT = 2000
MAX_LINE_CHARS = 2000
MAX_OUTPUT_CHARS = 40_000


class ReadInput(BaseModel):
    file_path: str = Field(..., description="Absolute path to the file to read.")
    offset: int | None = Field(
        None, description="1-indexed line number to start reading from (default: 1)."
    )
    limit: int | None = Field(
        None, description=f"Maximum number of lines to read (default: {DEFAULT_LIMIT})."
    )


class ReadTool(Tool[ReadInput]):
    name: ClassVar[str] = "read"
    description: ClassVar[str] = (
        "Reads a file from the local filesystem and returns its contents as "
        "numbered lines. Use this to view source files, configs, or logs before "
        "editing them — Edit and Write both require the file to have been read "
        "first in this session. file_path must be absolute. Defaults to the "
        f"first {DEFAULT_LIMIT} lines; pass offset/limit to page through larger "
        f"files. Lines longer than {MAX_LINE_CHARS} characters are cut with a "
        "marker, and total output is capped (use offset to read further)."
    )
    is_read_only: ClassVar[bool] = True
    Input = ReadInput

    def render_call(self, input: ReadInput) -> str:  # noqa: A002
        return f"⏺ Read {input.file_path}"

    async def run(self, input: ReadInput, ctx: ToolCtx) -> ToolResult:  # noqa: A002
        path = Path(input.file_path)
        if not path.is_absolute():
            path = ctx.cwd / path

        if not path.exists():
            return ToolResult(
                content=(
                    f"Error: file not found: {path}\n"
                    "Check the path (must be absolute) and that the file exists."
                ),
                is_error=True,
            )
        if not path.is_file():
            return ToolResult(
                content=f"Error: not a file: {path} (is it a directory?)",
                is_error=True,
            )

        try:
            raw = path.read_bytes()
        except OSError as exc:
            return ToolResult(
                content=f"Error: could not read {path}: {exc}",
                is_error=True,
            )

        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception as exc:  # pragma: no cover - decode with errors="replace" never raises
            return ToolResult(
                content=f"Error: could not decode {path} as text: {exc}",
                is_error=True,
            )

        lines = text.splitlines()
        offset = input.offset if input.offset and input.offset > 0 else 1
        limit = input.limit if input.limit and input.limit > 0 else DEFAULT_LIMIT

        start = offset - 1
        end = start + limit
        window = lines[start:end]

        if start >= len(lines) and len(lines) > 0:
            return ToolResult(
                content=(
                    f"Error: offset {offset} is past the end of the file "
                    f"({len(lines)} lines total)."
                ),
                is_error=True,
            )

        out_lines = []
        for i, line in enumerate(window, start=offset):
            if len(line) > MAX_LINE_CHARS:
                line = line[:MAX_LINE_CHARS] + "…[cut]"
            out_lines.append(f"{i:6d}→{line}")

        body = "\n".join(out_lines)
        remaining = len(lines) - (start + len(window))
        hint = ""
        if remaining > 0:
            hint = f"more lines available; re-read with offset={start + len(window) + 1}"
        body = truncate(body, MAX_OUTPUT_CHARS, hint=hint or "use offset to page further")

        try:
            mtime = path.stat().st_mtime
            ctx.read_registry.record(str(path), mtime)
        except OSError:
            pass

        return ToolResult(content=body)
