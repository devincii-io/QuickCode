"""``bash_output`` and ``bash_kill``: the other half of a background command.

``bash(run_in_background=true)`` returns a job id and nothing else. These two
are how that id becomes output and, when it is time, a stopped process. Both
act only on the conversation's own job table (``ToolCtx.extra["bash_jobs"]``),
so neither can name a process this conversation did not start: the id is a key
into that table, never a pid.

Output goes through the same decoding and cleaning the foreground tool applies
(``bash._clean_output``), so a stray non-UTF-8 byte or a progress bar reads the
same whether the command was waited on or not.
"""

from __future__ import annotations

import asyncio
import re
from typing import ClassVar

from pydantic import BaseModel, Field

from quickcode.context import toon
from quickcode.tools.base import PermissionSpec, Tool, ToolCtx, ToolResult
from quickcode.tools.bash import _cap, _clean_output
from quickcode.tools.bash_jobs import BashJob, BashJobs

MAX_WAIT_S = 120.0
POLL_S = 0.1

_UNAVAILABLE = (
    "Background shell jobs are not available here: this session has no job table "
    "to keep them in."
)


def _jobs(ctx: ToolCtx) -> BashJobs | None:
    return ctx.extra.get("bash_jobs")


def _unknown(jobs: BashJobs, job_id: str) -> ToolResult:
    if jobs.forgotten(job_id):
        return ToolResult(
            f"background job '{job_id}' finished long enough ago that its output has "
            "been let go: only the most recent finished jobs are kept.",
            is_error=True,
        )
    known = ", ".join(j.job_id for j in jobs) or "(none)"
    return ToolResult(
        f"unknown background job '{job_id}'. Jobs in this conversation: {known}",
        is_error=True,
    )


class BashOutputInput(BaseModel):
    bash_id: str | None = Field(
        default=None,
        description=(
            "The job id bash(run_in_background=true) returned, e.g. bash_1. Omit to "
            "list every background job in this conversation."
        ),
    )
    filter: str = Field(
        default="",
        description=(
            "Optional regular expression. Only new lines that match it are returned; "
            "the lines that do not are consumed all the same and will not be shown again."
        ),
    )
    wait_s: float = Field(
        default=0,
        description=(
            "Seconds to wait before reading, instead of polling with sleep. Returns "
            "early when the job exits or, with filter, as soon as a new line matches. "
            f"0 (the default) reads at once; max {int(MAX_WAIT_S)}."
        ),
    )


class BashOutputTool(Tool[BashOutputInput]):
    name: ClassVar[str] = "bash_output"
    description: ClassVar[str] = (
        "Read what a background shell job (bash with run_in_background=true) has "
        "written since you last read it, and whether it is still running or what "
        "it exited with. Each call returns only new output. Pass wait_s to wait "
        "for it to finish, or with filter for a matching line such as a server's "
        "ready message. Omit bash_id to list this conversation's jobs."
    )
    # It reads a buffer this conversation already holds: no filesystem, no
    # process started, nothing to prompt about. Interruptible because a wait
    # is exactly what Stop should be able to cut short.
    is_read_only: ClassVar[bool] = True
    interruptible: ClassVar[bool] = True
    permission = PermissionSpec(mutates=False, target_field="bash_id")
    Input = BashOutputInput

    def render_call(self, input: BashOutputInput) -> str:  # noqa: A002
        return f"⏺ bash_output[{input.bash_id}]" if input.bash_id else "⏺ bash_output"

    async def run(self, input: BashOutputInput, ctx: ToolCtx) -> ToolResult:  # noqa: A002
        jobs = _jobs(ctx)
        if jobs is None:
            return ToolResult(_UNAVAILABLE, is_error=True)
        if not input.bash_id:
            return _listing(jobs)
        job = jobs.get(input.bash_id)
        if job is None:
            return _unknown(jobs, input.bash_id)
        try:
            pattern = re.compile(input.filter) if input.filter else None
        except re.error as exc:
            return ToolResult(f"invalid filter regex {input.filter!r}: {exc}", is_error=True)

        wait = max(0.0, min(float(input.wait_s or 0), MAX_WAIT_S))
        if wait and job.running:
            await _wait(job, pattern, wait)

        # Before the take: a job seen to have ended gives up everything it wrote,
        # so an ending is only ever reported next to all of its output.
        ended = not job.running
        state = job.outcome()
        data, dropped = job.take()
        text = _clean_output(data)
        notes: list[str] = []
        if dropped:
            notes.append(
                f"({dropped} bytes were dropped before this read: the job wrote more "
                f"than the {job.buffer_bytes // 1024} KB it keeps between reads.)"
            )
        empty = "(no new output)"
        if pattern is not None and text:
            lines = text.splitlines()
            kept = [line for line in lines if pattern.search(line)]
            notes.append(f"(filter kept {len(kept)} of {len(lines)} new lines)")
            text = "\n".join(kept)
            empty = "(no new line matched the filter)"
        if ended:
            job.exit_seen = True

        head = f"{job.job_id} {state}."
        body = _cap(text) if text else empty
        return ToolResult("\n".join([head, *notes, body]))


