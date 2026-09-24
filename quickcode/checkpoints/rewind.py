"""Putting files back to how they were before a turn: the listing, the preview
and the act.

"Before turn N", for one file, is the contents saved by the earliest turn at
or after N that changed it -- or its absence, if that turn created it. What
the file should hold *now* is what the latest such turn left behind. The two
checks a rewind makes before touching anything follow from that:

* ``modified``: the file on disk is not what the last recorded change left.
  Something else wrote it since -- ``bash``, another program, the user.
* ``intervening``: between two recorded turns the file changed in a way no
  recorded change explains, so a rewind would discard that too.

Both are conflicts: a rewind refuses them unless forced. A file whose saved
copy is gone (evicted, too large, damaged) or whose path now leads through a
link or outside the project is never written, forced or not.

A rewind is the user's act, not a tool call. It writes exact bytes -- the
encoding, byte-order mark and line endings the file had are simply the bytes
it had -- atomically (``fsutil``), keeping the mode of the file it replaces.
What it overwrites is kept as a blob while it fits under the cap, so the
record of a rewind always says whether the replaced contents survive.
"""

from __future__ import annotations

import contextlib
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from quickcode.checkpoints import paths
from quickcode.checkpoints.diff import unified
from quickcode.checkpoints.snapshot import Snapshot, take
from quickcode.checkpoints.store import (
    CheckpointStore,
    FileEntry,
    Index,
    RewindFile,
    RewindRecord,
    now,
)
from quickcode.fsutil import atomic_write_bytes

# Said in every listing and preview, because the thing a rewind cannot see is
# the thing most worth knowing before trusting one.
UNTRACKED = (
    "Only changes made by write, edit and other tools that declare the file they "
    "write are checkpointed. Files changed by bash -- or by any program a shell "
    "command starts -- by MCP tools, or outside QuickCode are not tracked: a rewind "
    "does not restore them, and reports a tracked file they changed as a conflict."
)

# What each action does to the file on disk.
RESTORE, DELETE, CREATE, NONE = "restore", "delete", "create", "none"
_DONE = {RESTORE: "restored", DELETE: "deleted", CREATE: "created", NONE: "unchanged"}

MAX_SELECTED = 1000
# The saved copy is missing, or no longer hashes to what was saved.
DAMAGED = "damaged"


class RewindError(Exception):
    def __init__(self, status: int, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.status = status
        self.detail = {"message": message, **detail}


@dataclass
class Conflict:
    kind: str
    detail: str


@dataclass
class FilePlan:
    path: str
    from_turn: int
    last_turn: int
    target: str | None
    expected: str | None
    restorable: bool
    reason: str
    entries: list[FileEntry]
    dest: Path | None = None
    current: Snapshot | None = None
    action: str = NONE
    blocked: str = ""
    conflicts: list[Conflict] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "from_turn": self.from_turn,
            "last_turn": self.last_turn,
            "action": self.action,
            "restorable": self.restorable,
            "reason": self.reason,
            "blocked": self.blocked,
            "conflicts": [{"kind": c.kind, "detail": c.detail} for c in self.conflicts],
        }


# ---- listing ----


def listing(store: CheckpointStore) -> dict[str, Any]:
    index = store.load()
    return {
        "conv_id": store.conv_id,
        "checkpoints": [
            {
                "turn": cp.turn,
                "time": cp.time,
                "restorable": any(e.active and e.restorable for e in cp.files),
                "files": [_entry_json(e) for e in cp.files],
            }
            for cp in index.turns
        ],
        "rewinds": [
            {
                "id": rw.id,
                "time": rw.time,
                "turn": rw.turn,
                "forced": rw.forced,
                "files": [{"path": f.path, "action": f.action, "from_turn": f.from_turn,
                           "backup": f.backup} for f in rw.files],
            }
            for rw in index.rewinds
        ],
        "storage": {"used_bytes": index.used_bytes(), "max_bytes": store.max_bytes,
                    "max_file_bytes": store.max_file_bytes},
        "untracked": UNTRACKED,
    }


