"""The Jobs tab's routes: one conversation's background shell jobs.

::

    GET  .../conversations/{conv_id}/jobs                       every job the table holds
    GET  .../conversations/{conv_id}/jobs/{job_id}/output       ?since=<offset>&limit=<bytes>
    POST .../conversations/{conv_id}/jobs/{job_id}/kill         the whole process tree

Each is mounted in both path shapes (``http.scoped``). Only an open
conversation has a job table -- its processes die when it closes -- so a
conversation id that is not open is a 404 here, never a reason to open one.

The output route is a viewer's read of the job's ring (``BashJob.tail``): it
takes absolute byte offsets and leaves the model's cursor alone, so the panel
watching a server scroll by does not turn what ``bash_output`` would report as
new into old news. It is decoded by the decoder ``bash_output`` uses, with the
same care for a character split across two reads, but not cleaned: escape codes
and carriage returns stay in for the panel's terminal renderer, which applies a
redraw that straddles two reads correctly where a per-read strip could not.

A kill here is the user's, recorded as such: ``bash_job_done`` carries
``killed_by: "user"``, the transcript gets a note, and the model hears about it
at the top of its next turn the way it hears about any ending it did not see.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from fastapi import FastAPI, HTTPException

from quickcode.server.http import scoped, valid_conv_id
from quickcode.server.manager import ConversationManager
from quickcode.tools.base import decode_output
from quickcode.tools.bash_jobs import USER, BashJob, BashJobs

JOB_ID_RE = re.compile(r"bash_[1-9][0-9]{0,8}\Z")
DEFAULT_TAIL_BYTES = 64 * 1024
MAX_TAIL_BYTES = 256 * 1024
# The full command is already in the tool call; a row carries enough of it to
# read and to copy, not a heredoc the size of the ring.
COMMAND_CHARS = 16 * 1024


def _table(manager: ConversationManager, conv_id: str) -> BashJobs | None:
    if not valid_conv_id(conv_id):
        raise HTTPException(400, "invalid conversation id")
    conv = manager.get(conv_id)
    if conv is None:
        raise HTTPException(404, f"no open conversation {conv_id!r}")
    return conv.bash_jobs()


def _job(manager: ConversationManager, conv_id: str, job_id: str) -> BashJob:
    table = _table(manager, conv_id)
    if not JOB_ID_RE.fullmatch(job_id):
        raise HTTPException(400, "invalid job id")
    job = table.get(job_id) if table is not None else None
    if job is None:
        if table is not None and table.forgotten(job_id):
            raise HTTPException(410, f"{job_id} finished long enough ago that it was let go")
        raise HTTPException(404, f"no background job {job_id!r} in this conversation")
    return job


def job_row(job: BashJob) -> dict[str, Any]:
    command = job.command
    return {
        "id": job.job_id,
        "command": command[:COMMAND_CHARS],
        "command_truncated": len(command) > COMMAND_CHARS,
        "description": job.description,
        "label": job.label,
        "status": job.status,
        "exit_code": job.exit_code,
        "killed_by": job.killed_by,
        "started": job.started_ts,
        "ended": job.ended_ts,
        "seconds": job.seconds(),
        "bytes": job.written(),
        "dropped": job.dropped(),
        # What bash_output would call new: the panel says when the model has
        # not looked, but never changes it.
        "unread": job.unread_bytes(),
    }


async def list_jobs(manager: ConversationManager, conv_id: str) -> dict[str, Any]:
    table = _table(manager, conv_id)
    rows = [job_row(job) for job in table] if table is not None else []
    return {
        "jobs": rows,
        "running": sum(1 for r in rows if r["status"] == "running"),
        "max_running": table.max_running if table is not None else 0,
    }


async def job_output(
    manager: ConversationManager, conv_id: str, job_id: str,
    since: int = 0, limit: int = DEFAULT_TAIL_BYTES,
) -> dict[str, Any]:
    if since < 0:
        raise HTTPException(400, "since must be 0 or more")
    if not 0 < limit <= MAX_TAIL_BYTES:
        raise HTTPException(400, f"limit must be between 1 and {MAX_TAIL_BYTES}")
    job = _job(manager, conv_id, job_id)
    # The state before the bytes, as bash_output reads it: an ending reported
    # here always comes with everything the job wrote before it.
    status, exit_code = job.status, job.exit_code
    tail = job.tail(since, limit)
    return {
        "id": job.job_id,
        "status": status,
        "exit_code": exit_code,
        "text": decode_output(tail.data),
        "start": tail.start,
        "next": tail.next,
        "end": tail.end,
        "gap": tail.gap,
        "dropped": job.dropped(),
        "unread": job.unread_bytes(),
    }


async def kill_job(manager: ConversationManager, conv_id: str, job_id: str) -> dict[str, Any]:
    job = _job(manager, conv_id, job_id)
    # Waited for, as bash_kill waits, so the answer says how it ended; off the
    # loop, because the wait is a thread's.
    killed = await asyncio.to_thread(job.kill, by=USER)
    return {"killed": killed, "job": job_row(job)}


def register_job_routes(app: FastAPI, hub: Any) -> None:
    scoped(app, hub, "GET", "/conversations/{conv_id}/jobs", list_jobs)
    scoped(app, hub, "GET", "/conversations/{conv_id}/jobs/{job_id}/output", job_output)
    scoped(app, hub, "POST", "/conversations/{conv_id}/jobs/{job_id}/kill", kill_job)
