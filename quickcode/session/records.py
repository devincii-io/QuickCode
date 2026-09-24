"""Parsing session-log bytes without trusting them.

A log is written one line at a time by a process that can die mid-write, and
it lives in the user's project where an editor can add a BOM or CRLFs and a
crashed filesystem can leave NUL padding where data never landed. None of that
may cost more than the line it damaged: every line is decoded and parsed on its
own, and a line that does not come out as a JSON object is counted and skipped.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

_BOM = b"\xef\xbb\xbf"
# NUL is included because it is what a crash leaves when the file's length was
# extended before its data reached the disk.
_BLANK = b" \t\r\n\x00"


@dataclass
class Parsed:
    records: list[dict[str, Any]] = field(default_factory=list)
    #: 1-based line numbers of lines that could not be read as a record.
    damaged: list[int] = field(default_factory=list)
    #: How many bytes were consumed. Short of the input only when it ends in a
    #: torn record, which a later append will complete into a (damaged) line.
    end: int = 0


def _record(raw: bytes) -> dict[str, Any] | None:
    try:
        value = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def parse(data: bytes, *, at_start: bool = True, first_line: int = 1) -> Parsed:
    """Every record in ``data``, plus where the damage was.

    ``at_start`` says ``data`` begins at byte 0 of the file, which is the only
    place a BOM can legitimately be. ``first_line`` numbers the lines when
    ``data`` is a tail read from further in.

    Invalid UTF-8 inside a string is replaced rather than rejected: the record
    around it is still worth having.
    """
    out = Parsed()
    pos = 0
    if at_start and data.startswith(_BOM):
        pos = len(_BOM)
    lineno = first_line
    size = len(data)
    while pos < size:
        nl = data.find(b"\n", pos)
        terminated = nl != -1
        stop = nl if terminated else size
        raw = data[pos:stop].strip(_BLANK)
        if raw:
            rec = _record(raw)
            if rec is not None:
                out.records.append(rec)
            else:
                out.damaged.append(lineno)
                if not terminated:
                    # A torn tail: leave it unconsumed.
                    out.end = pos
                    return out
        pos = stop + 1 if terminated else size
        lineno += 1
    out.end = size
    return out