def _entry_json(e: FileEntry) -> dict[str, Any]:
    return {
        "path": e.path,
        "change": e.change,
        "added": e.added,
        "removed": e.removed,
        "binary": e.binary,
        "restorable": e.active and e.restorable,
        "reason": e.reason,
        "rewound": e.rewound or None,
        "tool": e.tool,
        "agent": e.agent,
        "calls": list(e.calls),
        "time": e.time,
    }


# ---- planning ----


def _plan(store: CheckpointStore, index: Index, turn: int,
          selected: list[str] | None) -> list[FilePlan]:
    chains: dict[str, list[tuple[int, FileEntry]]] = {}
    for cp in index.turns:
        if cp.turn < turn:
            continue
        for e in cp.files:
            if e.active:
                chains.setdefault(paths.fold(e.path), []).append((cp.turn, e))
    if selected is not None:
        wanted = {paths.fold(p): p for p in selected}
        unknown = sorted(p for key, p in wanted.items() if key not in chains)
        if unknown:
            raise RewindError(400, f"no checkpoint at or after turn {turn} for these files",
                              paths=unknown)
        chains = {k: v for k, v in chains.items() if k in wanted}

    root = paths.real_root(store.root)
    plans: list[FilePlan] = []
    for chain in chains.values():
        (first_turn, first), (last_turn, last) = chain[0], chain[-1]
        plan = FilePlan(
            path=first.path, from_turn=first_turn, last_turn=last_turn,
            target=first.before, expected=last.after,
            restorable=first.restorable, reason=first.reason,
            entries=[e for _, e in chain],
        )
        if plan.restorable and plan.target is not None and not store.has_blob(plan.target):
            plan.restorable, plan.reason = False, DAMAGED
        for (ta, a), (tb, b) in zip(chain, chain[1:], strict=False):
            if a.after != b.before:
                plan.conflicts.append(Conflict(
                    "intervening",
                    f"changed between turn {ta} and turn {tb} by something the "
                    "checkpoints did not record; rewinding discards that change too",
                ))
        if paths.in_git_dir(first.path):
            plan.blocked = ("it is inside the repository's .git directory, which a rewind "
                            "never writes; restore it with git")
        elif (dest := paths.target(root, first.path)) is None:
            plan.blocked = "the path now leads outside the project or through a link"
        else:
            plan.dest = dest
            plan.current = take(dest, store.max_file_bytes)
            if plan.current is None:
                plan.blocked = "something other than a regular file is at this path now"
        if plan.current is not None:
            if plan.current.digest != plan.expected:
                plan.conflicts.insert(0, Conflict(
                    "modified",
                    f"changed on disk since turn {last_turn}'s recorded edit -- by bash, "
                    "another program or by hand",
                ))
            plan.action = _action(plan.target, plan.current)
        plans.append(plan)
    plans.sort(key=lambda p: p.path)
    return plans


def _action(target: str | None, current: Snapshot) -> str:
    if target is None:
        return DELETE if current.exists else NONE
    if not current.exists:
        return CREATE
    return NONE if current.digest == target else RESTORE


def _summary(plans: list[FilePlan], turn: int) -> dict[str, Any]:
    return {
        "turn": turn,
        "conflicts": [p.path for p in plans if p.conflicts and not p.blocked],
        "not_restorable": [{"path": p.path, "reason": p.blocked or p.reason}
                           for p in plans if p.blocked or not p.restorable],
        "untracked": UNTRACKED,
    }


def preview(store: CheckpointStore, turn: int, selected: list[str] | None = None) -> dict:
    """What a rewind to before ``turn`` would do, file by file, with diffs."""
    plans = _plan(store, store.load(), turn, selected)
    files = []
    for p in plans:
        row = p.to_json()
        if p.current is not None and p.restorable and not p.blocked:
            row.update(_diff(store, p, p.current))
        files.append(row)
    return {**_summary(plans, turn), "files": files}


