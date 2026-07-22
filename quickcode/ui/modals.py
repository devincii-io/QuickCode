"""Modal screens: permission prompt, model picker, help."""

from __future__ import annotations

from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, ListItem, ListView, Static

from quickcode.core.agent import PermissionOutcome, PermissionRequest
from quickcode.providers.base import ModelInfo

MODEL_PAGE_SIZE = 40


def format_context(n: int | None) -> str:
    """Compact context-length label: 200000 -> '200K', 1000000 -> '1M'."""
    if not n:
        return "—"
    if n >= 1_000_000:
        v = n / 1_000_000
        s = f"{v:.1f}".rstrip("0").rstrip(".")
        return f"{s}M"
    if n >= 1_000:
        return f"{round(n / 1_000)}K"
    return str(n)


def format_price(prompt_price: float | None, completion_price: float | None) -> str:
    """Price label in USD per 1M tokens, e.g. '$3.00/$15.00 /M'."""
    if prompt_price is None and completion_price is None:
        return "price n/a"
    if prompt_price == 0 and completion_price == 0:
        return "free"
    p = f"${prompt_price:.2f}" if prompt_price is not None else "$?"
    c = f"${completion_price:.2f}" if completion_price is not None else "$?"
    return f"{p}/{c} /M"


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
    """F2: searchable, paginated model picker with context/price columns.

    Fetches the full model catalog once (via ``provider.list_models()`` in a
    worker) and caches it, but only ever builds widgets for a capped page —
    the default view (empty filter) surfaces curated ``catalog_ids`` first,
    then the highest-context models; typing narrows to substring matches on
    the model id. Dismisses with the selected model id, or ``None``.
    """

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("up", "cursor_up", "Up"),
        ("down", "cursor_down", "Down"),
    ]

    DEFAULT_CSS = """
    ModelPicker {
        align: center middle;
    }

    ModelPicker > Vertical {
        width: 92;
        height: 32;
        background: $panel;
        border: round $primary;
        padding: 1 2;
    }

    ModelPicker #model-title {
        text-style: bold;
        color: $accent;
        padding-bottom: 1;
    }

    ModelPicker #model-filter {
        margin-bottom: 1;
    }

    ModelPicker #model-status {
        color: $text-muted;
        padding-bottom: 1;
        height: 1;
    }

    ModelPicker ListView {
        height: 1fr;
        background: $boost;
        border: round $primary-darken-1;
    }

    ModelPicker ListItem {
        padding: 0 1;
    }

    ModelPicker ListItem.model-current Label {
        color: $accent;
        text-style: bold;
    }

    ModelPicker ListView > ListItem.-highlight {
        background: $accent 40%;
    }

    ModelPicker ListView:focus > ListItem.-highlight {
        background: $accent 60%;
    }
    """

    def __init__(
        self,
        provider,
        current: str = "",
        catalog_ids: list[str] | None = None,
    ) -> None:
        super().__init__()
        self._provider = provider
        self._current = current
        self._catalog_ids = catalog_ids or []
        self._models: list[ModelInfo] = []
        self._loaded = False
        self._load_error = False

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Select a model  (Esc cancel · Enter select)", id="model-title")
            yield Input(placeholder="filter…", id="model-filter")
            yield Static("Loading models…", id="model-status")
            yield ListView(id="model-list")

    def on_mount(self) -> None:
        self.query_one("#model-filter", Input).focus()
        self._load_models()

    @work(exclusive=True)
    async def _load_models(self) -> None:
        try:
            models = await self._provider.list_models()
        except Exception:
            models = []
        if not models:
            self._load_error = True
            ids = list(
                dict.fromkeys(self._catalog_ids + ([self._current] if self._current else []))
            )
            models = [ModelInfo(id=i, name=i) for i in ids if i]
        self._models = models
        self._loaded = True
        self._render_list(self.query_one("#model-filter", Input).value)

    def _sorted_default(self) -> list[ModelInfo]:
        """Curated ids first, then descending context length."""
        pinned_ids = set(self._catalog_ids)
        pinned = [m for m in self._models if m.id in pinned_ids]
        rest = [m for m in self._models if m.id not in pinned_ids]
        pinned.sort(key=lambda m: m.context_length or 0, reverse=True)
        rest.sort(key=lambda m: m.context_length or 0, reverse=True)
        return pinned + rest

    def _render_list(self, query: str) -> None:
        needle = query.strip().lower()
        if needle:
            matches = [m for m in self._models if needle in m.id.lower()]
        else:
            matches = self._sorted_default()

        total = len(matches)
        page = matches[:MODEL_PAGE_SIZE]

        status = self.query_one("#model-status", Static)
        if not self._loaded:
            status.update("Loading models…")
        else:
            note = ""
            if self._load_error:
                note = "  (couldn't reach provider — showing catalog/defaults)"
            status.update(f"showing {len(page)} of {total} — type to filter{note}")

        list_view = self.query_one("#model-list", ListView)
        list_view.clear()
        for m in page:
            list_view.append(self._item(m))
        if page:
            list_view.index = 0

    def _item(self, m: ModelInfo) -> ListItem:
        marker = "● " if m.id == self._current else "  "
        ctx = format_context(m.context_length)
        price = format_price(m.prompt_price, m.completion_price)
        label = f"{marker}{m.id:<42} {ctx:>6} ctx   {price}"
        classes = "model-current" if m.id == self._current else ""
        item = ListItem(Label(label, markup=False), classes=classes)
        item.model_id = m.id  # type: ignore[attr-defined]
        return item

    @on(Input.Changed, "#model-filter")
    def _filter(self, event: Input.Changed) -> None:
        self._render_list(event.value)

    @on(Input.Submitted, "#model-filter")
    def _submit(self, event: Input.Submitted) -> None:
        list_view = self.query_one("#model-list", ListView)
        item = list_view.highlighted_child
        model_id = getattr(item, "model_id", None) if item else None
        if model_id:
            self.dismiss(model_id)

    @on(ListView.Selected, "#model-list")
    def _selected(self, event: ListView.Selected) -> None:
        model_id = getattr(event.item, "model_id", None)
        self.dismiss(model_id)

    def action_cursor_up(self) -> None:
        self.query_one("#model-list", ListView).action_cursor_up()

    def action_cursor_down(self) -> None:
        self.query_one("#model-list", ListView).action_cursor_down()

    def action_cancel(self) -> None:
        self.dismiss(None)


class HelpScreen(ModalScreen[None]):
    """F1: keybinding reference."""

    BINDINGS = [("escape", "close", "Close"), ("f1", "close", "Close")]

    _TEXT = """\
QuickCode — keybindings (keyboard-only friendly)

  Enter          submit message
  Ctrl+J         newline in input
  Tab            focus into the transcript (thinking / tool blocks); again to advance
  Shift+Tab      in the input: cycle permission mode · in the transcript: focus back
  Enter (block)  expand / collapse a focused Thinking or tool block
  Esc            return focus to the input (or interrupt a running turn)
  PageUp/Down    scroll the transcript
  Ctrl+Home/End  jump to top / bottom of transcript
  Ctrl+B         toggle the task board sidebar
  Ctrl+P         command palette
  F1             this help screen
  F2             model picker (type to filter · ↑/↓ · Enter)
  F3             settings (Models / Usage / Permissions / Theme / Profile)
  Ctrl+C         clear input, press twice to quit
  Esc            interrupt the running turn
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
