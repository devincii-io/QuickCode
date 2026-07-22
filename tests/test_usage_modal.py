"""The /usage overview screen: content + it opens/closes via the /usage
command in the full app."""

from pathlib import Path

from quickcode.app import QuickCodeApp
from quickcode.config import Config
from quickcode.core.agent import AgentInstance, Ledger
from quickcode.core.events import ReasoningDelta, TextDelta, TurnDone
from quickcode.core.history import History
from quickcode.core.permissions import Mode, PermissionEngine, Rules
from quickcode.tools.base import ReadRegistry, ToolCtx
from quickcode.tools.registry import default_registry
from quickcode.ui.slashmenu import SLASH_COMMANDS
from quickcode.ui.usage_modal import UsageScreen, usage_text


def test_usage_text_includes_model_tokens_and_cost():
    ledger = Ledger(input_tokens=100, output_tokens=50, cached_tokens=10, cost_usd=0.1234)
    text = usage_text(model="gpt-test", ledger=ledger, context_pct=12.5)
    assert "gpt-test" in text
    assert "100" in text
    assert "50" in text
    assert "10" in text
    assert "12.5%" in text
    assert "$0.1234" in text


def test_usage_text_includes_subagent_line_when_spawned():
    ledger = Ledger()
    text = usage_text(
        model="m", ledger=ledger, context_pct=None, spawned=["explore-1", "general-2"]
    )
    assert "subagents spawned this session: 2" in text
    assert "explore-1" in text and "general-2" in text


def test_usage_text_omits_subagent_line_when_empty():
    ledger = Ledger()
    text = usage_text(model="m", ledger=ledger, context_pct=None, spawned=[])
    assert "subagents spawned" not in text


def test_usage_command_registered():
    assert any(name == "/usage" for name, _d, _a in SLASH_COMMANDS)


class _Prov:
    async def stream_chat(self, req):
        yield ReasoningDelta("thinking ")
        for w in ("Hey! ", "there"):
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


async def test_usage_screen_opens_and_closes_via_command():
    app = QuickCodeApp(_agent(), Config())
    async with app.run_test(size=(100, 40)) as pilot:
        app._handle_command("/usage")
        await pilot.pause()
        assert isinstance(app.screen, UsageScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, UsageScreen)
