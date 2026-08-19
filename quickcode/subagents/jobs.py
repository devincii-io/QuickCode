"""Detached subagent jobs: one record per background spawn.

A blocking delegation needs no bookkeeping -- the tool call *is* the job, and
its result is the report. A detached one has to survive the tool call that
started it, so there is something to name in a later ``agent_status`` /
``agent_result`` call, and something for the conversation to cancel when the
user interrupts.

That is all a ``JobRecord`` is: an id, what it was asked to do, where it got
to, and the ``asyncio.Task`` still doing it. The report it eventually holds has
already been through ``sanitize_report`` and ``maybe_offload`` -- a detached
job runs the identical finishing path a blocking one does, so what a collector
reads is what the blocking tool would have returned.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from quickcode.context import toon

RUNNING = "running"
DONE = "done"
ERROR = "error"
CANCELLED = "cancelled"

# Everything that is not ``running``. A terminal job never becomes live again:
# resuming one is ``send_message``, which starts a fresh turn on the same child.
TERMINAL = (DONE, ERROR, CANCELLED)




@dataclass
class JobRecord:
    """One detached subagent run, from spawn to collection."""

    agent_id: str
    agent_type: str
    description: str = ""
    status: str = RUNNING
    started_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    # The sanitized final report, once there is one.
    report: str = ""
    # Whether the spawner has read it. Tracked so the turn-end check can tell
    # "finished and read" from "finished and still sitting here".
    collected: bool = False
    # Held so the conversation that owns this job can cancel it. Never awaited
    # by the registry itself -- ``agent_result`` waits on it, ``close()``
    # cancels it, and nothing else touches it.
    task: asyncio.Task | None = field(default=None, repr=False)

    @property
    def running(self) -> bool:
        return self.status == RUNNING

    def seconds(self) -> float:
        end = time.monotonic() if self.finished_at is None else self.finished_at
        return round(end - self.started_at, 1)

    def finish(self, status: str, report: str) -> None:
        """Move to a terminal state once. Later calls are ignored, so a task
        that is cancelled while already finishing cannot rewrite the answer."""
        if self.status != RUNNING:
            return
        self.status = status
        self.report = report
        self.finished_at = time.monotonic()

    def to_row(self) -> dict[str, object]:
        """One job as a record, for the TOON table every reporting tool emits.

        Every field is present on every row, including ``collected`` on a job
        that is still running: a table needs its rows to agree, and "not
        collected yet" is a true answer for a job nobody could have collected.
        The XML attributes this replaces carried their own hand-rolled
        escaping, which is exactly the thing an encoder is for.
        """
        return {
            "id": self.agent_id,
            "type": self.agent_type,
            "status": self.status,
            "seconds": self.seconds(),
            "collected": self.collected,
            "description": self.description,
        }

    def to_toon(self) -> str:
        """This job alone, in the same shape a listing of many uses."""
        return toon.fenced({"agent_jobs": [self.to_row()]})

    def reminder(self) -> str:
        """What the spawner is told, once, on the turn after this job ends."""
        if self.status == DONE:
            return (
                f"Background subagent job {self.agent_id} finished. Call "
                f'agent_result with agent_id="{self.agent_id}" to read its report.'
            )
        return (
            f"Background subagent job {self.agent_id} ended with status "
            f'"{self.status}". Call agent_result with agent_id="{self.agent_id}" '
            "to see what it managed before it stopped."
        )
