"""The `/agents` overview screen: a read-only reference of the subagent
definitions available in this project, plus this session's spawn tally.

Delegation itself stays the model's job (the ``agent`` tool); this screen is
for the human to see *what* agents exist and how they're configured.
"""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from quickcode.subagents.definitions import AgentDef, load_defs
from quickcode.subagents.runner import MAX_AGENTS, MAX_DEPTH


def _def_block(defn: AgentDef) -> str:
    tools = "all core tools" if defn.tools is None else ", ".join(defn.tools)
    return (
        f"▍ {defn.name}\n"
        f"    {defn.description}\n"
        f"    tools: {tools}\n"
        f"    model: {defn.model}   mode cap: {defn.mode_cap.value}   "
        f"max turns: {defn.max_turns}\n"
    )


def agents_overview(cwd: Path, spawned: list[str]) -> str:
    defs = load_defs(cwd)
    lines = ["Agents — subagent definitions the model can delegate to\n"]
    # Built-ins first, then any custom .md definitions, each in name order.
    for name in ("explore", "general"):
        if name in defs:
            lines.append(_def_block(defs[name]))
    for name in sorted(defs):
        if name not in ("explore", "general"):
            lines.append(_def_block(defs[name]))

    lines.append("")
    lines.append(
        f"This session: {len(spawned)} spawned"
        + (f" ({', '.join(spawned)})" if spawned else "")
        + f"   ·   limits: {MAX_AGENTS} per conversation, depth ≤ {MAX_DEPTH}"
    )
    lines.append(
        "\nDefinitions load from .quickcode/agents/*.md (project) and "
        "~/.quickcode/agents/*.md (user); project shadows user shadows built-in.\n"
        "The model delegates via the agent tool; running subagents appear as live "
        "panes (Ctrl+←/→ to navigate)."
    )
    return "\n".join(lines)


class AgentsScreen(ModalScreen[None]):
    """`/agents`: read-only overview of available subagent definitions."""

    BINDINGS = [("escape", "close", "Close"), ("f4", "close", "Close")]

    DEFAULT_CSS = """
    AgentsScreen {
        align: center middle;
    }

    AgentsScreen > VerticalScroll {
        width: 80%;
        max-width: 100;
        height: 80%;
        border: round $primary;
        background: $panel;
        padding: 1 2;
    }

    AgentsScreen #agents-text {
        height: auto;
    }

    AgentsScreen Button {
        margin: 1 0 0 0;
    }
    """

    def __init__(self, *, cwd: Path | str, spawned: list[str] | None = None) -> None:
        super().__init__()
        self._cwd = Path(cwd)
        self._spawned = list(spawned or [])

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static(
                agents_overview(self._cwd, self._spawned),
                id="agents-text",
                markup=False,
            )
            yield Button("Close (Esc)", id="close-agents")

    def action_close(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close-agents":
            self.dismiss(None)
