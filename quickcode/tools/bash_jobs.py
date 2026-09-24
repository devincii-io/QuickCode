"""Background shell jobs: commands that outlive the tool call that started them.

``bash(run_in_background=true)`` hands its command here instead of waiting on
it; ``bash_output`` and ``bash_kill`` (``tools/bash_job_tools.py``) are how the
model reads and stops it afterwards.

A job runs on plain pipes with stdin on the null device, on every platform. The
foreground tool gives the reason for Windows (``bash._use_pty``): a program
that stops to read stdin under a tty waits for a person who is not there. A
detached one would wait for ever, not merely until a timeout, so here it gets
EOF and gets on with it. ``PYTHONUNBUFFERED`` defaults on because a Python
program block-buffers into a pipe, and a dev server whose "listening on" line
sits in a 8 KB buffer looks to the model like one that never started.

Two daemon threads per job: a reader that moves bytes from the pipe into a
bounded ring, and a watcher that waits on the shell and marks the job finished
once the pipe has closed behind it. Bytes stay bytes until a read decodes them,
which is where the foreground tool's encoding handling applies.

One ``BashJobs`` per conversation, carried in ``ToolCtx.extra["bash_jobs"]``
and shared down the agent tree the way the subagent job table is. It caps how
many jobs run at once and kills every process tree it still holds when the
conversation closes.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any

from quickcode import subproc
from quickcode.pty.session import IS_WINDOWS, _kill_tree

if TYPE_CHECKING:
    from subprocess import Popen

RUNNING = "running"
EXITED = "exited"
KILLED = "killed"

MAX_RUNNING = 8
BUFFER_BYTES = 1024 * 1024
# Finished jobs kept for reading. Older ones are forgotten so a long session's
# memory is bounded by this times BUFFER_BYTES, not by how many it has run.
MAX_RETAINED = 32
READ_SIZE = 65536
POLL_S = 0.05
# After a kill, how long the pipe may stay open before the job is called
# killed anyway (see ``BashJob._watch``).
KILL_GRACE_S = 2.0
KILL_WAIT_S = 5.0
COMMAND_LOG_CHARS = 200

OnEvent = Callable[[dict[str, Any]], None]


class JobLimitReached(RuntimeError):
    """Refused rather than queued: a queued shell job is a hidden one."""


class BashJob:
    """One detached command: its process, its output ring and where it got to."""

    def __init__(self, job_id: str, command: str, description: str,
                 proc: Popen, buffer_bytes: int) -> None:
        self.job_id = job_id
        self.command = command
        self.description = description
        self.pid = proc.pid
        self.started_at = time.monotonic()
        self.finished_at: float | None = None
        self.status = RUNNING
        self.exit_code: int | None = None
        # The model has been shown the final status (by bash_output or
        # bash_kill), so an exit reminder would be old news.
        self.exit_seen = False
        # An exit reminder has already been queued for it.
        self.noticed = False
        self.buffer_bytes = buffer_bytes
        self.finished = threading.Event()
        self._proc = proc
        self._buf = bytearray()
        self._base = 0  # absolute offset of _buf[0]; grows as the ring drops
        self._cursor = 0  # absolute offset of the next byte the model has not read
        self._lock = threading.Lock()
        self._kill_requested = False
        self._kill_deadline: float | None = None
        self._reader_done = threading.Event()

    @property
    def running(self) -> bool:
        return self.status == RUNNING

    @property
    def label(self) -> str:
        first = self.command.strip().splitlines()[0] if self.command.strip() else ""
        return self.description.strip() or first[:80]

    def seconds(self) -> float:
        end = time.monotonic() if self.finished_at is None else self.finished_at
        return round(end - self.started_at, 1)

    def outcome(self) -> str:
        if self.status == RUNNING and self.exit_code is not None:
            return (
                f"is still running ({self.seconds()}s): its shell exited with code "
                f"{self.exit_code}, but something it started still holds its output open"
            )
        if self.status == RUNNING:
            return f"is still running ({self.seconds()}s)"
        if self.status == KILLED:
            return f"was killed after {self.seconds()}s"
        return f"exited with code {self.exit_code} after {self.seconds()}s"

    # ---- output ring ----
    def unread_bytes(self) -> int:
        with self._lock:
            return self._base + len(self._buf) - max(self._cursor, self._base)

    def peek(self) -> bytes:
        """The unread bytes, left unread."""
        with self._lock:
            return bytes(self._buf[max(self._cursor, self._base) - self._base:])

    def take(self) -> tuple[bytes, int]:
        """The unread bytes, now read, and how many the ring dropped first.

        While the job runs, a UTF-8 sequence cut in half by the end of what has
        arrived stays unread until its other half does. Decoding the first half
        on its own would fail, and ``decode_output`` would then read the whole
        chunk in the system code page -- one split character turning a page of
        good output into mojibake.
        """
        with self._lock:
            dropped = max(0, self._base - self._cursor)
            start = max(self._cursor, self._base)
            data = bytes(self._buf[start - self._base:])
            if self.running:
                data = data[:_utf8_boundary(data)]
            self._cursor = start + len(data)
        if dropped:
            data = _skip_continuation(data)
        return data, dropped

    def _append(self, data: bytes) -> None:
        with self._lock:
            self._buf += data
            over = len(self._buf) - self.buffer_bytes
            if over > 0:
                del self._buf[:over]
                self._base += over

    # ---- lifecycle ----
    def _begin(self, on_finished: Callable[[BashJob], None]) -> None:
        threading.Thread(target=self._read, name=f"{self.job_id}-reader", daemon=True).start()
        threading.Thread(
            target=self._watch, args=(on_finished,), name=f"{self.job_id}-watcher", daemon=True
        ).start()

    def _read(self) -> None:
        stream = self._proc.stdout
        try:
            while stream is not None:
                data = stream.read(READ_SIZE)
                if not data:
                    break
                self._append(data)
        except (OSError, ValueError):
            pass
        finally:
            self._reader_done.set()

    def _watch(self, on_finished: Callable[[BashJob], None]) -> None:
        """Finish the job when its output pipe closes, not when its shell exits.

        ``npm run dev &`` is how a model often writes a background command:
        the shell exits at once and the server it started stays attached to the
        pipe. A job called finished at that point would hide a live server from
        ``bash_kill`` and from the close that is meant to stop it. The one way
        out besides EOF is a kill that has had its chance -- something that left
        the process group can hold the pipe for ever, and the slot it takes
        against the cap has to come back.
        """
        code = self._proc.wait()
        with self._lock:
            self.exit_code = code
        while not self._reader_done.wait(POLL_S):
            deadline = self._kill_deadline
            if deadline is not None and time.monotonic() >= deadline:
                break
        with self._lock:
            self.status = KILLED if self._kill_requested else EXITED
            self.finished_at = time.monotonic()
        # Announced before ``finished`` is set, so whoever waits on a kill or a
        # close resumes after the ending is already on its way to the log.
        try:
            on_finished(self)
        finally:
            self.finished.set()

    def request_kill(self) -> bool:
        """Kill the process tree without waiting. False if it had already ended."""
        with self._lock:
            if self.status != RUNNING:
                return False
            self._kill_requested = True
            self._kill_deadline = time.monotonic() + KILL_GRACE_S
        if IS_WINDOWS:
            _kill_tree(self.pid)
        else:
            # The job's own process group (``start_new_session``), signalled by
            # id rather than looked up from the shell's pid: the shell may be
            # gone already while what it started lives on, and POSIX does not
            # reuse a pid while a process group still carries it.
            with contextlib.suppress(OSError):
                os.killpg(self.pid, signal.SIGKILL)
        return True

    def kill(self, wait_s: float = KILL_WAIT_S) -> bool:
        killed = self.request_kill()
        if killed:
            self.finished.wait(wait_s)
        return killed

    def to_row(self) -> dict[str, object]:
        return {
            "id": self.job_id,
            "status": self.status,
            "exit_code": self.exit_code,
            "seconds": self.seconds(),
            "unread_bytes": self.unread_bytes(),
            "command": self.label,
        }


class BashJobs:
    """Every background shell job one conversation has started."""

    def __init__(self, *, on_event: OnEvent | None = None, max_running: int = MAX_RUNNING,
                 buffer_bytes: int = BUFFER_BYTES, max_retained: int = MAX_RETAINED) -> None:
        self.on_event = on_event
        self.max_running = max_running
        self.buffer_bytes = buffer_bytes
        self.max_retained = max_retained
        self._jobs: dict[str, BashJob] = {}
        self._forgotten: set[str] = set()
        self._counter = itertools.count(1)
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = False

    def __iter__(self) -> Iterator[BashJob]:
        with self._lock:
            return iter(list(self._jobs.values()))

    def __len__(self) -> int:
        return len(self._jobs)

    def get(self, job_id: str) -> BashJob | None:
        return self._jobs.get(job_id)

    def forgotten(self, job_id: str) -> bool:
        return job_id in self._forgotten

    def running(self) -> list[BashJob]:
        return [j for j in self if j.running]

    async def start(self, argv: list[str], *, cwd: str, command: str,
                    description: str = "") -> BashJob:
        """Spawn ``argv`` detached and return its job once it is running.

        Raises ``JobLimitReached`` at the cap and ``OSError`` when the spawn
        itself fails.
        """
        self._loop = asyncio.get_running_loop()
        return await asyncio.to_thread(self._start, argv, cwd, command, description)

    def _start(self, argv: list[str], cwd: str, command: str, description: str) -> BashJob:
        with self._lock:
            if self._closed:
                raise JobLimitReached("this conversation is closing; no new jobs start")
            live = [j for j in self._jobs.values() if j.running]
            if len(live) >= self.max_running:
                ids = ", ".join(j.job_id for j in live)
                raise JobLimitReached(
                    f"{len(live)} background jobs are already running ({ids}), which is "
                    f"the limit of {self.max_running}. Stop one with bash_kill, or wait "
                    "for one to finish, before starting another."
                )
            env = dict(os.environ)
            env.setdefault("PYTHONUNBUFFERED", "1")
            proc = subproc.popen(
                argv,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
                # Its own process group on POSIX, so a kill reaches everything
                # it started. Ignored on Windows, where taskkill /T walks the tree.
                start_new_session=True,
            )
            job = BashJob(f"bash_{next(self._counter)}", command, description, proc,
                          self.buffer_bytes)
            self._jobs[job.job_id] = job
            self._forget_oldest()
        # Emitted before the threads exist, so no job's ending can reach the log
        # ahead of its beginning however fast the command is.
        self._emit({
            "type": "bash_job_started",
            "job_id": job.job_id,
            "command": command.strip()[:COMMAND_LOG_CHARS],
            "description": description,
        })
        job._begin(self._finished)
        return job

    def _forget_oldest(self) -> None:
        done = [j for j in self._jobs.values() if not j.running]
        for job in done[: max(0, len(done) - self.max_retained)]:
            del self._jobs[job.job_id]
            self._forgotten.add(job.job_id)

    def _finished(self, job: BashJob) -> None:
        self._emit({
            "type": "bash_job_done",
            "job_id": job.job_id,
            "status": job.status,
            "exit_code": job.exit_code,
            "seconds": job.seconds(),
        })

    def _emit(self, ev: dict[str, Any]) -> None:
        """Hand an event to the owner on its own loop, from whichever thread."""
        handler = self.on_event
        if handler is None:
            return
        loop = self._loop
        if loop is None:
            handler(ev)
            return
        try:
            loop.call_soon_threadsafe(handler, ev)
        except RuntimeError:  # the loop has closed: the process is going away
            pass

    def exit_notices(self) -> list[str]:
        """One reminder per job that ended without the model seeing it end."""
        out: list[str] = []
        for job in self:
            if job.running or job.exit_seen or job.noticed:
                continue
            job.noticed = True
            unread = job.unread_bytes()
            tail = (
                f" It has {unread} bytes of output you have not read: call "
                f'bash_output(bash_id="{job.job_id}").'
                if unread else ""
            )
            out.append(f"Background shell job {job.job_id} ({job.label}) {job.outcome()}.{tail}")
        return out

    def close(self) -> int:
        """Kill every job still running and stop accepting new ones."""
        with self._lock:
            self._closed = True
        live = [j for j in self.running() if j.request_kill()]
        deadline = time.monotonic() + KILL_WAIT_S
        for job in live:
            job.finished.wait(max(0.0, deadline - time.monotonic()))
        # Whatever has not ended by now ends unannounced: the owner is going
        # away, and a late event could recreate a session log just deleted.
        self.on_event = None
        return len(live)


def _utf8_boundary(data: bytes) -> int:
    """Length of ``data`` without a trailing UTF-8 sequence that is unfinished."""
    n = len(data)
    for back in range(1, min(4, n) + 1):
        byte = data[n - back]
        if byte < 0x80:
            return n
        if byte >= 0xC0:
            need = 2 if byte < 0xE0 else 3 if byte < 0xF0 else 4
            return n if back >= need else n - back
    return n


def _skip_continuation(data: bytes) -> bytes:
    """Drop the tail of a character whose head the ring already let go of."""
    i = 0
    while i < min(3, len(data)) and 0x80 <= data[i] < 0xC0:
        i += 1
    return data[i:]
