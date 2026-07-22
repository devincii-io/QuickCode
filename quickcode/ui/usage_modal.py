"""The `/usage` overview screen: a read-only report of this session's token
counts and cost, mirroring the style of the Usage tab in Settings.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from quickcode.core.agent import Ledger


def usage_text(
    *,
    model: str,
    ledger: Ledger,
    context_pct: float | None,
    spawned: list[str] | None = None,
) -> str:
    pct_s = f"{context_pct:.1f}%" if context_pct is not None else "n/a"
    lines = [
        f"model: {model}\n",
        f"input tokens:  {ledger.input_tokens}",
        f"output tokens: {ledger.output_tokens}",
        f"cached tokens: {ledger.cached_tokens}",
        f"context used:  {pct_s}",
        f"cost (session): ${ledger.cost_usd:.4f}",
    ]
    spawned = list(spawned or [])
    if spawned:
        lines.append("")
        lines.append(
            f"subagents spawned this session: {len(spawned)} ({', '.join(spawned)})"
        )
    return "\n".join(lines)


class UsageScreen(ModalScreen[None]):
    """`/usage`: read-only report of this session's token & cost usage."""

    BINDINGS = [("escape", "close", "Close"), ("f4", "close", "Close")]

    DEFAULT_CSS = """
    UsageScreen {
        align: center middle;
    }

    UsageScreen > VerticalScroll {
        width: 80%;
        max-width: 100;
        height: 80%;
        border: round $primary;
        background: $panel;
        padding: 1 2;
    }

    UsageScreen #usage-text {
        height: auto;
    }

    UsageScreen Button {
        margin: 1 0 0 0;
    }
    """

    def __init__(
        self,
        *,
        model: str,
        ledger: Ledger,
        context_pct: float | None,
        spawned: list[str] | None = None,
    ) -> None:
        super().__init__()
        self._model = model
        self._ledger = ledger
        self._context_pct = context_pct
        self._spawned = list(spawned or [])

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static(
                usage_text(
                    model=self._model,
                    ledger=self._ledger,
                    context_pct=self._context_pct,
                    spawned=self._spawned,
                ),
                id="usage-text",
                markup=False,
            )
            yield Button("Close (Esc)", id="close-usage")

    def action_close(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close-usage":
            self.dismiss(None)
