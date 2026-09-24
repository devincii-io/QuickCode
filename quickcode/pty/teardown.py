"""Ending everything a terminal started, not just the shell.

``session._kill_tree`` is right for the agent's one-shot commands: ``bash -lc``
has no job control, so everything the command starts stays in the shell's
process group and one ``killpg`` ends it. An *interactive* shell is different.
With job control on, every pipeline gets a process group of its own, so
killing the shell's group left ``sleep 1000 &`` — or a dev server started in
the background — running after the panel was gone.

What they all still share is the session: the shell is its leader (it was
started with ``start_new_session``), and a job only leaves it by calling
``setsid`` on purpose. So the unit this module ends is the session. First
``SIGHUP``, which is what a real terminal sends when its window closes and
what lets an editor save its recovery file; then, for whatever ignored it,
``SIGKILL``.

Membership is read fresh before each signal rather than remembered: a PID
from a stale snapshot may already belong to somebody else. Filtering on the
session id is safe against that — the kernel does not hand out a number that
is still in use as a session id.
"""

from __future__ import annotations

import os
import signal
import sys
import time

from quickcode import subproc

IS_WINDOWS = sys.platform.startswith("win")
HANGUP_GRACE_S = 0.2
_POLL_S = 0.01


def session_members(sid: int) -> set[int] | None:
    """Every live process in session ``sid``; None when it cannot be told."""
    if sys.platform.startswith("linux"):
        return _members_from_proc(sid)
    return _members_from_pgrep(sid)


def _members_from_proc(sid: int) -> set[int] | None:
    members: set[int] = set()
    try:
        entries = os.listdir("/proc")
    except OSError:
        return None
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", encoding="utf-8", errors="replace") as fh:
                stat = fh.read()
        except OSError:
            continue  # exited while we looked
        # `pid (comm) state ppid pgrp session ...` -- comm may hold spaces or
        # parentheses, so split after the last one.
        fields = stat.rsplit(")", 1)[-1].split()
        if len(fields) > 3 and fields[0] != "Z" and fields[3] == str(sid):
            members.add(int(entry))
    return members


def _members_from_pgrep(sid: int) -> set[int] | None:
    # macOS and the BSDs have no /proc; their pgrep filters on the session id
    # through sysctl (KERN_PROC_SESSION), which is the same question.
    try:
        done = subproc.run(["pgrep", "-s", str(sid)], capture_output=True, timeout=5)
    except Exception:  # noqa: BLE001 - no pgrep: fall back to the leader alone
        return None
    if done.returncode not in (0, 1):  # 1 is "no match", which is an answer
        return None
    return {int(tok) for tok in done.stdout.split() if tok.isdigit()}


def _signal_all(pids: set[int], sig: int) -> None:
    groups: set[int] = set()
    for pid in pids:
        try:
            groups.add(os.getpgid(pid))
        except OSError:
            pass
    for pgid in groups:
        try:
            os.killpg(pgid, sig)
        except OSError:
            pass
    for pid in pids:
        try:
            os.kill(pid, sig)
        except OSError:
            pass


def end_session(leader: int | None, *, leader_alive: bool = True) -> None:
    """SIGHUP, then SIGKILL, every process in the session ``leader`` leads.

    ``leader_alive`` is the caller's word that ``leader`` is still its
    unreaped child. Once reaped, the number is free for reuse, so it is only
    signalled directly while that holds; members are found by session id.
    """
    if leader is None:
        return
    if IS_WINDOWS:
        # taskkill /T already walks the whole tree; there is no session to
        # find. Imported late: the batch module owns that code path.
        from quickcode.pty.session import _kill_tree

        if leader_alive:
            _kill_tree(leader)
        return

    def members() -> set[int]:
        found = session_members(leader) or set()
        return found | {leader} if leader_alive else found

    targets = members()
    if not targets:
        return
    _signal_all(targets, signal.SIGHUP)
    # A stopped job cannot act on SIGHUP until it runs again.
    _signal_all(targets, signal.SIGCONT)
    deadline = time.monotonic() + HANGUP_GRACE_S
    while time.monotonic() < deadline:
        if session_members(leader) == set():
            return
        time.sleep(_POLL_S)
    _signal_all(members(), signal.SIGKILL)
