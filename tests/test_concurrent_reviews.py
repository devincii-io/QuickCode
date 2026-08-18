"""Several permission prompts can be open at once, and each is answered on its own.

Read-only tool calls in one assistant message run concurrently (core/loop.py
gathers them), so four `read` calls against a protected path open four permission
futures at the same time. The web UI used to show one dialog per request and let
each new one wipe the last, which left three futures awaiting a decision that
could no longer be given: the tool calls hung and the turn never ended. The
frontend now queues reviews, and it leans on two server guarantees pinned here —
every request gets its own id and future, and `state.pending` lists all of them,
so a reload or a lost frame recovers rather than strands.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from quickcode.core.agent import PermissionRequest
from tests.test_server import FakeProvider, make_manager


def _conversation(tmp_path: Path):
    manager = make_manager(tmp_path, FakeProvider([]))
    return manager, manager.open()


async def test_four_concurrent_requests_each_get_their_own_decision(tmp_path: Path) -> None:
    _, conv = _conversation(tmp_path)
    reqs = [
        PermissionRequest(tool="read", arg=f".quickcode/artifacts/explore-{i}.md",
                          rule_suggestion=f"allow read {i}")
        for i in range(4)
    ]
    pending = [asyncio.create_task(conv.permission_cb(r)) for r in reqs]
    await asyncio.sleep(0)  # let each callback register and emit

    assert len(conv.pending) == 4
    ids = [p.req_id for p in conv.pending.values()]
    assert len(set(ids)) == 4, "each request needs its own id, or answers collide"

    # Answering the last one leaves the other three waiting, not resolved.
    assert conv.resolve_permission(ids[-1], allow=True, persist=False, deny_message="")
    done = await pending[-1]
    assert done.allow
    assert sum(t.done() for t in pending) == 1

    for req_id in ids[:-1]:
        assert conv.resolve_permission(req_id, allow=False, persist=False, deny_message="no")
    outcomes = await asyncio.gather(*pending)
    assert [o.allow for o in outcomes] == [False, False, False, True]
    assert conv.pending == {}


async def test_state_lists_every_pending_review_so_a_reload_can_recover(tmp_path: Path) -> None:
    _, conv = _conversation(tmp_path)
    reqs = [
        PermissionRequest(tool="read", arg=f"file-{i}.md", rule_suggestion="allow read")
        for i in range(3)
    ]
    pending = [asyncio.create_task(conv.permission_cb(r)) for r in reqs]
    await asyncio.sleep(0)

    listed = conv.state_event()["pending"]
    assert len(listed) == 3
    assert {p["kind"] for p in listed} == {"permission"}
    # The payload has to carry enough to rebuild the dialog after a reconnect.
    for entry in listed:
        assert entry["req_id"] and entry["tool"] == "read" and entry["rule_suggestion"]

    for entry in listed:
        conv.resolve_permission(entry["req_id"], allow=True, persist=False, deny_message="")
    await asyncio.gather(*pending)
    assert conv.state_event()["pending"] == []


async def test_a_decision_for_an_unknown_request_is_refused_not_swallowed(tmp_path: Path) -> None:
    _, conv = _conversation(tmp_path)
    assert not conv.resolve_permission("nope", allow=True, persist=False, deny_message="")

    req = PermissionRequest(tool="read", arg="a.md", rule_suggestion="allow read")
    task = asyncio.create_task(conv.permission_cb(req))
    await asyncio.sleep(0)
    req_id = next(iter(conv.pending))
    assert conv.resolve_permission(req_id, allow=True, persist=False, deny_message="")
    # A second answer for the same request must not raise on the settled future.
    assert not conv.resolve_permission(req_id, allow=False, persist=False, deny_message="")
    await task
