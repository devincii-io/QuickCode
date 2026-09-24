"""The checkpoint store: one conversation's file contents from before each turn.

Layout, under ``<project>/.quickcode/checkpoints/<conv_id>/``::

    index.json       which files each turn changed, and what they held before
    blobs/<sha256>   the bytes, content-addressed: one copy however many turns
                     started from the same contents

``.quickcode/checkpoints/.gitignore`` holds ``*``: these are copies of project
files, secrets included, and a project whose ``.quickcode/.gitignore`` predates
this directory would otherwise commit them.

The index is small and rewritten whole, atomically (``fsutil``), under one lock
per conversation shared by the recording hook and the API's worker threads.
Every operation re-reads it from disk, so the two never hold diverging copies.

Size is capped per conversation. When a new snapshot does not fit, the oldest
turn's snapshots are evicted first: its entries stay in the index marked
``restorable: false, reason: "evicted"``, so a rewind that needed one says so
instead of restoring something else, and blobs nothing live refers to are
deleted.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import logging
import re
import threading
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from quickcode.checkpoints import paths
from quickcode.checkpoints.diff import line_delta
from quickcode.checkpoints.snapshot import Snapshot, digest
from quickcode.fsutil import atomic_write_bytes, atomic_write_text
from quickcode.session.store import CHECKPOINTS_DIRNAME, safe_conv_id
from quickcode.workspace import ensure_project_dir

log = logging.getLogger("quickcode.checkpoints")

INDEX_VERSION = 1
DEFAULT_MAX_BYTES = 128 * 1024 * 1024
DEFAULT_MAX_FILE_BYTES = 8 * 1024 * 1024
# Call ids kept per file per turn. A turn that edits one file two hundred times
# does not need two hundred ids to say which calls did it.
MAX_CALLS_PER_ENTRY = 64

_SHA = re.compile(r"[0-9a-f]{64}\Z")

GITIGNORE = (
    "# Written by QuickCode: copies of project files from before each agent turn,\n"
    "# kept so a rewind can put them back. Never meant for a commit.\n"
    "*\n"
)

# Why an entry cannot be restored. Codes, so the UI can word them itself.
TOO_LARGE = "too_large"      # over the per-file limit when it was first changed
SIZE_CAP = "size_cap"        # did not fit under the conversation's cap
EVICTED = "evicted"          # saved, then dropped to make room for newer turns

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(directory: Path) -> threading.RLock:
    key = str(directory.resolve())
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _load(cls, raw: Any):
    """A dataclass from a dict, ignoring keys it does not know."""
    if not isinstance(raw, dict):
        raise ValueError(f"expected an object for {cls.__name__}")
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in raw.items() if k in known})


@dataclass
class FileEntry:
    """One file, the first time a turn changed it, and where the turn left it."""

    path: str
    # sha256 of the bytes before the turn's first change; None: it did not exist.
    before: str | None
    # sha256 after the turn's last recorded change; None: it no longer exists.
    after: str | None
    size: int = 0
    restorable: bool = True
    reason: str = ""
    tool: str = ""
    agent: str = ""
    calls: list[str] = field(default_factory=list)
    time: str = ""
    added: int | None = None
    removed: int | None = None
    binary: bool = False
    # The rewind that undid this entry; it no longer takes part in rewinds.
    rewound: str = ""

    @property
    def active(self) -> bool:
        return not self.rewound

    @property
    def change(self) -> str:
        if self.before is None:
            return "created"
        if self.after is None:
            return "deleted"
        return "modified"


@dataclass
class TurnCheckpoint:
    turn: int
    seq: int
    time: str
    files: list[FileEntry] = field(default_factory=list)

    def entry(self, rel: str) -> FileEntry | None:
        """The file's live entry in this turn. One a rewind undid does not count:
        a change after it is a new one, with its own before."""
        key = paths.fold(rel)
        return next((e for e in self.files if e.active and paths.fold(e.path) == key), None)


@dataclass
class RewindFile:
    path: str
    # restored | deleted | created | unchanged
    action: str
    from_turn: int
    # What the rewind overwrote (sha256), None if nothing was there, and
    # whether a copy of it is kept.
    replaced: str | None = None
    size: int = 0
    backup: bool = False
    restored: str | None = None


@dataclass
class RewindRecord:
    id: str
    seq: int
    time: str
    turn: int
    forced: bool = False
    # Whether the model has been told. The next turn's reminder sets it.
    announced: bool = False
    files: list[RewindFile] = field(default_factory=list)


@dataclass
class Index:
    seq: int = 0
    turns: list[TurnCheckpoint] = field(default_factory=list)
    rewinds: list[RewindRecord] = field(default_factory=list)

    def checkpoint(self, turn: int) -> TurnCheckpoint | None:
        return next((c for c in self.turns if c.turn == turn), None)

    def open_turn(self, turn: int) -> TurnCheckpoint:
        found = self.checkpoint(turn)
        if found is None:
            self.seq += 1
            found = TurnCheckpoint(turn=turn, seq=self.seq, time=now())
            self.turns.append(found)
            self.turns.sort(key=lambda c: c.turn)
        return found

    def live_blobs(self) -> dict[str, int]:
        """Every blob something could still restore, with its size."""
        live: dict[str, int] = {}
        for cp in self.turns:
            for e in cp.files:
                if e.active and e.restorable and e.before is not None:
                    live[e.before] = e.size
        for rw in self.rewinds:
            for f in rw.files:
                if f.backup and f.replaced is not None:
                    live[f.replaced] = f.size
        return live

    def used_bytes(self) -> int:
        return sum(self.live_blobs().values())

    def to_json(self) -> dict[str, Any]:
        return {"version": INDEX_VERSION, "seq": self.seq,
                "turns": [asdict(c) for c in self.turns],
                "rewinds": [asdict(r) for r in self.rewinds]}

    @classmethod
    def from_json(cls, raw: Any) -> Index:
        if not isinstance(raw, dict):
            raise ValueError("index is not an object")
        turns = []
        for c in raw.get("turns") or []:
            cp = _load(TurnCheckpoint, {**c, "files": []})
            cp.files = [_load(FileEntry, f) for f in c.get("files") or []]
            turns.append(cp)
        rewinds = []
        for r in raw.get("rewinds") or []:
            rw = _load(RewindRecord, {**r, "files": []})
            rw.files = [_load(RewindFile, f) for f in r.get("files") or []]
            rewinds.append(rw)
        return cls(seq=int(raw.get("seq") or 0), turns=sorted(turns, key=lambda c: c.turn),
                   rewinds=rewinds)


class CheckpointStore:
    """One conversation's checkpoints on disk."""

    def __init__(self, root: Path | str, conv_id: str, *,
                 max_bytes: int | None = None, max_file_bytes: int | None = None) -> None:
        if not safe_conv_id(conv_id):
            raise ValueError(f"not a conversation id: {conv_id!r}")
        self.root = Path(root)
        self.conv_id = conv_id
        self.max_bytes = DEFAULT_MAX_BYTES if max_bytes is None else max_bytes
        self.max_file_bytes = min(
            DEFAULT_MAX_FILE_BYTES if max_file_bytes is None else max_file_bytes,
            self.max_bytes)
        self.dir = self.root / CHECKPOINTS_DIRNAME / conv_id
        self.index_path = self.dir / "index.json"
        self.blob_dir = self.dir / "blobs"
        self._lock = _lock_for(self.dir)

    def exists(self) -> bool:
        return self.index_path.is_file()

    # ---- the index ----

    def load(self) -> Index:
        with self._lock:
            return self._read()[0]

    @contextlib.contextmanager
    def transaction(self) -> Iterator[Index]:
        """The index, locked; written back on the way out if it changed."""
        with self._lock:
            index, raw = self._read()
            yield index
            text = json.dumps(index.to_json(), indent=1)
            if text != raw:
                self._prepare()
                atomic_write_text(self.index_path, text)

    def _read(self) -> tuple[Index, str]:
        try:
            raw = self.index_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return Index(), json.dumps(Index().to_json(), indent=1)
        try:
            return Index.from_json(json.loads(raw)), raw
        except (ValueError, TypeError) as exc:
            # Set aside rather than overwritten: whatever it still says is the
            # only record of which blobs belong to which turn.
            aside = self.index_path.with_name(f"index.damaged-{datetime.datetime.now():%Y%m%d%H%M%S}.json")
            log.warning("checkpoint index %s is unreadable (%s); moved to %s",
                        self.index_path, exc, aside.name)
            with contextlib.suppress(OSError):
                self.index_path.replace(aside)
            return Index(), ""

    def _prepare(self) -> None:
        ensure_project_dir(self.root)
        top = self.dir.parent
        top.mkdir(parents=True, exist_ok=True)
        guard = top / ".gitignore"
        if not guard.exists():
            with contextlib.suppress(OSError):
                guard.write_text(GITIGNORE, encoding="utf-8")
        self.blob_dir.mkdir(parents=True, exist_ok=True)

    # ---- blobs ----

    def _blob_path(self, sha: str) -> Path | None:
        return self.blob_dir / sha if isinstance(sha, str) and _SHA.match(sha) else None

    def has_blob(self, sha: str) -> bool:
        path = self._blob_path(sha)
        return path is not None and path.is_file()

    def read_blob(self, sha: str | None) -> bytes | None:
        """The saved bytes for ``sha``; None if missing or no longer matching it."""
        path = self._blob_path(sha or "")
        if path is None:
            return None
        try:
            data = path.read_bytes()
        except OSError:
            return None
        return data if digest(data) == sha else None

    def _put_blob(self, sha: str, data: bytes) -> None:
        path = self._blob_path(sha)
        if path is None:
            raise ValueError("bad digest")
        if path.is_file() and self.read_blob(sha) is not None:
            return
        self._prepare()
        atomic_write_bytes(path, data)

    def keep_if_room(self, index: Index, sha: str, data: bytes) -> bool:
        """Save ``data`` without evicting anything. False when it does not fit."""
        live = index.live_blobs()
        incoming = 0 if sha in live else len(data)
        if len(data) > self.max_file_bytes or sum(live.values()) + incoming > self.max_bytes:
            return False
        self._put_blob(sha, data)
        return True

    def collect_garbage(self, index: Index) -> None:
        """Delete every blob the index no longer needs."""
        live = index.live_blobs()
        try:
            names = [p for p in self.blob_dir.iterdir() if p.name not in live]
        except OSError:
            return
        for path in names:
            with contextlib.suppress(OSError):
                path.unlink()

    def _fit(self, index: Index, incoming: int, keep_seq: int) -> bool:
        """Evict oldest-first until ``incoming`` more bytes fit. False if they cannot.

        ``keep_seq`` is the turn being recorded into, which is never evicted to
        make room for itself.
        """
        if incoming > self.max_bytes:
            return False
        evicted = False
        while index.used_bytes() + incoming > self.max_bytes:
            victim = self._oldest_unit(index, keep_seq)
            if victim is None:
                return False
            _evict(victim)
            evicted = True
        if evicted:
            self.collect_garbage(index)
        return True

    @staticmethod
    def _oldest_unit(index: Index, keep_seq: int) -> TurnCheckpoint | RewindRecord | None:
        units: list[TurnCheckpoint | RewindRecord] = [*index.turns, *index.rewinds]
        for unit in sorted(units, key=lambda u: u.seq):
            if unit.seq != keep_seq and _holds_blobs(unit):
                return unit
        return None

    # ---- recording ----

    def record(self, turn: int, rel: str, before: Snapshot, after: Snapshot, *,
               call_id: str = "", tool: str = "", agent: str = "") -> tuple[FileEntry, bool]:
        """Note that ``rel`` changed from ``before`` to ``after`` during ``turn``.

        The first change a turn makes to a file saves ``before``; later ones only
        move ``after`` along. Returns the entry and whether it is new.
        """
        with self.transaction() as index:
            cp = index.open_turn(turn)
            entry = cp.entry(rel)
            created = entry is None
            if entry is None:
                entry = FileEntry(path=rel, before=before.digest, after=after.digest,
                                  size=before.size if before.exists else 0,
                                  tool=tool, agent=agent, time=now())
                if before.exists:
                    self._save_before(index, cp, entry, before)
                cp.files.append(entry)
                before_data = before.data if before.exists else None
                before_known = not before.exists or before.data is not None
            else:
                entry.after = after.digest
                before_data = self.read_blob(entry.before) if entry.before else None
                before_known = entry.before is None or before_data is not None
            if call_id and call_id not in entry.calls and len(entry.calls) < MAX_CALLS_PER_ENTRY:
                entry.calls.append(call_id)
            after_known = not after.exists or after.data is not None
            if before_known and after_known:
                entry.added, entry.removed, entry.binary = line_delta(
                    before_data, after.data if after.exists else None)
            else:
                entry.added = entry.removed = None
        return entry, created

    def _save_before(self, index: Index, cp: TurnCheckpoint, entry: FileEntry,
                     before: Snapshot) -> None:
        if before.data is None or before.digest is None:
            entry.restorable, entry.reason = False, TOO_LARGE
            return
        incoming = 0 if before.digest in index.live_blobs() else len(before.data)
        if not self._fit(index, incoming, keep_seq=cp.seq):
            entry.restorable, entry.reason = False, SIZE_CAP
            return
        self._put_blob(before.digest, before.data)

    def take_unannounced(self) -> list[RewindRecord]:
        """Rewinds the model has not been told about, now marked as told."""
        if not self.exists():
            return []
        with self.transaction() as index:
            fresh = [r for r in index.rewinds if not r.announced]
            for r in fresh:
                r.announced = True
        return fresh


def _holds_blobs(unit: TurnCheckpoint | RewindRecord) -> bool:
    if isinstance(unit, TurnCheckpoint):
        return any(e.active and e.restorable and e.before is not None for e in unit.files)
    return any(f.backup for f in unit.files)


def _evict(unit: TurnCheckpoint | RewindRecord) -> None:
    if isinstance(unit, TurnCheckpoint):
        for e in unit.files:
            # A file the turn created needs no blob to undo: that one stays.
            if e.active and e.restorable and e.before is not None:
                e.restorable, e.reason = False, EVICTED
    else:
        for f in unit.files:
            f.backup = False
