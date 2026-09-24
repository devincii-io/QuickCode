"""Task board: solo checklist + future teammate coordination backbone.

One system for both solo todo tracking and multi-agent coordination (see
docs/AGENTS.md §3). A ``TaskBoard`` holds a flat set of ``Task`` records with
id-based dependency edges (``blocked_by`` / ``blocks``), persisted as JSON so
state survives restarts and compaction.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from quickcode.context import toon
from quickcode.workspace import ensure_project_dir_for

log = logging.getLogger("quickcode.core.tasks")

STATUSES = ("pending", "in_progress", "completed", "deleted")
DESCRIPTION_CHARS = 200


def _id_number(task_id: str) -> int:
    try:
        return int(task_id[1:])
    except ValueError:
        return 0


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


@dataclass
class Task:
    id: str
    subject: str
    description: str = ""
    active_form: str = ""
    status: str = "pending"
    owner: str | None = None
    blocked_by: list[str] = field(default_factory=list)
    blocks: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "subject": self.subject,
            "description": self.description,
            "active_form": self.active_form,
            "status": self.status,
            "owner": self.owner,
            "blocked_by": list(self.blocked_by),
            "blocks": list(self.blocks),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Task:
        return cls(
            id=data["id"],
            subject=data["subject"],
            description=data.get("description", ""),
            active_form=data.get("active_form", ""),
            status=data.get("status", "pending"),
            owner=data.get("owner"),
            blocked_by=list(data.get("blocked_by", [])),
            blocks=list(data.get("blocks", [])),
        )


class TaskBoard:
    """A persistent set of tasks, optionally bound to a JSON file on disk."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self.tasks: dict[str, Task] = {}
        self._counter = 0

    # --- id assignment -----------------------------------------------
    def _next_id(self) -> str:
        self._counter += 1
        return f"T{self._counter}"

    # --- CRUD ----------------------------------------------------------
    def create(self, subject: str, description: str = "", active_form: str = "") -> Task:
        task_id = self._next_id()
        task = Task(
            id=task_id,
            subject=subject,
            description=description,
            active_form=active_form,
            status="pending",
        )
        self.tasks[task_id] = task
        self.save()
        return task

    def get(self, task_id: str) -> Task:
        if task_id not in self.tasks:
            raise KeyError(f"unknown task id: {task_id}")
        return self.tasks[task_id]

    def list(self, include_deleted: bool = False) -> list[Task]:
        ids = sorted(self.tasks.keys(), key=_id_number)
        tasks = [self.tasks[i] for i in ids]
        if not include_deleted:
            tasks = [t for t in tasks if t.status != "deleted"]
        return tasks

    def update(
        self,
        task_id: str,
        *,
        status: str | None = None,
        owner: str | None = None,
        add_blocked_by: list[str] | None = None,
        add_blocks: list[str] | None = None,
    ) -> Task:
        """Apply one update, or none of it.

        Everything is checked before anything changes. The model is told a
        refused call failed, and it used to be half-applied anyway: the edges
        added before a bad id stayed, and the next save persisted them.
        """
        task = self.get(task_id)
        # (blocker, blocked) pairs: the blocker has to complete first.
        edges = [(other, task_id) for other in add_blocked_by or () if other != task_id]
        edges += [(task_id, other) for other in add_blocks or () if other != task_id]
        for blocker, blocked in edges:
            self.get(blocked if blocker == task_id else blocker)
        for blocker, blocked in edges:
            # There is no call that removes an edge, so a cycle is permanent.
            if self._reaches(blocked, blocker, edges):
                raise ValueError(
                    f"{blocker} blocking {blocked} would make a dependency cycle"
                )

        if status is not None:
            if status not in STATUSES:
                raise ValueError(
                    f"invalid status {status!r}; must be one of {', '.join(STATUSES)}"
                )
            if status == "in_progress":
                blockers = dict.fromkeys(
                    [*task.blocked_by, *(b for b, d in edges if d == task_id)]
                )
                incomplete = [
                    b for b in blockers if self.tasks.get(b, None) is None
                    or self.tasks[b].status != "completed"
                ]
                if incomplete:
                    raise ValueError(
                        f"{task_id} is blocked by incomplete "
                        f"{', '.join(incomplete)}; complete them first"
                    )

        for blocker, blocked in edges:
            if blocked not in self.tasks[blocker].blocks:
                self.tasks[blocker].blocks.append(blocked)
            if blocker not in self.tasks[blocked].blocked_by:
                self.tasks[blocked].blocked_by.append(blocker)
        if status is not None:
            task.status = status
        if owner is not None:
            task.owner = owner

        self.save()
        return task

    def _reaches(self, start: str, goal: str, extra: list[tuple[str, str]]) -> bool:
        """Whether ``goal`` has to wait on ``start``, counting ``extra`` edges."""
        seen: set[str] = set()
        stack = [start]
        while stack:
            node = stack.pop()
            if node == goal:
                return True
            if node in seen:
                continue
            seen.add(node)
            known = self.tasks.get(node)
            stack.extend(known.blocks if known else ())
            stack.extend(d for b, d in extra if b == node)
        return False

    def claimable(self) -> list[Task]:
        result = []
        for task in self.list():
            if task.status != "pending" or task.owner:
                continue
            if all(self.tasks.get(b) and self.tasks[b].status == "completed" for b in task.blocked_by):
                result.append(task)
        return result

    # --- persistence -----------------------------------------------------
    def save(self) -> None:
        if self.path is None:
            return
        # A board carries the subjects the user asked for, so it is one of the
        # things ``.quickcode/.gitignore`` exists to cover -- and a board can
        # in principle be the first thing written into a fresh project.
        ensure_project_dir_for(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "counter": self._counter,
            "tasks": [t.to_dict() for t in self.list(include_deleted=True)],
        }
        # Written beside and swapped in, so a process that dies mid-write
        # leaves the previous board rather than half of this one.
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    @classmethod
    def load(cls, path: Path) -> TaskBoard:
        path = Path(path)
        board = cls(path=path)
        if not path.exists():
            return board
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            tasks = [Task.from_dict(t) for t in data.get("tasks", [])]
            counter = int(data.get("counter", 0))
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            # The board is opened with its conversation, and an unreadable one
            # used to stop the conversation opening at all. It is set aside
            # instead of being overwritten by the next save.
            log.warning("task board %s is unreadable (%s); starting empty", path, exc)
            with contextlib.suppress(OSError):
                os.replace(path, path.with_name(path.name + ".corrupt"))
            return board
        for task in tasks:
            board.tasks[task.id] = task
        # Never below an id already on the board, or the next create reuses it
        # and silently replaces that task.
        board._counter = max([counter, *(_id_number(t) for t in board.tasks)])
        return board

    # --- rendering -----------------------------------------------------
    def render_table(self) -> str:
        """The board as the model reads it: one TOON row per task.

        The markdown checklist this replaces could only carry a mark, an id
        and a subject, so ``owner``, ``blocks`` and ``description`` were
        silently dropped -- a coordination board whose rendering hid who had
        claimed what. A table has columns for them.

        ``description`` is clipped rather than dropped: the whole point is
        that it stops vanishing, but ``task_list`` runs often enough that a
        paragraph per row would be paid for on every call. ``task_get``
        returns the full text.
        """
        tasks = self.list()
        if not tasks:
            return "(no tasks)"
        rows = [
            {
                "id": task.id,
                "status": task.status,
                "subject": task.subject,
                "owner": task.owner or "",
                # Space-joined, not comma-joined: a list has nowhere to go in a
                # flat row, and a comma would only force the cell to be quoted.
                "blocked_by": " ".join(self._open_blockers(task)),
                "blocks": " ".join(task.blocks),
                "description": _clip(task.description, DESCRIPTION_CHARS),
            }
            for task in tasks
        ]
        return toon.fenced({"tasks": rows})

    def _open_blockers(self, task: Task) -> list[str]:
        """Blockers that are not completed yet -- the only ones that still stop
        this task from starting."""
        return [
            b for b in task.blocked_by
            if not (self.tasks.get(b) and self.tasks[b].status == "completed")
        ]
