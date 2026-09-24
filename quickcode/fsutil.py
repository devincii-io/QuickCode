"""Replacing a file so a crash leaves the old contents or the new, never half.

The data is written to a temporary file beside the target and renamed over it:
a rename within one directory is atomic on every platform QuickCode runs on.
The temporary name starts with a dot, which no plugin or config scan reads, and
is unique per call, so two writers in one process cannot collide on it.

Durability is the caller's choice: ``fsync=True`` survives a power cut as well
as a crash, and costs a disk flush.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path


def atomic_write_bytes(path: str | os.PathLike[str], data: bytes, *,
                       fsync: bool = False) -> None:
    _replace(Path(path), "wb", data, fsync=fsync)


def atomic_write_text(path: str | os.PathLike[str], text: str, *,
                      encoding: str = "utf-8", newline: str | None = None,
                      fsync: bool = False) -> None:
    """Same arguments and newline translation as ``Path.write_text``."""
    _replace(Path(path), "w", text, fsync=fsync, encoding=encoding, newline=newline)


def _replace(path: Path, mode: str, payload: str | bytes, *, fsync: bool,
             encoding: str | None = None, newline: str | None = None) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp")
    try:
        with open(tmp, mode.replace("w", "x"), encoding=encoding, newline=newline) as fh:
            fh.write(payload)
            if fsync:
                fh.flush()
                os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
