"""Read tool: view a text file with numbered lines.

Reads a file from disk and returns its content as 1-indexed, arrow-prefixed
lines (``   123→text``), the same shape a human sees in an editor gutter.
Records the file's mtime in ``ctx.read_registry`` so Edit/Write can later
verify the file hasn't changed underneath the agent.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from quickcode.tools.base import PermissionSpec, Tool, ToolCtx, ToolResult
from quickcode.tools.fs import textfile

DEFAULT_LIMIT = 2000
MAX_LINE_CHARS = 2000
MAX_OUTPUT_CHARS = 40_000
# Past this size a file is not held in memory: the window is streamed out of
# it line by line, and the total is not counted. Reading a multi-gigabyte log
# whole to show two thousand lines of it is how a read took the app down.
WHOLE_FILE_BYTES = 10_000_000
# How much of a streamed file is looked at to decide its encoding.
ENCODING_SAMPLE_BYTES = 65_536


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
    permission = PermissionSpec(mutates=False, target_field="file_path", path_target=True)
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

        offset = input.offset if input.offset and input.offset > 0 else 1
        limit = input.limit if input.limit and input.limit > 0 else DEFAULT_LIMIT
        try:
            stat = path.stat()
            if stat.st_size <= WHOLE_FILE_BYTES:
                view = _read_whole(path, offset, limit)
            else:
                view = _read_streamed(path, offset, limit)
        except textfile.NotText:
            return ToolResult(
                content=(
                    f"Error: {path} is a binary file ({stat.st_size:,} bytes), not "
                    "text. read shows text files only; inspect it with bash "
                    "(file, xxd, a format-specific tool) if you need to."
                ),
                is_error=True,
            )
        except OSError as exc:
            return ToolResult(content=f"Error: could not read {path}: {exc}", is_error=True)

        ctx.read_registry.record(str(path), stat.st_mtime, view.digest)

        if view.seen == 0:
            return ToolResult(content="(the file is empty)")
        if not view.lines:
            return ToolResult(
                content=(
                    f"Error: offset {offset} is past the end of the file "
                    f"({view.seen} lines total)."
                ),
                is_error=True,
            )
        return ToolResult(content=_render(view, offset))


@dataclass
class _View:
    lines: list[str]      # the window, starting at the requested offset
    seen: int             # lines counted: all of them, unless the read stopped early
    more: bool            # whether lines follow the window
    total: int | None     # None when the file was streamed and not counted
    encoding: textfile.Encoding
    digest: str | None    # of the whole file, when it was read whole


def _read_whole(path: Path, offset: int, limit: int) -> _View:
    raw = path.read_bytes()
    encoding = textfile.detect(raw)
    lines = textfile.split_lines(textfile.decode(raw, encoding))
    window = lines[offset - 1:offset - 1 + limit]
    return _View(
        lines=window,
        seen=len(lines),
        more=offset - 1 + len(window) < len(lines),
        total=len(lines),
        encoding=encoding,
        digest=textfile.digest(raw),
    )


def _read_streamed(path: Path, offset: int, limit: int) -> _View:
    with path.open("rb") as fh:
        encoding = textfile.detect(fh.read(ENCODING_SAMPLE_BYTES), complete=False)
    window: list[str] = []
    seen = 0
    more = False
    # ``newline="\n"`` splits on LF only and translates nothing, which is the
    # numbering ``textfile.split_lines`` gives a file read whole.
    with path.open("r", encoding=encoding.codec, errors="replace", newline="\n") as fh:
        for line in fh:
            if seen == 0 and encoding.bom:
                line = line.removeprefix("\ufeff")
            seen += 1
            if seen < offset:
                continue
            if len(window) == limit:
                more = True
                break
            window.append(line.removesuffix("\n").removesuffix("\r"))
    return _View(lines=window, seen=seen, more=more, total=None,
                 encoding=encoding, digest=None)


def _render(view: _View, offset: int) -> str:
    """Numbered lines, then a marker if the file goes on.

    Lines are dropped whole when the output cap bites, so the marker's offset
    is exactly the first line that was not shown. Cutting the string instead
    left a half line and an offset pointing past everything that was cut.
    """
    out: list[str] = []
    used = 0
    last = offset - 1
    capped = False
    for number, line in enumerate(view.lines, start=offset):
        if len(line) > MAX_LINE_CHARS:
            line = line[:MAX_LINE_CHARS] + "…[cut]"
        row = f"{number:6d}→{line}"
        if out and used + len(row) + 1 > MAX_OUTPUT_CHARS:
            capped = True
            break
        out.append(row)
        used += len(row) + 1
        last = number

    if capped or view.more:
        total = f' total="{view.total}"' if view.total is not None else ""
        why = "output cap reached; " if capped else ""
        out.append(
            f'<truncated shown="{last - offset + 1}"{total} '
            f'hint="{why}re-read with offset={last + 1}"/>'
        )
    if not view.encoding.is_default:
        out.append(
            f'<file encoding="{view.encoding.label}" '
            'note="edit and write keep this encoding"/>'
        )
    return "\n".join(out)
