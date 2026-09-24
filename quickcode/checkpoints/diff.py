"""Line counts and unified diffs over raw file bytes.

Text is decoded the way the file tools decode it (``tools/fs/textfile.py``:
BOM, then UTF-8, then the legacy code pages), so a cp1252 file diffs as the
words in it rather than as replacement characters. Line endings are kept on
each line: a rewind that would turn CRLF back into LF is a change, and a diff
that normalised it away would show nothing where something is about to happen.
"""

from __future__ import annotations

import difflib

from quickcode.tools.fs import textfile

# Past this many bytes on either side no diff is computed: a SequenceMatcher
# over two large files is seconds of CPU for a preview nobody reads whole.
MAX_DIFF_BYTES = 2_000_000
MAX_DIFF_CHARS = 100_000
CONTEXT_LINES = 3

_NO_EOL = "\n\\ No newline at end of file\n"


def _text(raw: bytes) -> str | None:
    """``raw`` as text, or None when it is binary."""
    try:
        encoding = textfile.detect(raw)
    except textfile.NotText:
        return None
    return textfile.decode(raw, encoding, errors="replace")


def _lines(text: str) -> list[str]:
    parts = text.split("\n")
    lines = [p + "\n" for p in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1] + _NO_EOL)
    return lines


def _both(before: bytes | None, after: bytes | None) -> tuple[list[str], list[str]] | None:
    """Both sides as lines; an absent file is empty. None when either is binary."""
    old = _text(before or b"")
    new = _text(after or b"")
    if old is None or new is None:
        return None
    return _lines(old), _lines(new)


def line_delta(before: bytes | None, after: bytes | None) -> tuple[int | None, int | None, bool]:
    """``(added, removed, binary)`` from ``before`` to ``after``.

    ``None`` for either side means the file did not exist. The counts are None
    when a side is too large to compare; ``binary`` says a side is not text.
    """
    if len(before or b"") > MAX_DIFF_BYTES or len(after or b"") > MAX_DIFF_BYTES:
        return None, None, False
    sides = _both(before, after)
    if sides is None:
        return None, None, True
    added = removed = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, *sides).get_opcodes():
        if tag in ("replace", "delete"):
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    return added, removed, False


def unified(path: str, before: bytes | None, after: bytes | None) -> dict:
    """The change from ``before`` (on disk now) to ``after`` (what a rewind writes).

    Returns ``{"diff", "added", "removed", "binary", "truncated", "omitted"}``.
    ``omitted`` names why there is no diff text: ``"binary"`` or ``"too_large"``.
    """
    out = {"diff": "", "added": None, "removed": None, "binary": False,
           "truncated": False, "omitted": ""}
    if len(before or b"") > MAX_DIFF_BYTES or len(after or b"") > MAX_DIFF_BYTES:
        out["omitted"] = "too_large"
        return out
    sides = _both(before, after)
    if sides is None:
        out.update(binary=True, omitted="binary")
        return out
    old, new = sides
    body = difflib.unified_diff(
        old, new,
        fromfile="/dev/null" if before is None else f"a/{path}",
        tofile="/dev/null" if after is None else f"b/{path}",
        n=CONTEXT_LINES,
    )
    added = removed = 0
    chunks: list[str] = []
    size = 0
    for n, line in enumerate(body):
        # The first two lines are the ---/+++ header; a removed line that
        # itself begins with "--" must still count.
        if n >= 2 and line.startswith("+"):
            added += 1
        elif n >= 2 and line.startswith("-"):
            removed += 1
        if size < MAX_DIFF_CHARS:
            chunks.append(line)
            size += len(line)
        else:
            out["truncated"] = True
    out.update(diff="".join(chunks), added=added, removed=removed)
    return out
