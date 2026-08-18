"""Edit tool: exact string replacement in a file.

Replaces one occurrence of ``old_string`` with ``new_string`` (or all
occurrences with ``replace_all=True``). Requires the file to have been read
this session and to be unchanged on disk since that read, so the agent is
always editing what it actually saw.
"""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from quickcode.tools.base import PermissionSpec, Tool, ToolCtx, ToolResult

MTIME_TOLERANCE = 1e-3
MAX_DIFF_LINES = 60


class EditInput(BaseModel):
    file_path: str = Field(..., description="Absolute path of the file to edit.")
    old_string: str = Field(..., description="Exact text to find and replace.")
    new_string: str = Field(..., description="Text to replace it with.")
    replace_all: bool = Field(
        False, description="Replace every occurrence instead of requiring exactly one."
    )


class EditTool(Tool[EditInput]):
    name: ClassVar[str] = "edit"
    description: ClassVar[str] = (
        "Performs an exact string replacement in a file. Use this for targeted "
        "changes instead of rewriting the whole file with Write. old_string must "
        "match exactly (including whitespace) and, unless replace_all is set, "
        "must be unique in the file — include enough surrounding context to make "
        "it so. The file must have been read with the Read tool earlier in this "
        "session and must not have changed on disk since."
    )
    is_read_only: ClassVar[bool] = False
    permission = PermissionSpec(mutates=True, target_field="file_path", path_target=True)
    Input = EditInput

    def render_call(self, input: EditInput) -> str:  # noqa: A002
        return f"⏺ Edit {input.file_path}"

    async def run(self, input: EditInput, ctx: ToolCtx) -> ToolResult:  # noqa: A002
        path = Path(input.file_path)
        if not path.is_absolute():
            path = ctx.cwd / path

        if not path.exists() or not path.is_file():
            return ToolResult(
                content=f"Error: file not found: {path}",
                is_error=True,
            )

        if not ctx.read_registry.was_read(str(path)):
            return ToolResult(
                content=(
                    f"Error: {path} has not been read in this session. "
                    "Read it first with the Read tool before editing it."
                ),
                is_error=True,
            )

        try:
            current_mtime = path.stat().st_mtime
        except OSError as exc:
            return ToolResult(content=f"Error: could not stat {path}: {exc}", is_error=True)

        recorded_mtime = ctx.read_registry.mtime_at_read(str(path))
        if recorded_mtime is not None and abs(current_mtime - recorded_mtime) > MTIME_TOLERANCE:
            return ToolResult(
                content=(
                    f"Error: {path} has changed on disk since it was read. "
                    "Read it again before editing."
                ),
                is_error=True,
            )

        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return ToolResult(content=f"Error: could not read {path}: {exc}", is_error=True)

        if input.old_string == "":
            return ToolResult(
                content="Error: old_string must not be empty.",
                is_error=True,
            )

        count = text.count(input.old_string)
        if count == 0:
            return ToolResult(
                content=(
                    "Error: old_string not found in file. Re-read the file to "
                    "confirm the exact text (whitespace matters)."
                ),
                is_error=True,
            )
        if count > 1 and not input.replace_all:
            return ToolResult(
                content=(
                    f"Error: old_string matches {count} locations in the file. "
                    "Provide more surrounding context to make it unique, or pass "
                    "replace_all=True to replace every occurrence."
                ),
                is_error=True,
            )

        if input.replace_all:
            new_text = text.replace(input.old_string, input.new_string)
        else:
            new_text = text.replace(input.old_string, input.new_string, 1)

        try:
            path.write_text(new_text, encoding="utf-8")
        except OSError as exc:
            return ToolResult(content=f"Error: could not write {path}: {exc}", is_error=True)

        try:
            new_mtime = path.stat().st_mtime
            ctx.read_registry.record(str(path), new_mtime)
        except OSError:
            pass

        diff_lines = list(
            difflib.unified_diff(
                text.splitlines(keepends=True),
                new_text.splitlines(keepends=True),
                fromfile=str(path),
                tofile=str(path),
                n=2,
            )
        )
        diff_snippet = "".join(diff_lines[:MAX_DIFF_LINES])
        if len(diff_lines) > MAX_DIFF_LINES:
            diff_snippet += f"\n<truncated shown=\"{MAX_DIFF_LINES}\" total=\"{len(diff_lines)}\"/>"

        replaced = count if input.replace_all else 1
        summary = f"Replaced {replaced} occurrence(s) in {path}"
        content = f"{summary}\n{diff_snippet}" if diff_snippet else summary
        return ToolResult(content=content, ui_meta={"diff": diff_snippet})
