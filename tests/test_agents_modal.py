"""The /agents overview screen: content + it opens/closes standalone."""

from pathlib import Path

from textual.app import App
from textual.widgets import Static

from quickcode.ui.agents_modal import AgentsScreen, agents_overview
from quickcode.ui.slashmenu import SLASH_COMMANDS


def test_overview_lists_builtins_and_session_tally():
    text = agents_overview(Path.cwd(), ["explore-1", "general-2"])
    assert "explore" in text and "general" in text
    assert "read, glob, grep" in text  # explore's bounded toolset
    assert "all core tools" in text  # general inherits all
    assert "2 spawned" in text
    assert "explore-1" in text


def test_agents_command_registered():
    assert any(name == "/agents" for name, _d, _a in SLASH_COMMANDS)


class _Host(App[None]):
    def compose(self):
        yield Static("host")


async def test_agents_screen_opens_and_closes():
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(AgentsScreen(cwd=Path.cwd(), spawned=[]))
        await pilot.pause()
        assert isinstance(app.screen, AgentsScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, AgentsScreen)
