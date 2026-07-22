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


def _tidy(text: str) -> str:
    """Trim and collapse 3+ blank lines to one, so stray model newlines don't
    render as huge vertical gaps between wrapped lines."""
    import re

    return re.sub(r"\n{3,}", "\n\n", text.strip())


def _clip(content: str, max_lines: int = 100) -> str:
    """Cap a tool-result body for display (the model still saw the full text)."""
    lines = content.splitlines()
    if len(lines) <= max_lines:
        return content
    hidden = len(lines) - max_lines
    return "\n".join(lines[:max_lines]) + f"\n… ({hidden} more lines)"


class Transcript(VerticalScroll):
    """Container that owns the running conversation view."""

    def __init__(self, **kwargs) -> None:
        super().__init__(id="transcript", **kwargs)
        # During a turn the answer streams into a plain Static (cheap, correct
        # wrapping). At finish_turn it's swapped for one Markdown render — this
        # avoids re-parsing/rebuilding Markdown on every token, which under real
        # terminal latency left stale blocks and phantom line gaps.
        self._current_assistant: Static | None = None
        self._assistant_text: str = ""
        self._assistant_dirty: bool = False
        self._current_reasoning: Static | None = None
        self._current_reasoning_box: Collapsible | None = None
        self._reasoning_text: str = ""
        self._reasoning_dirty: bool = False
        self._tool_headers: dict[str, Static] = {}
        self._tool_names: dict[str, str] = {}
        self._tool_args: dict[str, str] = {}

    def on_mount(self) -> None:
        # Flush accumulated stream text at ~12Hz rather than re-rendering the
        # whole Markdown/Static on every token (which was O(n²) and janky).
        self.set_interval(0.08, self._flush_streams)
        # Stick to the bottom as content streams in. Textual releases the anchor
        # the moment the user scrolls up (so they can read) and re-engages it
        # automatically once they scroll back to the bottom.
        self.anchor()

    # ---- lifecycle helpers ----

    # Only add_user forces the view to the bottom (a deliberate user action,
    # re-engaging the anchor). Everything else just mounts: the anchor keeps the
    # view pinned when at the bottom and leaves it alone when scrolled up.
    def add_banner(self, text: str) -> None:
        self._reset_streams()
        self.mount(Static(text, classes="banner-notice", markup=False))

    def add_user(self, text: str) -> None:
        self._reset_streams()
        self.mount(Static(f"› {text}", classes="msg-user", markup=False))
        self.scroll_end(animate=False)

    def add_system_note(self, text: str) -> None:
        self.mount(Static(text, classes="msg-reasoning", markup=False))

    def add_error(self, text: str) -> None:
        self.mount(Static(f"⚠ {text}", classes="tool-result-error", markup=False))

    def _reset_streams(self) -> None:
        self._current_assistant = None
        self._assistant_text = ""
        self._assistant_dirty = False
        self._current_reasoning = None
        self._current_reasoning_box = None
        self._reasoning_text = ""
        self._reasoning_dirty = False

    def _flush_streams(self) -> None:
        """Render whatever text accumulated since the last tick, once the target
        widget is actually mounted."""
        st = self._current_assistant
        if self._assistant_dirty and st is not None and st.is_mounted:
            st.update(self._assistant_text)
            self._assistant_dirty = False
        rs = self._current_reasoning
        if self._reasoning_dirty and rs is not None and rs.is_mounted:
            rs.update(self._reasoning_text)
            self._reasoning_dirty = False

    # ---- streaming assistant text ----
    # Deltas only accumulate + mark dirty; the flush timer renders. No scroll_end
    # here either — the app drain loop does one anchor-aware scroll per frame.

    def append_text_delta(self, text: str) -> None:
        # Real answer text starting: render the final reasoning and collapse the
        # thinking box out of the way (still expandable by the user).
        if self._current_reasoning_box is not None:
            if self._current_reasoning is not None and self._current_reasoning.is_mounted:
                self._current_reasoning.update(self._reasoning_text)
            self._current_reasoning_box.collapsed = True
            self._current_reasoning_box = None
        self._current_reasoning = None
        if self._current_assistant is None:
            # Content streams into a plain Static (cheap, correct wrapping),
            # swapped for a single Markdown render at block end.
            self._current_assistant = Static("", classes="msg-assistant", markup=False)
            self.mount(self._current_assistant)
            self._assistant_text = ""
        self._assistant_text += text
        self._assistant_dirty = True

    def append_reasoning_delta(self, text: str) -> None:
        # Accumulate the whole reasoning stream into one wrapped, collapsible
        # block (a widget per delta rendered thinking as a narrow one-token-per-
        # line column). Starts expanded so you watch it stream; collapses when
        # the answer begins.
        self._current_assistant = None
        if self._current_reasoning is None:
            self._reasoning_text = ""
            self._current_reasoning = Static("", classes="msg-reasoning", markup=False)
            self._current_reasoning_box = Collapsible(
                self._current_reasoning,
                title="✱ Thinking",
                collapsed=False,
                classes="reasoning-box",
            )
            self.mount(self._current_reasoning_box)
        self._reasoning_text += text
        self._reasoning_dirty = True

    def _finalize_assistant(self) -> None:
        """Swap the plain streaming Static for one final Markdown render (nice
        code blocks / emphasis) built in a single parse — no mid-stream churn.
        Deferred so it also works when the Static mounted late (replay / very
        short turns)."""
        st, txt = self._current_assistant, self._assistant_text
        if st is not None:

            def _swap(st: Static = st, txt: str = txt) -> None:
                if not st.is_mounted:
                    return
                if txt.strip():
                    self.mount(Markdown(_tidy(txt), classes="msg-assistant"), after=st)
                st.remove()

            self.call_after_refresh(_swap)
        self._current_assistant = None
        self._assistant_text = ""
        self._assistant_dirty = False

    def finish_turn(self) -> None:
        self._finalize_assistant()
        rs, rtxt = self._current_reasoning, self._reasoning_text
        if rs is not None:
            self.call_after_refresh(
                lambda rs=rs, rtxt=rtxt: rs.update(rtxt) if rs.is_mounted else None
            )
        self._reset_streams()

    # ---- tool calls ----

    def tool_call_start(self, tool_id: str, name: str) -> None:
        # Any assistant text that preceded this tool call is a complete block
        # now — render it as Markdown before the tool header.
        self._finalize_assistant()
        self._current_reasoning = None
        header = Static(f"⏺ {name}(…)", classes="tool-header", markup=False)
        self._tool_headers[tool_id] = header
        self._tool_names[tool_id] = name
        self._tool_args[tool_id] = ""
        self.mount(header)

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
        # Cap the rendered body so expanding a huge file-read/log result doesn't
        # trigger a giant reflow; the full content already went to the model.
        body = Static(_clip(content), classes=body_cls, markup=False)
        collapsible = Collapsible(
            body,
            title=title,
            collapsed=not is_error,
        )
        self.mount(collapsible)
