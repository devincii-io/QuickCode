"""Edit tool: exact string replacement in a file.

Replaces one occurrence of ``old_string`` with ``new_string`` (or all
occurrences with ``replace_all=True``). Requires the file to have been read
this session and to be unchanged on disk since that read, so the agent is
always editing what it actually saw.

The file keeps its encoding, BOM and line endings (``fs/textfile.py``). The
model writes ``\\n`` because ``\\n`` is all read ever showed it, so in a CRLF
file both strings are matched and written with CRLF.
"""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from quickcode.tools.base import PermissionSpec, Tool, ToolCtx, ToolResult
from quickcode.tools.fs import diffpreview, textfile

MAX_DIFF_LINES = 60
# How many of an ambiguous old_string's line numbers the error lists.
MAX_LISTED_MATCHES = 10


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

    def render_diff(self, input: EditInput, ctx: ToolCtx) -> str:  # noqa: A002
        path = diffpreview.target(input.file_path, ctx)
        text = diffpreview.seen_text(path, ctx)
        if text is not None and input.old_string:
            old, new, count = _locate(text, input.old_string, input.new_string)
            if count == 1 or (count and input.replace_all):
                new_text = text.replace(old, new) if input.replace_all else text.replace(old, new, 1)
                return diffpreview.unified(text, new_text, str(path), str(path))
        # Not read yet, or old_string is not (uniquely) there: the call will be
        # refused, and its own two strings are what there is to show.
        return diffpreview.unified(input.old_string, input.new_string,
                                   f"{path} (old_string)", f"{path} (new_string)")

    async def run(self, input: EditInput, ctx: ToolCtx) -> ToolResult:  # noqa: A002
        path = Path(input.file_path)
        if not path.is_absolute():
            path = ctx.cwd / path

        if not path.exists() or not path.is_file():
            return _error(f"file not found: {path}")
        if not ctx.read_registry.was_read(str(path)):
            return _error(
                f"{path} has not been read in this session. "
                "Read it first with the Read tool before editing it."
            )
        if input.old_string == "":
            return _error("old_string must not be empty.")
        if input.old_string == input.new_string:
            return _error("old_string and new_string are identical; there is nothing to change.")

        try:
            raw = path.read_bytes()
            mtime = path.stat().st_mtime
        except OSError as exc:
            return _error(f"could not read {path}: {exc}")
        try:
            encoding = textfile.detect(raw)
        except textfile.NotText:
            return _error(f"{path} is a binary file; edit changes text files only.")
        if ctx.read_registry.changed_since_read(str(path), mtime, textfile.digest(raw)):
            return _error(
                f"{path} has changed on disk since it was read. Read it again before editing."
            )

        text = textfile.decode(raw, encoding)
        old, new, count = _locate(text, input.old_string, input.new_string)
        if count == 0:
            return _error(
                "old_string not found in file. Re-read the file to confirm the "
                "exact text (whitespace matters)."
            )
        if count > 1 and not input.replace_all:
            return _error(
                f"old_string matches {count} locations in the file (lines "
                f"{_lines_of(text, old)}). Provide more surrounding context to make "
                "it unique, or pass replace_all=True to replace every occurrence."
            )

        new_text = text.replace(old, new) if input.replace_all else text.replace(old, new, 1)
        try:
            data = textfile.encode(new_text, encoding)
        except textfile.Unencodable as exc:
            return _error(f"{path}: {exc}")

        try:
            path.write_bytes(data)
            ctx.read_registry.record(str(path), path.stat().st_mtime, textfile.digest(data))
        except OSError as exc:
            return _error(f"could not write {path}: {exc}")

        diff_lines = list(
            difflib.unified_diff(
                [line + "\n" for line in textfile.split_lines(text)],
                [line + "\n" for line in textfile.split_lines(new_text)],
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


def _error(message: str) -> ToolResult:
    return ToolResult(content=f"Error: {message}", is_error=True)


def _locate(text: str, old: str, new: str) -> tuple[str, str, int]:
    """The strings to replace with, in the file's own line endings.

    In a CRLF file the model's LF strings are tried as CRLF first, so an edit
    that spans lines matches and one that adds lines does not leave bare LFs
    behind. Only if that finds nothing are they tried exactly as written --
    which is what finds an LF stretch inside a mostly-CRLF file.
    """
    if textfile.newline_of(text) == "\r\n":
        crlf_old = textfile.with_newlines(old, "\r\n")
        count = text.count(crlf_old)
        if count:
            return crlf_old, textfile.with_newlines(new, "\r\n"), count
    return old, new, text.count(old)


def _lines_of(text: str, needle: str) -> str:
    """The 1-based line numbers where ``needle`` starts, for the ambiguity error."""
    found: list[str] = []
    start = text.find(needle)
    while start != -1 and len(found) < MAX_LISTED_MATCHES:
        found.append(str(text.count("\n", 0, start) + 1))
        start = text.find(needle, start + len(needle))
    more = ", …" if start != -1 else ""
    return ", ".join(found) + more
