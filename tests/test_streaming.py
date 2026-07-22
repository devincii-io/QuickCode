"""Streaming render path: event coalescing + throttled transcript flush.

Guards the perf fixes: a burst of token deltas must fold to minimal DOM work,
and the accumulated text must render in full (the Markdown widget's own mount
resets its content, so a naive per-token append was silently wiped)."""

from pathlib import Path

from textual.widgets import Markdown

from quickcode.app import QuickCodeApp, _coalesce
from quickcode.config import Config
from quickcode.core.agent import AgentInstance
from quickcode.core.events import TextDelta, ToolCallDelta, TurnDone, Usage
from quickcode.core.history import History
from quickcode.core.permissions import Mode, PermissionEngine, Rules
from quickcode.tools.base import ReadRegistry, ToolCtx
from quickcode.tools.registry import default_registry
from quickcode.ui.transcript import Transcript


def test_coalesce_folds_deltas_and_keeps_last_usage():
    evs = [
        TextDelta("a"),
        TextDelta("b"),
        TextDelta("c"),
        ToolCallDelta("1", "{"),
        ToolCallDelta("1", "}"),
        Usage(input_tokens=1),
        Usage(input_tokens=2),
    ]
    out = _coalesce(evs)
    texts = [e for e in out if isinstance(e, TextDelta)]
    tcs = [e for e in out if isinstance(e, ToolCallDelta)]
    usages = [e for e in out if isinstance(e, Usage)]
    assert len(texts) == 1 and texts[0].text == "abc"
    assert len(tcs) == 1 and tcs[0].arguments == "{}"
    assert len(usages) == 1 and usages[0].input_tokens == 2


def test_coalesce_does_not_merge_across_different_tool_ids():
    out = _coalesce([ToolCallDelta("1", "a"), ToolCallDelta("2", "b")])
    assert [e.id for e in out] == ["1", "2"]


class _StreamProvider:
    async def stream_chat(self, req):
        for w in ("Here ", "is ", "**bold** ", "and ", "`code`. "):
            yield TextDelta(w)
        yield Usage(input_tokens=10, output_tokens=5)
        yield TurnDone("stop")

    async def list_models(self):
        return []


def _agent():
    ctx = ToolCtx(cwd=Path.cwd(), read_registry=ReadRegistry(), extra={})
    return AgentInstance(
        name="m",
        provider=_StreamProvider(),
        registry=default_registry(),
        history=History("SYS"),
        ctx=ctx,
        permissions=PermissionEngine(Mode.ask, Rules(), Path.cwd()),
        model="t",
        permission_cb=None,
    )


async def test_streamed_text_renders_in_full():
    app = QuickCodeApp(_agent(), Config())
    async with app.run_test(size=(100, 40)) as pilot:
        await app._run_turn("hi")
        app._drain_bus()
        for _ in range(8):
            await pilot.pause()
        transcript = app.query_one(Transcript)
        blocks = list(transcript.walk_children(Markdown))
        assert len(blocks) == 1
        assert "Here is **bold** and `code`." in blocks[0].source
