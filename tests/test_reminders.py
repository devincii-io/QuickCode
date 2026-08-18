"""A reminder is delivered when it is news, not on every turn.

The permission-mode reminder used to be spliced into every single user turn
for the life of a session — the same sentence, unchanged, paid for on every
request. It is edge-triggered now: the first turn announces the mode, and
after that only a change does.

Everything here runs against a scripted fake provider; no live model call is
made and none is needed to see what was put in front of one.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

from quickcode.core.agent import AgentInstance, PermissionOutcome
from quickcode.core.events import TextDelta, TurnDone
from quickcode.core.history import History
from quickcode.core.permissions import Mode, PermissionEngine, Rules
from quickcode.tools.base import ReadRegistry, ToolCtx
from quickcode.tools.registry import default_registry


class TextProvider:
    """Answers every request with one word and records what it was sent."""

    def __init__(self) -> None:
        self.requests: list = []

    async def stream_chat(self, req) -> AsyncIterator:
        self.requests.append(req)
        yield TextDelta("ok")
        yield TurnDone("stop")

    async def list_models(self):
        return []


async def _deny(_r):
    return PermissionOutcome(allow=False)


def _agent(provider, mode=Mode.ask):
    ctx = ToolCtx(cwd=Path.cwd(), read_registry=ReadRegistry(), extra={})
    return AgentInstance(
        name="main",
        provider=provider,
        registry=default_registry(),
        history=History("SYS"),
        ctx=ctx,
        permissions=PermissionEngine(mode, Rules(), Path.cwd()),
        model="test/model",
        permission_cb=_deny,
    )


def _user_turns(provider):
    """The user messages of the most recent request, in order."""
    return [m.content or "" for m in provider.requests[-1].messages if m.role == "user"]


def _mode_notes(provider):
    """Every user message carrying a permission-mode reminder."""
    return [c for c in _user_turns(provider) if "Permission mode:" in c]


def test_the_first_turn_announces_the_mode():
    provider = TextProvider()
    agent = _agent(provider, mode=Mode.ask)
    asyncio.run(agent.run_turn("hello"))
    assert len(_mode_notes(provider)) == 1
    assert "ASK" in _mode_notes(provider)[0]


def test_an_unchanged_mode_is_not_repeated():
    """The defect: the same sentence, every turn, for the whole session."""
    provider = TextProvider()
    agent = _agent(provider, mode=Mode.ask)
    for _ in range(4):
        asyncio.run(agent.run_turn("again"))
    # Four turns, one announcement — the first.
    assert len(_mode_notes(provider)) == 1


def test_changing_the_mode_announces_it_once():
    provider = TextProvider()
    agent = _agent(provider, mode=Mode.ask)
    asyncio.run(agent.run_turn("one"))
    agent.set_mode(Mode.yolo)
    asyncio.run(agent.run_turn("two"))
    asyncio.run(agent.run_turn("three"))

    notes = _mode_notes(provider)
    assert len(notes) == 2, "expected the initial mode and the change, nothing more"
    assert "ASK" in notes[0]
    assert "YOLO" in notes[1]


def test_compaction_re_announces_the_mode():
    """Compaction rewrites the transcript, so the announcement may not survive."""
    provider = TextProvider()
    agent = _agent(provider, mode=Mode.ask)
    asyncio.run(agent.run_turn("one"))
    agent.mark_compacted()
    asyncio.run(agent.run_turn("two"))

    assert len(_mode_notes(provider)) == 2


def test_a_queued_reminder_is_delivered_once():
    provider = TextProvider()
    agent = _agent(provider, mode=Mode.ask)
    agent.queue_reminder("the composition changed under you")
    asyncio.run(agent.run_turn("one"))
    assert any("composition changed" in c for c in _user_turns(provider))

    # A second turn does not re-send it. It stays visible in the transcript,
    # because it was said once and the record of a turn does not change — what
    # must not happen is it being said again.
    asyncio.run(agent.run_turn("two"))
    assert sum("composition changed" in c for c in _user_turns(provider)) == 1


def test_a_duplicate_reminder_collapses():
    provider = TextProvider()
    agent = _agent(provider, mode=Mode.ask)
    agent.queue_reminder("same thing")
    agent.queue_reminder("same thing")
    asyncio.run(agent.run_turn("one"))
    assert sum("same thing" in c for c in _user_turns(provider)) == 1
