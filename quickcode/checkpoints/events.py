"""The two records checkpoints add to the session log.

New event types rather than new fields on old ones: the log's schema is locked
and widens only by addition. Neither carries file contents -- a path, a turn,
the call that changed it -- because the log is a transcript, and the bytes live
in the checkpoint store, which a rewind reads and the log never needs to.

``checkpoint``     a turn saved a file's prior contents, before its first change
``files_rewound``  the user put files back to how they were before a turn
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, ClassVar

from quickcode.session.wire import register_event

# A rewind of a whole large refactor names every file in the index; the log
# needs enough to say what happened, not a second copy of that list.
MAX_LOGGED_FILES = 200


@dataclass
class FileCheckpointed:
    wire_type: ClassVar[str] = "checkpoint"

    turn: int
    path: str
    call_id: str = ""
    tool: str = ""
    # The file did not exist before the turn: rewinding the turn deletes it.
    created: bool = False
    restorable: bool = True
    reason: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"type": self.wire_type, **asdict(self)}


@dataclass
class FilesRewound:
    wire_type: ClassVar[str] = "files_rewound"

    rewind_id: str
    to_turn: int
    # [{path, action, from_turn}] -- action: restored | deleted | created | unchanged
    files: list[dict[str, Any]] = field(default_factory=list)
    # [{path, reason}] for files the rewind was asked for and left alone.
    skipped: list[dict[str, Any]] = field(default_factory=list)
    forced: bool = False

    def to_json(self) -> dict[str, Any]:
        out = asdict(self)
        total = len(self.files)
        out["files"] = self.files[:MAX_LOGGED_FILES]
        out["skipped"] = self.skipped[:MAX_LOGGED_FILES]
        out["file_count"] = total
        return {"type": self.wire_type, **out}


register_event(FileCheckpointed, FileCheckpointed.to_json, logged=True)
register_event(FilesRewound, FilesRewound.to_json, logged=True)