def _diff(store: CheckpointStore, plan: FilePlan, current: Snapshot) -> dict[str, Any]:
    if current.exists and current.data is None:
        return {"diff": "", "added": None, "removed": None, "binary": False,
                "truncated": False, "omitted": "too_large"}
    target = store.read_blob(plan.target) if plan.target is not None else None
    if plan.target is not None and target is None:
        return {"restorable": False, "reason": DAMAGED}
    return unified(plan.path, current.data if current.exists else None, target)


# ---- the act ----


@dataclass
class RewindResult:
    turn: int
    record: RewindRecord | None
    skipped: list[dict[str, Any]]
    plans: list[FilePlan]

    def to_json(self) -> dict[str, Any]:
        files = self.record.files if self.record else []
        return {
            **_summary(self.plans, self.turn),
            "rewind_id": self.record.id if self.record else None,
            "files": [{"path": f.path, "action": f.action, "from_turn": f.from_turn,
                       "backup": f.backup} for f in files],
            "skipped": self.skipped,
        }


def apply(store: CheckpointStore, turn: int, selected: list[str] | None = None, *,
          force: bool = False) -> RewindResult:
    """Rewind to before ``turn``. Raises ``RewindError`` (409) on a conflict
    unless ``force``; writes nothing in that case."""
    with store.transaction() as index:
        plans = _plan(store, index, turn, selected)
        conflicted = [p for p in plans if p.conflicts and not p.blocked and p.restorable]
        if conflicted and not force:
            raise RewindError(
                409, "files changed since their checkpoints; preview the rewind, then "
                     "deselect them or force it",
                conflicts=[p.to_json() for p in conflicted],
            )
        record = RewindRecord(id="", seq=0, time=now(), turn=turn,
                              forced=bool(conflicted))
        skipped: list[dict[str, Any]] = []
        for plan in plans:
            reason = _refusal(plan)
            if reason:
                skipped.append({"path": plan.path, "reason": reason})
                continue
            done = _rewind_one(store, index, plan)
            if isinstance(done, str):
                skipped.append({"path": plan.path, "reason": done})
                continue
            record.files.append(done)
        if record.files:
            index.seq += 1
            record.seq, record.id = index.seq, f"rw{index.seq}"
            for plan in plans:
                if any(f.path == plan.path for f in record.files):
                    for e in plan.entries:
                        e.rewound = record.id
            index.rewinds.append(record)
            store.collect_garbage(index)
    return RewindResult(turn, record if record.files else None, skipped, plans)


def _refusal(plan: FilePlan) -> str:
    if plan.blocked:
        return plan.blocked
    if not plan.restorable:
        return plan.reason or "not restorable"
    return ""


def _rewind_one(store: CheckpointStore, index: Index, plan: FilePlan) -> RewindFile | str:
    """Put one file back. The record of it, or why it was left alone."""
    assert plan.dest is not None and plan.current is not None
    data: bytes | None = None
    if plan.target is not None:
        data = store.read_blob(plan.target)
        if data is None:
            return DAMAGED
    current = plan.current
    done = RewindFile(path=plan.path, action=_DONE[plan.action], from_turn=plan.from_turn,
                      replaced=current.digest, size=current.size, restored=plan.target)
    if plan.action == NONE:
        return done
    if current.exists and current.data is not None and current.digest:
        done.backup = store.keep_if_room(index, current.digest, current.data)
    try:
        if data is None:
            plan.dest.unlink(missing_ok=True)
        else:
            _write(plan.dest, data)
    except OSError as exc:
        return f"could not write: {exc.strerror or exc}"
    return done


def _write(dest: Path, data: bytes) -> None:
    mode = None
    with contextlib.suppress(OSError):
        mode = stat.S_IMODE(os.stat(dest).st_mode)
    dest.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(dest, data)
    if mode is not None:
        with contextlib.suppress(OSError):
            os.chmod(dest, mode)

