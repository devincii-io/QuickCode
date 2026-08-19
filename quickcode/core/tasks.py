"""Task board: solo checklist + future teammate coordination backbone.

One system for both solo todo tracking and multi-agent coordination (see
docs/AGENTS.md §3). A ``TaskBoard`` holds a flat set of ``Task`` records with
id-based dependency edges (``blocked_by`` / ``blocks``), persisted as JSON so
state survives restarts and compaction.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from quickcode.context import toon
from quickcode.workspace import ensure_project_dir_for

STATUSES = ("pending", "in_progress", "completed", "deleted")
DESCRIPTION_CHARS = 200


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
        def key(task_id: str) -> int:
            try:
                return int(task_id[1:])
            except ValueError:
                return 0

        ids = sorted(self.tasks.keys(), key=key)
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
        task = self.get(task_id)

        if add_blocked_by:
            for other_id in add_blocked_by:
                if other_id == task_id:
                    continue
                # will raise KeyError if unknown
                other = self.get(other_id)
                if other_id not in task.blocked_by:
                    task.blocked_by.append(other_id)
                if task_id not in other.blocks:
                    other.blocks.append(task_id)

        if add_blocks:
            for other_id in add_blocks:
                if other_id == task_id:
                    continue
                other = self.get(other_id)
                if other_id not in task.blocks:
                    task.blocks.append(other_id)
                if task_id not in other.blocked_by:
                    other.blocked_by.append(task_id)

        if status is not None:
            if status not in STATUSES:
                raise ValueError(
                    f"invalid status {status!r}; must be one of {', '.join(STATUSES)}"
                )
            if status == "in_progress":
                incomplete = [
                    b for b in task.blocked_by if self.tasks.get(b, None) is None
                    or self.tasks[b].status != "completed"
                ]
                if incomplete:
                    raise ValueError(
                        f"{task_id} is blocked by incomplete "
                        f"{', '.join(incomplete)}; complete them first"
                    )
            task.status = status

        if owner is not None:
            task.owner = owner

        self.save()
        return task

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
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> TaskBoard:
        path = Path(path)
        board = cls(path=path)
        if not path.exists():
            return board
        data = json.loads(path.read_text(encoding="utf-8"))
        board._counter = data.get("counter", 0)
        for task_data in data.get("tasks", []):
            task = Task.from_dict(task_data)
            board.tasks[task.id] = task
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
