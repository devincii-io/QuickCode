"""The slash-command autocomplete popup.

Appears above the input the moment you type ``/``, filters as you keep typing,
and is fully keyboard-driven (↑/↓ to move, Tab/Enter to accept, Esc to close).
"""

from __future__ import annotations

from textual.widgets import OptionList
from textual.widgets.option_list import Option

# (name, description, takes_args). ``takes_args`` commands complete to "<cmd> "
# and wait for input; the rest run on accept.
SLASH_COMMANDS: list[tuple[str, str, bool]] = [
    ("/help", "Keybindings & command reference", False),
    ("/model", "Switch the active model (picker)", False),
    ("/settings", "Open settings (models · usage · theme · profile)", False),
    ("/agents", "Show subagent definitions the model can delegate to", False),
    ("/usage", "Show token & cost usage for this session", False),
    ("/mode", "Set permission mode: plan | ask | auto-edit | yolo", True),
    ("/tasks", "Toggle the task board sidebar", False),
    ("/compact", "Compress the conversation to free up context", False),
    ("/clear", "Clear the conversation and start fresh", False),
    ("/quit", "Exit QuickCode", False),
]

_TAKES_ARGS = {name for name, _d, args in SLASH_COMMANDS if args}


def command_takes_args(name: str) -> bool:
    return name in _TAKES_ARGS


def match_commands(query: str) -> list[tuple[str, str, bool]]:
    """Commands whose name starts with ``query`` (a leading-slash token)."""
    q = query.strip().lower()
    if not q.startswith("/"):
        return []
    return [c for c in SLASH_COMMANDS if c[0].startswith(q)]


class SlashMenu(OptionList):
    """A filtered, keyboard-navigable list of slash commands."""

    DEFAULT_CSS = """
    SlashMenu {
        display: none;
        height: auto;
        max-height: 9;
        margin: 0 1;
        padding: 0;
        border: round $primary-darken-1;
        background: $panel;
        color: $text;
    }

    SlashMenu > .option-list--option {
        padding: 0 1;
    }

    SlashMenu:focus > .option-list--option-highlighted,
    SlashMenu > .option-list--option-highlighted {
        background: $accent 35%;
        color: $text;
    }
    """

    def show_for(self, query: str) -> bool:
        """Repopulate for ``query``; return True if the menu is now visible."""
        matches = match_commands(query)
        # Only offer while still typing the command token (no space/newline yet).
        if not matches or " " in query.strip() or "\n" in query:
            self.display = False
            return False
        self.clear_options()
        for name, desc, _args in matches:
            self.add_option(Option(f"{name:<11} {desc}", id=name))
        self.display = True
        self.highlighted = 0
        return True

    def hide(self) -> None:
        self.display = False

    @property
    def is_open(self) -> bool:
        return self.display

    def selected_command(self) -> str | None:
        if self.highlighted is None:
            return None
        return self.get_option_at_index(self.highlighted).id
