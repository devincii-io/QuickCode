"""What an edit or write would change, as the permission prompt shows it.

A unified diff (stdlib ``difflib``), capped so that rewriting a large file
cannot turn the prompt -- or the session log line that records the request --
into a copy of the file.

It is built only from text the session already holds: a file's current content
is used when the session has read it (``edit`` and ``write`` refuse to change
any other existing file, so that is also the only case in which the diff is
what will happen), and otherwise only the call's own text is shown. A prompt
for a file nobody read must not be the way its content reaches the log.
"""

from __future__ import annotations

import difflib
from pathlib import Path

from quickcode.tools.fs import textfile

MAX_LINES = 200
MAX_LINE_CHARS = 400
# Past this a file is not read for the preview at all: the call's own text is
# shown instead, and the prompt stays fast.
MAX_FILE_BYTES = 2 * 1024 * 1024


def target(file_path: str, ctx) -> Path:
    """The path the tool will act on, resolved the way the tools resolve it."""
    path = Path(file_path)
    return path if path.is_absolute() else ctx.cwd / path


def exists(path: Path) -> bool:
    try:
        return path.exists()
    except (OSError, ValueError):
        return False


def seen_text(path: Path, ctx) -> str | None:
    """The file's current text, when this session has read it and it is text."""
    try:
        if not ctx.read_registry.was_read(str(path)) or not path.is_file():
            return None
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        raw = path.read_bytes()
        return textfile.decode(raw, textfile.detect(raw))
    except (OSError, ValueError):  # NotText and a failed decode are ValueErrors
        return None


def unified(old: str, new: str, fromfile: str, tofile: str) -> str:
    lines = list(difflib.unified_diff(
        textfile.split_lines(old), textfile.split_lines(new),
        fromfile=fromfile, tofile=tofile, lineterm="",
    ))
    return capped(lines)


def capped(lines: list[str]) -> str:
    shown = [line if len(line) <= MAX_LINE_CHARS else line[:MAX_LINE_CHARS] + "…"
             for line in lines[:MAX_LINES]]
    if len(lines) > MAX_LINES:
        shown.append(f"… {len(lines) - MAX_LINES} more diff lines not shown")
    return "\n".join(shown)
