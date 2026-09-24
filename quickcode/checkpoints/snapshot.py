"""What is at a path right now: absent, or a regular file's digest and bytes."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Snapshot:
    exists: bool
    digest: str | None = None
    # The bytes, when the file is small enough to keep a copy of. None for an
    # absent file and for one over the size limit, whose digest is still known.
    data: bytes | None = None
    size: int = 0


ABSENT = Snapshot(False)


def take(path: Path, max_bytes: int) -> Snapshot | None:
    """``path`` as it is now, or None when a checkpoint cannot hold it.

    None means a directory, a device or a file that cannot be read: nothing a
    rewind could put back, and nothing worth claiming was saved.
    """
    try:
        st = os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return ABSENT
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    try:
        if st.st_size > max_bytes:
            with open(path, "rb") as fh:
                return Snapshot(True, hashlib.file_digest(fh, "sha256").hexdigest(),
                                None, st.st_size)
        data = path.read_bytes()
    except FileNotFoundError:
        return ABSENT
    except OSError:
        return None
    # Grown past the limit between the stat and the read: keep the digest only.
    kept = data if len(data) <= max_bytes else None
    return Snapshot(True, digest(data), kept, len(data))


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
