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

    def _resolve(self, task_id: str) -> str:
        """The board's own spelling of an id. A model writes ``t3`` or ``T3 ``
        as readily as ``T3``, and "unknown task id" for either sent it hunting
        for a task that was right there."""
        key = (task_id or "").strip()
        for candidate in (key, key.upper()):
            if candidate in self.tasks:
                return candidate
        raise KeyError(f"unknown task id: {task_id}")

    def get(self, task_id: str) -> Task:
        return self.tasks[self._resolve(task_id)]

    def _blocking(self, blocker_id: str) -> bool:
        """Whether this blocker still stops a dependent from starting.

        A deleted blocker does not: deleting is how work gets dropped, and
        there is no way to remove an edge, so a dependent of a deleted task
        used to stay unstartable and unclaimable for good.
        """
        blocker = self.tasks.get(blocker_id)
        return blocker is not None and blocker.status not in ("completed", "deleted")

    def _cycle_through(self, edges: list[tuple[str, str]]) -> list[str]:
        """A dependency cycle the new ``(blocker, blocked)`` edges would close,
        as a path of ids, or ``[]``. Every task on a cycle waits for itself."""
        graph: dict[str, list[str]] = {tid: list(t.blocks) for tid, t in self.tasks.items()}
        for blocker, blocked in edges:
            graph.setdefault(blocker, []).append(blocked)
        for blocker, blocked in edges:
            path = self._path(graph, blocked, blocker)
            if path:
                return [blocker, *path]
        return []

    @staticmethod
    def _path(graph: dict[str, list[str]], start: str, goal: str) -> list[str]:
        stack = [(start, [start])]
        seen: set[str] = set()
        while stack:
            node, path = stack.pop()
            if node == goal:
                return path
            if node in seen:
                continue
            seen.add(node)
            stack.extend((nxt, [*path, nxt]) for nxt in graph.get(node, ()))
        return []

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
        """Apply every change or none of them.

        Everything is checked before anything is changed. An unknown id in the
        middle of a list, or a status the new edges forbid, used to raise
        after the edges before it had already been linked -- a half-applied
        update reported as a failed one.
        """
        task = self.get(task_id)
        task_id = task.id
        blockers = [self._resolve(o) for o in add_blocked_by or ()]
        blocked = [self._resolve(o) for o in add_blocks or ()]
        if status is not None and status not in STATUSES:
            raise ValueError(f"invalid status {status!r}; must be one of {', '.join(STATUSES)}")

        edges = [(o, task_id) for o in blockers if o != task_id]
        edges += [(task_id, o) for o in blocked if o != task_id]
        cycle = self._cycle_through(edges)
        if cycle:
            raise ValueError(
                f"that would make a dependency cycle ({' blocks '.join(cycle)}); a task "
                "on a cycle waits for itself and can never start"
            )
        if status == "in_progress":
            waiting_on = dict.fromkeys([*task.blocked_by, *(b for b, _ in edges if b != task_id)])
            incomplete = [b for b in waiting_on if self._blocking(b)]
            if incomplete:
                raise ValueError(
                    f"{task_id} is blocked by incomplete "
                    f"{', '.join(incomplete)}; complete them first"
                )

        for blocker_id, blocked_id in edges:
            blocker, dependent = self.tasks[blocker_id], self.tasks[blocked_id]
            if blocked_id not in blocker.blocks:
                blocker.blocks.append(blocked_id)
            if blocker_id not in dependent.blocked_by:
                dependent.blocked_by.append(blocker_id)
        if status is not None:
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
            if not any(self._blocking(b) for b in task.blocked_by):
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
        """Blockers that still stop this task from starting: not completed,
        and not deleted."""
        return [b for b in task.blocked_by if self._blocking(b)]
