"""The Checkpointer: which turn a conversation is on, and what each call changed.

A turn is numbered the way the session log numbers it (``TranscriptRecorder``
counts ``user_message`` events), so "before turn 4" in the checkpoint API is
the turn the transcript shows as 4. Both drivers log the user's message before
the turn starts, so the first turn after a (re)open reads its number off the
log and every later one counts on from there.

A call is recorded in two halves around the tool: ``capture`` reads what its
target files hold before it runs, ``commit`` compares afterwards and saves the
first half only for files that actually changed.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from quickcode.checkpoints import paths
from quickcode.checkpoints.snapshot import Snapshot, take
from quickcode.checkpoints.store import CheckpointStore, FileEntry
from quickcode.session.store import SessionStore

# How many files a rewind reminder names before it says "and N more".
_NOTICE_FILES = 20


@dataclass(frozen=True)
class Capture:
    rel: str
    path: Path
    before: Snapshot


class Checkpointer:
    """One conversation's checkpointing, shared by its agent and subagents."""

    def __init__(self, root: Path | str, conv_id: str,
                 store: CheckpointStore | None = None) -> None:
        self.root = Path(root)
        self.real_root = paths.real_root(root)
        self.conv_id = conv_id
        self.store = store or CheckpointStore(root, conv_id)
        self.turn = 0
        self._synced = False

    def begin_turn(self) -> int:
        if self._synced:
            self.turn += 1
        else:
            self._synced = True
            logged = SessionStore(self.root, self.conv_id).last_turn()
            self.turn = max(self.turn + 1, logged)
        return self.turn

    def capture(self, targets: Iterable[Path]) -> list[Capture]:
        """What each target inside the project holds now. Others are skipped."""
        out: list[Capture] = []
        seen: set[str] = set()
        for path in targets:
            rel = paths.relative(self.real_root, path)
            if rel is None or paths.fold(rel) in seen:
                continue
            seen.add(paths.fold(rel))
            real = self.real_root / rel
            before = take(real, self.store.max_file_bytes)
            if before is not None:
                out.append(Capture(rel, real, before))
        return out

    def commit(self, captures: Iterable[Capture], *, call_id: str = "", tool: str = "",
               agent: str = "") -> tuple[int, list[FileEntry]]:
        """Record the captured files that changed. Returns the turn they were
        recorded in and the entries that are new -- the first change that turn
        made to each of those files."""
        turn = self.turn or self.begin_turn()
        created: list[FileEntry] = []
        for cap in captures:
            after = take(cap.path, self.store.max_file_bytes)
            if after is None or (after.exists, after.digest) == (cap.before.exists,
                                                                 cap.before.digest):
                continue
            entry, new = self.store.record(turn, cap.rel, cap.before, after,
                                           call_id=call_id, tool=tool, agent=agent)
            if new:
                created.append(entry)
        return turn, created

    def rewind_notice(self) -> str:
        """A reminder for the model about rewinds since its last turn, or ""."""
        lines: list[str] = []
        for record in self.store.take_unannounced():
            changed = [f.path for f in record.files if f.action != "unchanged"]
            if not changed:
                continue
            more = len(changed) - _NOTICE_FILES
            named = ", ".join(changed[:_NOTICE_FILES]) + (f" and {more} more" if more > 0 else "")
            lines.append(f"- back to before turn {record.turn}: {named}")
        if not lines:
            return ""
        return (
            "The user rewound files to earlier checkpoints since your last turn, "
            "undoing the edits made to them from that turn on:\n" + "\n".join(lines)
            + "\nThose files no longer hold what you wrote. Read one again before "
            "editing it."
        )
