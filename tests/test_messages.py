"""Transcript message presentation: plain "› " user lines, flush-left
un-indented assistant Markdown, and collapsible Thinking."""

from pathlib import Path

from textual.widgets import Collapsible, Markdown

from quickcode.app import QuickCodeApp
from quickcode.config import Config
from quickcode.core.agent import AgentInstance
from quickcode.core.events import ReasoningDelta, TextDelta, TurnDone
from quickcode.core.history import History
from quickcode.core.permissions import Mode, PermissionEngine, Rules
from quickcode.tools.base import ReadRegistry, ToolCtx
from quickcode.tools.registry import default_registry
from quickcode.ui.transcript import Transcript


class _Prov:
    async def stream_chat(self, req):
        yield ReasoningDelta("thinking hard ")
        for w in ("Hey! ", "what ", "are ", "you ", "on?"):
            yield TextDelta(w)
        yield TurnDone("stop")

    async def list_models(self):
        return []


def _agent():
    ctx = ToolCtx(cwd=Path.cwd(), read_registry=ReadRegistry(), extra={})
    return AgentInstance(
        name="m",
        provider=_Prov(),
        registry=default_registry(),
        history=History("SYS"),
        ctx=ctx,
        permissions=PermissionEngine(Mode.ask, Rules(), Path.cwd()),
        model="t",
        permission_cb=None,
    )


async def test_plain_messages_and_thinking_collapses():
    app = QuickCodeApp(_agent(), Config())
    async with app.run_test(size=(100, 40)) as pilot:
        transcript = app.query_one(Transcript)
        transcript.add_user("hey")
        await app._run_turn("hey")
        app._drain_bus()
        for _ in range(10):
            await pilot.pause()

        user = next(iter(transcript.query(".msg-user")))
        assert str(user.render()).startswith("› hey")

        md = next(iter(transcript.walk_children(Markdown)))
        assert "Hey! what are you on?" in md.source
        # Markdown's built-in left padding of 2 is removed (no phantom indent).
        assert md.styles.padding.left == 0

        boxes = [b for b in transcript.query(Collapsible) if "reasoning-box" in b.classes]
        assert boxes and boxes[0].collapsed is True
