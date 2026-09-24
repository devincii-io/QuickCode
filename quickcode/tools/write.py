"""Write tool: create or overwrite a file.

Writes full file content to disk. To prevent clobbering unseen changes, an
existing file must have been read (via the Read tool) earlier in this session
and must not have changed on disk since -- the same check edit makes. Checking
only that it had been read at *some* point let a user's edit, made after that
read, be overwritten without a word.

An overwritten file keeps its encoding, BOM and line endings; a new one is
written exactly as given, UTF-8 with no newline translation, on every platform.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from quickcode.tools.base import PermissionSpec, Tool, ToolCtx, ToolResult
from quickcode.tools.fs import diffpreview, textfile


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

    def render_diff(self, input: WriteInput, ctx: ToolCtx) -> str:  # noqa: A002
        path = diffpreview.target(input.file_path, ctx)
        text = diffpreview.seen_text(path, ctx)
        if text is not None:
            return diffpreview.unified(text, input.content, str(path), str(path))
        # A new file's head -- or, for a file the session never read (which the
        # write refuses), the content alone rather than a file nobody saw.
        exists = diffpreview.exists(path)
        before = f"{path} (not read in this session)" if exists else "/dev/null"
        return diffpreview.unified("", input.content, before, str(path))

    async def run(self, input: WriteInput, ctx: ToolCtx) -> ToolResult:  # noqa: A002
        path = Path(input.file_path)
        if not path.is_absolute():
            path = ctx.cwd / path

        content = input.content
        encoding = textfile.UTF8
        if path.exists():
            if path.is_dir():
                return _error(f"{path} is a directory, not a file.")
            if not ctx.read_registry.was_read(str(path)):
                return _error(
                    f"{path} exists and has not been read in this session. Read it "
                    "first with the Read tool before overwriting it."
                )
            try:
                raw = path.read_bytes()
                mtime = path.stat().st_mtime
            except OSError as exc:
                return _error(f"could not read {path}: {exc}")
            if ctx.read_registry.changed_since_read(str(path), mtime, textfile.digest(raw)):
                return _error(
                    f"{path} has changed on disk since it was read. Read it again "
                    "before overwriting it."
                )
            try:
                encoding = textfile.detect(raw)
                old_text = textfile.decode(raw, encoding)
                content = textfile.with_newlines(content, textfile.newline_of(old_text))
            except textfile.NotText:
                encoding = textfile.UTF8

        try:
            data = textfile.encode(content, encoding)
        except textfile.Unencodable as exc:
            return _error(f"{path}: {exc}")

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            ctx.read_registry.record(str(path), path.stat().st_mtime, textfile.digest(data))
        except OSError as exc:
            return _error(f"could not write {path}: {exc}")

        n_lines = len(textfile.split_lines(input.content))
        return ToolResult(
            content=f"Wrote {n_lines} lines to {path}",
            ui_meta={"diff": input.content},
        )


def _error(message: str) -> ToolResult:
    return ToolResult(content=f"Error: {message}", is_error=True)
