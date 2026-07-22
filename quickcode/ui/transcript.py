"""The scrollable conversation transcript.

Renders the normalized ``AgentEvent`` stream: streaming assistant markdown,
dim collapsible reasoning, one-line tool call headers, and collapsible tool
result bodies (auto-collapsed on success, expanded + red on error).
"""

from __future__ import annotations

from textual.containers import VerticalScroll
from textual.widgets import Collapsible, Markdown, Static


def _preview(args: str, limit: int = 60) -> str:
    args = args.replace("\n", " ").strip()
    if len(args) > limit:
        args = args[: limit - 1] + "…"
    return args


class Transcript(VerticalScroll):
    """Container that owns the running conversation view."""

    def __init__(self, **kwargs) -> None:
        super().__init__(id="transcript", **kwargs)
        self._current_assistant: Markdown | None = None
        self._assistant_text: str = ""
        self._tool_headers: dict[str, Static] = {}
        self._tool_names: dict[str, str] = {}
        self._tool_args: dict[str, str] = {}

    # ---- lifecycle helpers ----

    def add_banner(self, text: str) -> None:
        self.mount(Static(text, classes="banner-notice", markup=False))
        self.scroll_end(animate=False)

    def add_user(self, text: str) -> None:
        self._current_assistant = None
        self._assistant_text = ""
        self.mount(Static(f"› {text}", classes="msg-user", markup=False))
        self.scroll_end(animate=False)

    def add_system_note(self, text: str) -> None:
        self.mount(Static(text, classes="msg-reasoning", markup=False))
        self.scroll_end(animate=False)

    def add_error(self, text: str) -> None:
        self.mount(Static(f"⚠ {text}", classes="tool-result-error", markup=False))
        self.scroll_end(animate=False)

    # ---- streaming assistant text ----

    def append_text_delta(self, text: str) -> None:
        if self._current_assistant is None:
            self._current_assistant = Markdown("", classes="msg-assistant")
            self.mount(self._current_assistant)
            self._assistant_text = ""
        self._assistant_text += text
        self._current_assistant.update(self._assistant_text)
        self.scroll_end(animate=False)

    def append_reasoning_delta(self, text: str) -> None:
        # New reasoning chunk closes any in-flight assistant text block so
        # reasoning always renders as its own dim line, in order.
        self._current_assistant = None
        self.mount(Static(text, classes="msg-reasoning", markup=False))
        self.scroll_end(animate=False)

    def finish_turn(self) -> None:
        self._current_assistant = None
        self._assistant_text = ""

    # ---- tool calls ----

    def tool_call_start(self, tool_id: str, name: str) -> None:
        self._current_assistant = None
        header = Static(f"⏺ {name}(…)", classes="tool-header", markup=False)
        self._tool_headers[tool_id] = header
        self._tool_names[tool_id] = name
        self._tool_args[tool_id] = ""
        self.mount(header)
        self.scroll_end(animate=False)

    def tool_call_delta(self, tool_id: str, arguments: str) -> None:
        self._tool_args[tool_id] = self._tool_args.get(tool_id, "") + arguments

    def tool_call_end(self, tool_id: str, name: str, arguments: str) -> None:
        if name:
            self._tool_names[tool_id] = name
        if arguments:
            self._tool_args[tool_id] = arguments
        header = self._tool_headers.get(tool_id)
        display_name = self._tool_names.get(tool_id, name or "tool")
        args_preview = _preview(self._tool_args.get(tool_id, arguments))
        if header is not None:
            header.update(f"⏺ {display_name}({args_preview})")

    def tool_result(self, tool_id: str, name: str, content: str, is_error: bool) -> None:
        display_name = self._tool_names.get(tool_id, name)
        title = f"{'✗' if is_error else '✓'} {display_name} result"
        body_cls = "tool-result-error" if is_error else "tool-result-ok"
        body = Static(content, classes=body_cls, markup=False)
        collapsible = Collapsible(
            body,
            title=title,
            collapsed=not is_error,
        )
        self.mount(collapsible)
        self.scroll_end(animate=False)
