"""The process edges of a ``quickcode -p`` run.

A headless run is used by scripts, and a script sees three things: what goes
in on stdin, what comes out on stdout/stderr, and the exit status. Each had a
way to go wrong that no amount of correct agent behaviour could fix:

* **stdin** was read with whatever codec Python picked for the pipe. On a
  German Windows that is cp1252 bytes into a strict UTF-8 reader, or lone
  surrogates that die later in the session log. And with a terminal on stdin
  and no prompt, ``read()`` simply waited for a human who was not there.
* **stdout** printed with the console's codec, so a ✓ in the answer raised
  ``UnicodeEncodeError`` after the whole turn had run and the answer was lost.
* **the exit status** was 0 whenever nothing raised — and the loop reports a
  provider failure on the event bus rather than by raising, so a bad API key
  printed an empty line and "succeeded".
"""

from __future__ import annotations

import asyncio
import sys
from typing import IO, Any

from quickcode.core.events import TurnDone

EXIT_OK = 0
EXIT_TURN_FAILED = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130  # 128 + SIGINT, what a shell reports for Ctrl+C

_BOM = "﻿"


def prompt_from_stdin(stdin: IO[str] | None) -> str | None:
    """The prompt piped in, or None when there is no pipe to read.

    A terminal is not a pipe: nobody is going to type into it, and reading it
    would hang the run. Bytes are decoded the way command output is
    (``tools.base.decode_output``), never with surrogates.
    """
    if stdin is None:
        return None
    try:
        if stdin.isatty():
            return None
    except (AttributeError, ValueError):  # no isatty, or already closed
        return None
    buffer = getattr(stdin, "buffer", None)
    if buffer is None:
        return stdin.read()
    from quickcode.tools.base import decode_output

    text = decode_output(buffer.read())
    # PowerShell and Notepad put a byte-order mark in front of UTF-8.
    return text[1:] if text.startswith(_BOM) else text


def emit(text: str, stream: IO[str] | None = None) -> None:
    """Print ``text`` to ``stream`` even if its codec cannot hold all of it.

    Unencodable characters become ``?`` rather than an exception: a script
    reading the output can live with a replaced glyph, not with no output.
    """
    stream = sys.stdout if stream is None else stream
    if stream is None:  # pythonw: nowhere to print
        return
    try:
        print(text, file=stream)
        return
    except UnicodeEncodeError:
        pass
    encoding = getattr(stream, "encoding", None) or "ascii"
    safe = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
    print(safe, file=stream)


def watch_for_failure(bus: Any) -> asyncio.Queue:
    """Subscribe to ``bus`` before the turn, to learn afterwards whether it failed."""
    return bus.subscribe(maxsize=0)


def turn_failure(watch: asyncio.Queue) -> str | None:
    """The error that ended the turn, if one did."""
    failure = None
    while not watch.empty():
        ev = watch.get_nowait()
        if isinstance(ev, TurnDone) and ev.error:
            failure = ev.error
    return failure
