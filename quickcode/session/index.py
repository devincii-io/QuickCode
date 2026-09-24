"""The session list's cache: one summary per log, kept beside the logs.

Listing used to parse every log in full, several times over, to show a title
and a count -- about two seconds for three hundred sessions of ordinary length,
paid on every refresh of the sidebar. Logs are append-only, so a summary taken
once stays true of the bytes it covered, and a log that has grown only needs
its new tail folded in (see ``summary.Summary``).

``index.json`` lives in the sessions directory. It is a cache and nothing but:
not a ``*.jsonl``, so no listing ever mistakes it for a session; rebuilt from
the logs whenever it is missing, unreadable or of another version; and never
trusted for a log whose bytes it cannot vouch for. An entry is reused when the
log's size and mtime are the ones it recorded, extended when the log grew and
the bytes just before the recorded offset are still the ones it saw, and
recomputed from scratch otherwise.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from quickcode.session.records import parse
from quickcode.session.summary import Summary

INDEX_NAME = "index.json"
VERSION = 1
# How many bytes before the recorded offset must still match for a grown log
# to be read from there. Enough to cover the end of the last record folded.
_TAIL = 64


@dataclass
class _Entry:
    size: int
    mtime_ns: int
    offset: int
    tail: str
    summary: Summary

    def to_json(self) -> dict[str, Any]:
        return {
            "size": self.size, "mtime_ns": self.mtime_ns, "offset": self.offset,
            "tail": self.tail, "summary": self.summary.to_json(),
        }

    @classmethod
    def from_json(cls, raw: Any) -> _Entry | None:
        if not isinstance(raw, dict):
            return None
        summary = Summary.from_json(raw.get("summary"))
        if summary is None:
            return None
        try:
            return cls(
                size=int(raw["size"]), mtime_ns=int(raw["mtime_ns"]),
                offset=int(raw["offset"]), tail=str(raw["tail"]), summary=summary,
            )
        except (KeyError, TypeError, ValueError):
            return None


class SessionIndex:
    """Summaries for the logs in one sessions directory."""

    def __init__(self, sessions_dir: Path) -> None:
        self.sessions_dir = Path(sessions_dir)
        self.path = self.sessions_dir / INDEX_NAME
        self._entries: dict[str, _Entry] = {}
        self._dirty = False
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_bytes())
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict) or raw.get("version") != VERSION:
            return
        for key, value in (raw.get("sessions") or {}).items():
            entry = _Entry.from_json(value)
            if entry is not None:
                self._entries[str(key)] = entry

    def summary(self, key: str, path: Path, st: os.stat_result) -> Summary:
        """The summary of the log at ``path``, reading as little as it can.

        Raises ``OSError`` if the log cannot be read at all.
        """
        entry = self._entries.get(key)
        if entry is not None and (entry.size, entry.mtime_ns) == (st.st_size, st.st_mtime_ns):
            return entry.summary
        if entry is not None and 0 < entry.offset <= st.st_size:
            grown = self._extend(entry, path, st)
            if grown is not None:
                self._entries[key] = grown
                self._dirty = True
                return grown.summary
        fresh = self._build(path, st)
        self._entries[key] = fresh
        self._dirty = True
        return fresh.summary

    def _extend(self, entry: _Entry, path: Path, st: os.stat_result) -> _Entry | None:
        start = max(0, entry.offset - _TAIL)
        with path.open("rb") as f:
            f.seek(start)
            data = f.read()
        head, new = data[: entry.offset - start], data[entry.offset - start:]
        if head.hex() != entry.tail:
            return None
        summary = Summary.from_json(entry.summary.to_json()) or Summary()
        return self._fold(summary, new, base=entry.offset, st=st, at_start=False,
                          before=head)

    def _build(self, path: Path, st: os.stat_result) -> _Entry:
        return self._fold(Summary(), path.read_bytes(), base=0, st=st, at_start=True,
                          before=b"")

    @staticmethod
    def _fold(summary: Summary, data: bytes, *, base: int, st: os.stat_result,
              at_start: bool, before: bytes) -> _Entry:
        parsed = parse(data, at_start=at_start)
        summary.fold(parsed.records)
        consumed = data[: parsed.end]
        summary.add_artifacts(consumed.decode("utf-8", errors="replace"))
        offset = base + parsed.end
        tail = (before + consumed)[-_TAIL:]
        # The size recorded is the one this read saw, which may exceed the
        # stat taken before it if the log grew meanwhile; a mismatch next time
        # only means the tail is read again.
        return _Entry(size=base + len(data), mtime_ns=st.st_mtime_ns, offset=offset,
                      tail=tail.hex(), summary=summary)

    def prune(self, prefix: str, present: set[str]) -> None:
        """Forget the logs under ``prefix`` that are no longer there.

        ``prefix`` is "" for the active directory and "archive/" for the
        archive; only the directory that was just listed is pruned.
        """
        for key in list(self._entries):
            in_scope = key.startswith(prefix) if prefix else "/" not in key
            if in_scope and key not in present:
                del self._entries[key]
                self._dirty = True

    def save(self) -> None:
        """Write the index if anything changed. A failure is not an error."""
        if not self._dirty:
            return
        body = {
            "version": VERSION,
            "sessions": {k: e.to_json() for k, e in sorted(self._entries.items())},
        }
        try:
            fd, tmp = tempfile.mkstemp(prefix=".index-", suffix=".tmp", dir=self.sessions_dir)
        except OSError:
            return
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(body, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, self.path)
            self._dirty = False
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
