"""The scrollable conversation transcript.

Renders the normalized ``AgentEvent`` stream: streaming assistant markdown,
dim collapsible reasoning, one-line tool call headers, and collapsible tool
result bodies (auto-collapsed on success, expanded + red on error).
"""

from __future__ import annotations

from textual import events
from textual.containers import VerticalScroll
from textual.widgets import Collapsible, Markdown, Static
from textual.widgets._collapsible import CollapsibleTitle


def _preview(args: str, limit: int = 80) -> str:
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


class TranscriptCollapsibleTitle(CollapsibleTitle):
    """Toggle on mouse click without stealing focus from the composer.

    Textual provides this separate switch precisely for mouse focus. Keyboard
    focus via Tab remains available, so Enter/Esc accessibility is preserved.
    """

    FOCUS_ON_CLICK = False


class TranscriptCollapsible(Collapsible):
    """Collapsible with a title tailored to the chat transcript.

    A click anywhere on the expanded body collapses it — a fast dismiss that
    does not require aiming at the small title glyph — and, like the title,
    never steals focus from the composer.
    """

    # A click on the expanded body collapses it; like the title, the body must
    # not grab keyboard focus from the composer (the toggle is a mouse-only
    # convenience, Tab/Enter accessibility is handled by the title).
    FOCUS_ON_CLICK = False

    def __init__(self, *children, **kwargs) -> None:
        super().__init__(*children, **kwargs)
        old_title = self._title
        self._title = TranscriptCollapsibleTitle(
            label=old_title.label,
            collapsed_symbol=old_title.collapsed_symbol,
            expanded_symbol=old_title.expanded_symbol,
            collapsed=old_title.collapsed,
        )

    async def _on_click(self, event: events.Click) -> None:
        """Clicking the expanded body collapses it; clicking the title is left
        to the title's own handler (it stops the event, so this never fires for
        title clicks). The body's child widgets aren't focusable, so without
        this a click would walk up and focus the Transcript scroll container —
        we restore the composer to match the title-click behavior."""
        event.prevent_default()
        event.stop()
        if not self.collapsed:
            self.collapsed = True
            # Keep keyboard focus in the composer (the title does the same via
            # FOCUS_ON_CLICK=False; the body path needs it explicitly because
            # the click target is a non-focusable Static inside us).
            try:
                chat = self.app.query_one("#chat-input")
            except Exception:
                return
            if self.app.focused is not chat:
                chat.focus()


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
        self._tool_boxes: dict[str, TranscriptCollapsible] = {}

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

    def add_queued(self, text: str) -> None:
        """Show a queued follow-up as a dimmed user line (distinct from a live
        turn): the message will be sent when the current turn ends, so it's
        marked "queued" and visually de-emphasized."""
        self.mount(Static(f"› {text}  (queued)", classes="msg-queued", markup=False))

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
            self._current_reasoning_box = TranscriptCollapsible(
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
        full_args = self._tool_args.get(tool_id, "")
        glyph = "✗" if is_error else "✓"
        title = f"{glyph} {display_name}({full_args})" if full_args else f"{glyph} {display_name}"
        body_cls = "tool-result-error" if is_error else "tool-result-ok"
        # The expanded body shows the full command/arguments (the streaming
        # header only ever showed a truncated preview) followed by the result,
        # so a long bash command can finally be read in full. The whole body is
        # clickable to collapse again (TranscriptCollapsible._on_click).
        body_parts: list[str] = []
        if full_args:
            body_parts.append(f"$ {full_args}")
            body_parts.append("")
        body_parts.append(_clip(content))
        body = Static("\n".join(body_parts), classes=body_cls, markup=False)
        collapsible = TranscriptCollapsible(
            body,
            title=title,
            collapsed=not is_error,
        )
        # Replace the live-streaming header with the final, expandable record.
        header = self._tool_headers.pop(tool_id, None)
        self._tool_boxes[tool_id] = collapsible
        if header is not None and header.is_mounted:
            self.mount(collapsible, after=header)
            header.remove()
        else:
            # Header not flushed yet (or absent): just append the record and
            # make sure a pending header can't pop in after it.
            self.mount(collapsible)
            if header is not None:
                try:
                    header.remove()
                except Exception:
                    pass
