"""Modal screens: permission prompt, model picker, help."""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, ListItem, ListView, Static

from quickcode.core.agent import PermissionOutcome, PermissionRequest


class PermissionModal(ModalScreen[PermissionOutcome]):
    """Blocks the loop awaiting an allow/always/deny decision.

    Keys: y = allow once, a = always allow (persists rule_suggestion),
    n = deny (opens a free-text reason input).
    """

    BINDINGS = [
        ("y", "allow_once", "Allow once"),
        ("a", "allow_always", "Always allow"),
        ("n", "deny", "Deny"),
        ("escape", "deny", "Deny"),
    ]

    def __init__(self, request: PermissionRequest) -> None:
        super().__init__()
        self.request = request
        self._denying = False

    def compose(self) -> ComposeResult:
        req = self.request
        with Vertical():
            yield Label(f"Permission requested by {req.agent_name}", classes="tool-header")
            yield Static(f"tool: {req.tool}", markup=False)
            yield Static(f"arg:  {req.arg}", markup=False)
            if req.preview:
                yield Static(req.preview, classes="msg-reasoning", markup=False)
            yield Static(
                f"always-allow rule: {req.rule_suggestion}",
                classes="msg-reasoning",
                markup=False,
            )
            with Horizontal():
                yield Button("Allow once (y)", id="allow-once", variant="success")
                yield Button("Always allow (a)", id="allow-always", variant="warning")
                yield Button("Deny (n)", id="deny", variant="error")
            yield Input(placeholder="Reason for denial (optional), Enter to confirm", id="deny-reason")

    def on_mount(self) -> None:
        reason_input = self.query_one("#deny-reason", Input)
        reason_input.display = False

    def action_allow_once(self) -> None:
        self.dismiss(PermissionOutcome(allow=True))

    def action_allow_always(self) -> None:
        self.dismiss(PermissionOutcome(allow=True, persist=True))

    def action_deny(self) -> None:
        reason_input = self.query_one("#deny-reason", Input)
        if not self._denying:
            self._denying = True
            reason_input.display = True
            reason_input.focus()
            return
        self.dismiss(PermissionOutcome(allow=False, deny_message=reason_input.value))

    @on(Input.Submitted, "#deny-reason")
    def _submit_deny(self, event: Input.Submitted) -> None:
        self.dismiss(PermissionOutcome(allow=False, deny_message=event.value))

    @on(Button.Pressed, "#allow-once")
    def _allow_once(self) -> None:
        self.action_allow_once()

    @on(Button.Pressed, "#allow-always")
    def _allow_always(self) -> None:
        self.action_allow_always()

    @on(Button.Pressed, "#deny")
    def _deny(self) -> None:
        self.action_deny()


class ModelPicker(ModalScreen[str | None]):
    """F2: fuzzy-ish list of models; selecting returns the model id (or None)."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, models: list[str], current: str = "") -> None:
        super().__init__()
        self._models = models
        self._current = current

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Select a model (Esc to cancel)")
            yield Input(placeholder="filter…", id="model-filter")
            yield ListView(*self._items(self._models), id="model-list")

    def _items(self, models: list[str]) -> list[ListItem]:
        items = []
        for m in models:
            label = f"* {m}" if m == self._current else f"  {m}"
            item = ListItem(Label(label))
            item.model_id = m  # type: ignore[attr-defined]
            items.append(item)
        return items

    @on(Input.Changed, "#model-filter")
    def _filter(self, event: Input.Changed) -> None:
        needle = event.value.lower()
        filtered = [m for m in self._models if needle in m.lower()] if needle else self._models
        list_view = self.query_one("#model-list", ListView)
        list_view.clear()
        for item in self._items(filtered):
            list_view.append(item)

    @on(ListView.Selected, "#model-list")
    def _selected(self, event: ListView.Selected) -> None:
        model_id = getattr(event.item, "model_id", None)
        self.dismiss(model_id)

    def action_cancel(self) -> None:
        self.dismiss(None)


class HelpScreen(ModalScreen[None]):
    """F1: keybinding reference."""

    BINDINGS = [("escape", "close", "Close"), ("f1", "close", "Close")]

    _TEXT = """\
QuickCode — keybindings

  Enter        submit message
  Ctrl+J       newline in input
  Shift+Tab    cycle permission mode (plan -> ask -> auto-edit [-> yolo])
  Ctrl+P       command palette
  F1           this help screen
  F2           model picker
  F3           settings (Models / Usage / Permissions / Profile)
  Ctrl+C       clear input, press twice to quit
  Esc          interrupt the running turn
"""

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._TEXT)
            yield Button("Close (Esc)", id="close-help")

    @on(Button.Pressed, "#close-help")
    def _close(self) -> None:
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)