async def _wait(job: BashJob, pattern: re.Pattern | None, wait: float) -> None:
    """Until the job ends, a new line matches, or ``wait`` runs out."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait
    seen = -1
    while job.running and loop.time() < deadline:
        if pattern is not None:
            size = job.unread_bytes()
            if size != seen:
                seen = size
                if any(pattern.search(line)
                       for line in _clean_output(job.peek()).splitlines()):
                    return
        await asyncio.sleep(POLL_S)


def _listing(jobs: BashJobs) -> ToolResult:
    rows = [job.to_row() for job in jobs]
    if not rows:
        return ToolResult("No background shell jobs have been started in this conversation.")
    return ToolResult(toon.fenced({
        "running": sum(1 for r in rows if r["status"] == "running"),
        "bash_jobs": rows,
    }))


class BashKillInput(BaseModel):
    bash_id: str = Field(
        description="The id of a background job this conversation started, e.g. bash_1."
    )


class BashKillTool(Tool[BashKillInput]):
    name: ClassVar[str] = "bash_kill"
    description: ClassVar[str] = (
        "Stop a background shell job started with bash(run_in_background=true), "
        "killing its whole process tree. Whatever it wrote before it died stays "
        "readable with bash_output. Takes a job id, never a pid, so only jobs this "
        "conversation started can be stopped."
    )
    # Not read-only: it ends a process, so it runs on its own rather than
    # alongside the reads in a round. Not prompted either: the command was
    # approved when it started, and stopping it can only take away what that
    # approval allowed -- the id is a key into this conversation's own table,
    # so nothing else on the machine is reachable through it.
    is_read_only: ClassVar[bool] = False
    permission = PermissionSpec(mutates=False, target_field="bash_id")
    Input = BashKillInput

    def render_call(self, input: BashKillInput) -> str:  # noqa: A002
        return f"⏺ bash_kill[{input.bash_id}]"

    async def run(self, input: BashKillInput, ctx: ToolCtx) -> ToolResult:  # noqa: A002
        jobs = _jobs(ctx)
        if jobs is None:
            return ToolResult(_UNAVAILABLE, is_error=True)
        job = jobs.get(input.bash_id)
        if job is None:
            return _unknown(jobs, input.bash_id)
        killed = await asyncio.to_thread(job.kill)
        job.exit_seen = True
        if not killed:
            return ToolResult(f"{job.job_id} {job.outcome()}; there was nothing to stop.")
        unread = job.unread_bytes()
        tail = (
            f" {unread} bytes of its output are still unread; bash_output reads them."
            if unread else ""
        )
        if job.running:
            return ToolResult(
                f"Sent a kill to {job.job_id}'s process tree, but it has not exited yet.{tail}",
                is_error=True,
            )
        return ToolResult(f"Killed {job.job_id} ({job.label}) after {job.seconds()}s.{tail}")
