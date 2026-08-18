"""Write tool: create or overwrite a file.

Writes full file content to disk. To prevent clobbering unseen changes, an
existing file must have been read (via the Read tool) earlier in this
session before Write will overwrite it.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from quickcode.tools.base import PermissionSpec, Tool, ToolCtx, ToolResult


class WriteInput(BaseModel):
    file_path: str = Field(..., description="Absolute path of the file to write.")
    content: str = Field(..., description="Full text content to write to the file.")


class WriteTool(Tool[WriteInput]):
    name: ClassVar[str] = "write"
    description: ClassVar[str] = (
        "Writes content to a file, creating it (and any parent directories) if "
        "it doesn't exist, or overwriting it if it does. Use this for new files "
        "or full-file rewrites; for small changes to an existing file prefer "
        "Edit. file_path must be absolute. If the file already exists, it must "
        "have been read with the Read tool earlier in this session, or the call "
        "is rejected."
    )
    is_read_only: ClassVar[bool] = False
    permission = PermissionSpec(mutates=True, target_field="file_path", path_target=True)
    Input = WriteInput

    def render_call(self, input: WriteInput) -> str:  # noqa: A002
        return f"⏺ Write {input.file_path}"

    async def run(self, input: WriteInput, ctx: ToolCtx) -> ToolResult:  # noqa: A002
        path = Path(input.file_path)
        if not path.is_absolute():
            path = ctx.cwd / path

        if path.exists():
            if path.is_dir():
                return ToolResult(
                    content=f"Error: {path} is a directory, not a file.",
                    is_error=True,
                )
            if not ctx.read_registry.was_read(str(path)):
                return ToolResult(
                    content=(
                        f"Error: {path} exists and has not been read in this "
                        "session. Read it first with the Read tool before "
                        "overwriting it."
                    ),
                    is_error=True,
                )

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(input.content, encoding="utf-8")
        except OSError as exc:
            return ToolResult(
                content=f"Error: could not write {path}: {exc}",
                is_error=True,
            )

        try:
            mtime = path.stat().st_mtime
            ctx.read_registry.record(str(path), mtime)
        except OSError:
            pass

        n_lines = input.content.count("\n") + (1 if input.content else 0)
        return ToolResult(
            content=f"Wrote {n_lines} lines to {path}",
            ui_meta={"diff": input.content},
        )
