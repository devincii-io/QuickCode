"""QuickCodeApp: the Textual TUI entry point.

Owns one ``AgentInstance``, subscribes to its event bus, and renders the
stream into the transcript. Supplies ``permission_cb`` so the agent loop can
await a modal decision from here.
"""

from __future__ import annotations

from pathlib import Path

from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Static, TextArea

from quickcode.config import Config
from quickcode.core.agent import (
    AgentInstance,
    PermissionOutcome,
    PermissionRequest,
    PlanOutcome,
)
from quickcode.core.compact import run_compaction, should_compact
from quickcode.core.events import (
    AgentStatus,
    ReasoningDelta,
    TextDelta,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
    ToolResultEvent,
    TurnDone,
    Usage,
)
from quickcode.core.permissions import Mode, next_mode
from quickcode.ui.agent_pane import AgentPanes
from quickcode.ui.agents_modal import AgentsScreen
from quickcode.ui.modals import HelpScreen, ModelPicker, PermissionModal
from quickcode.ui.palette import THEME_NAME, build_theme
from quickcode.ui.plan_modal import PlanDecision, PlanReviewModal
from quickcode.ui.settings import SettingsScreen
from quickcode.ui.slashmenu import SlashMenu, command_takes_args
from quickcode.ui.statusbar import StatusBar
from quickcode.ui.transcript import Transcript
from quickcode.ui.usage_modal import UsageScreen

_THEME_PATH = Path(__file__).parent / "ui" / "theme.tcss"


def _coalesce(events: list) -> list:
    """Merge a drained batch so one frame does minimal DOM work.

    Consecutive text/reasoning deltas fold into one; per-id tool-arg deltas fold
    per id; only the last Usage in the batch survives (they're cumulative).
    """
    out: list = []
    for ev in events:
        if isinstance(ev, TextDelta) and out and isinstance(out[-1], TextDelta):
            out[-1] = TextDelta(out[-1].text + ev.text)
        elif isinstance(ev, ReasoningDelta) and out and isinstance(out[-1], ReasoningDelta):
            out[-1] = ReasoningDelta(out[-1].text + ev.text)
        elif (
            isinstance(ev, ToolCallDelta)
            and out
            and isinstance(out[-1], ToolCallDelta)
            and out[-1].id == ev.id
        ):
            out[-1] = ToolCallDelta(ev.id, out[-1].arguments + ev.arguments)
        else:
            out.append(ev)
    last_usage = next((e for e in reversed(out) if isinstance(e, Usage)), None)
    if last_usage is not None:
        out = [e for e in out if not isinstance(e, Usage)]
        out.append(last_usage)
    return out


def _explain_error(error: str, agent: AgentInstance, api_key_env: str = "") -> str:
    """Turn a raw provider error into a one-line message with a next step.

    Raw provider payloads can be huge (OpenRouter nests a repeated
    ``previous_errors`` array), so the fallback is always truncated.
    """
    low = error.lower()
    short = error if len(error) <= 160 else error[:157].rstrip() + "…"
    if "401" in error or "authentication" in low or "api key" in low or "unauthorized" in low:
        var = api_key_env or "your provider API key env var"
        return (
            f"Auth failed (401). No valid API key. Set {var} and restart, "
            f"or change the provider in Settings (F3 -> Profile)."
        )
    if "402" in error or "requires more credits" in low or "insufficient" in low:
        return (
            "Out of credits (402): this turn's token budget exceeds your OpenRouter "
            "balance. Add credits at openrouter.ai/credits (or switch to a cheaper "
            "model with F2), then retry."
        )
    if "404" in error or ("model" in low and "not" in low):
        return f"Model '{agent.model}' was rejected (404). Pick another with F2. ({short})"
    if "429" in error or "rate" in low:
        return "Rate limited (429). Wait a moment and retry."
    if "connect" in low or "timeout" in low or "network" in low:
        return "Network error reaching the provider. Check your connection."
    return f"Request failed: {short}"


class ChatInput(TextArea):
    """A TextArea where Enter submits and Ctrl+J inserts a newline.

    While the slash-command menu is open, ↑/↓/Tab/Enter/Esc drive the menu
    instead of the text area. When the input is empty and the subagent list
    is showing, a plain Down hands focus off to ``AgentPanes`` instead of
    moving the (empty) cursor.

    ↑ at the top line recalls the previous submitted input (shell-style
    history); ↓ at the bottom line moves forward. The in-progress draft is
    preserved while browsing and restored when you move past either end.
    """

    class Submitted(events.Event):
        def __init__(self, text: str, queued: bool = False) -> None:
            super().__init__()
            self.text = text
            # True when the app queued this submit because the agent was busy;
            # the app uses it to avoid double-queuing on replay.
            self.queued = queued

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._history: list[str] = []
        self._history_index: int = -1  # -1 = not browsing; otherwise cursor into _history
        self._draft: str = ""  # the in-progress text saved when entering history

    def _menu(self) -> SlashMenu | None:
        try:
            menu = self.app.query_one(SlashMenu)
        except Exception:
            return None
        return menu if menu.is_open else None

    def _agent_panes(self) -> AgentPanes | None:
        try:
            return self.app.query_one(AgentPanes)
        except Exception:
            return None

    async def _on_key(self, event: events.Key) -> None:
        menu = self._menu()
        if menu is not None:
            if event.key in ("up", "down"):
                event.prevent_default()
                event.stop()
                (menu.action_cursor_up if event.key == "up" else menu.action_cursor_down)()
                return
            if event.key in ("enter", "tab"):
                event.prevent_default()
                event.stop()
                self.app.accept_slash_command()
                return
            if event.key == "escape":
                event.prevent_default()
                event.stop()
                menu.hide()
                return
        if event.key == "down" and self.text == "":
            panes = self._agent_panes()
            if panes is not None and panes.display:
                event.prevent_default()
                event.stop()
                panes.focus()
                return
        # Shell-style history: Up at the first line recalls the previous
        # submitted input; Down at the last line moves forward. Multi-line
        # drafts keep the cursor moving within the draft until it reaches the
        # top/bottom edge, so normal editing is unaffected.
        if event.key == "up" and self.cursor_location[0] == 0 and not self.text.startswith("/"):
            if self._history_index == -1:
                self._draft = self.text
                self._history_index = len(self._history)
            if self._history_index > 0:
                self._history_index -= 1
                self.load_text(self._history[self._history_index])
                self.move_cursor((0, 0))
            event.prevent_default()
            event.stop()
            return
        if event.key == "down" and self.cursor_location[0] == self.document.line_count - 1 and not self.text.startswith("/"):
            if self._history_index != -1:
                self._history_index += 1
                if self._history_index >= len(self._history):
                    # Past the newest: restore the in-progress draft.
                    self._history_index = -1
                    self.load_text(self._draft)
                else:
                    self.load_text(self._history[self._history_index])
                self.move_cursor((self.document.line_count - 1, 0))
                event.prevent_default()
                event.stop()
                return
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            text = self.text
            self.text = ""
            # Record into history: skip empty and pure-slash commands (those
            # are handled by the command menu, not worth recalling). Dedup only
            # on exact repeats so a re-phrased retry is still logged.
            if text and not text.startswith("/"):
                if not (self._history and self._history[-1] == text):
                    self._history.append(text)
            self._history_index = -1
            self._draft = ""
            self.post_message(ChatInput.Submitted(text))
            return
        if event.key == "ctrl+j":
            event.prevent_default()
            event.stop()
            self.insert("\n")
            return
        # Ctrl+V (inherited from TextArea) reads the OS clipboard; some terminals
        # swallow Ctrl+V or map it to something else, so Alt+V is an explicit
        # alias that always reaches us. Bracketed-paste (terminal "right-click
        # paste") is handled by _on_paste independently of either binding.
        if event.key == "alt+v":
            event.prevent_default()
            event.stop()
            self.action_paste()
            return
        if event.key == "tab":
            # Move focus out of the input into the transcript (collapsibles,
            # scroll) rather than inserting an indent. Shift+Tab / Esc come back.
            event.prevent_default()
            event.stop()
            self.screen.focus_next()
            return
        await super()._on_key(event)


class QuickCodeApp(App[None]):
    """The QuickCode terminal UI."""

    CSS_PATH = str(_THEME_PATH)

    BINDINGS = [
        Binding("shift+tab", "cycle_mode", "Cycle mode", show=True, priority=True),
        Binding("ctrl+b", "toggle_sidebar", "Sidebar", show=True),
        Binding("f1", "show_help", "Help", show=True),
        Binding("f2", "show_model_picker", "Model", show=True),
        Binding("f3", "show_settings", "Settings", show=True),
        Binding("ctrl+c", "clear_or_quit", "Clear/Quit", show=True),
        Binding("escape", "interrupt", "Interrupt", show=True),
        # Keyboard-only transcript scrolling (work even while the input is focused).
        Binding("pageup", "scroll_transcript('pageup')", "Scroll up", show=False, priority=True),
        Binding("pagedown", "scroll_transcript('pagedown')", "Scroll down", show=False, priority=True),
        Binding("ctrl+home", "scroll_transcript('home')", "Top", show=False, priority=True),
        Binding("ctrl+end", "scroll_transcript('end')", "Bottom", show=False, priority=True),
        # A transcript control reached with Tab should have an intuitive path
        # back to composing. TextArea consumes Down itself while it has focus.
        Binding("down", "focus_input_from_transcript", "Return to input", show=False),
        # Keyboard-only subagent pane navigation (no-ops when no panes exist).
        Binding("ctrl+right", "pane_next", "Next pane", show=False, priority=True),
        Binding("ctrl+left", "pane_prev", "Prev pane", show=False, priority=True),
        Binding("ctrl+down", "pane_next", "Next pane", show=False, priority=True),
        Binding("ctrl+up", "pane_prev", "Prev pane", show=False, priority=True),
        Binding("ctrl+e", "pane_expand", "Expand pane", show=False, priority=True),
        Binding("ctrl+w", "pane_close_finished", "Dismiss done panes", show=False, priority=True),
    ]

    def __init__(self, agent: AgentInstance, config: Config, *, allow_yolo: bool = False,
                 startup_notice: str | None = None, initial_prompt: str | None = None,
                 session_store=None, env=None) -> None:
        super().__init__()
        self.agent = agent
        self.config = config
        self.allow_yolo = allow_yolo
        self._startup_notice = startup_notice
        self._initial_prompt = initial_prompt
        self._env = env  # for re-rendering the system-prompt identity on model switch
        self._ctrl_c_armed = False
        self._bus_queue = None
        self._tool_names: dict[str, str] = {}
        self._store = session_store
        self._persisted = 0  # count of history messages already written to disk
        # Messages submitted while the agent was mid-turn; sent FIFO once the
        # current turn ends. Lets the user steer the conversation or queue the
        # next task without waiting for the stream to finish.
        self._queue: list[str] = []

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Horizontal(id="body"):
            yield Transcript()
            yield AgentPanes(id="agent-panes")
            yield Static("", id="sidebar", markup=False)
        yield SlashMenu(id="slash-menu")
        with Vertical(id="input-area"):
            yield ChatInput(id="chat-input", show_line_numbers=False)
        yield StatusBar(id="status-bar")

    def on_mount(self) -> None:
        self.register_theme(build_theme(self.config.theme_colors()))
        self.theme = THEME_NAME
        self.agent.permission_cb = self._permission_cb
        self.agent.plan_cb = self._plan_cb
        self._bus_queue = self.agent.bus.subscribe(maxsize=0)  # unbounded UI queue
        status = self.query_one("#status-bar", StatusBar)
        status.model = self.agent.model
        status.cwd = str(self.agent.ctx.cwd)
        status.mode = self.agent.mode
        self.query_one("#sidebar", Static).display = False
        # Route subagent spawns to live panes. The deps object is shared across
        # the whole agent tree, so setting the hook once reaches every child.
        deps = self.agent.ctx.extra.get("subagent") if self.agent.ctx else None
        if deps is not None:
            deps.on_pane = self._on_subagent_pane
        self.query_one("#input-area").border_title = "▌ message"
        transcript = self.query_one(Transcript)
        # Replay any resumed history into the transcript.
        self._replay_history(transcript)
        if self._startup_notice:
            transcript.add_banner(self._startup_notice)
        self.set_interval(1 / 30, self._drain_bus)
        # The ctx meter needs the model's context window; fetch it off-thread
        # so startup never blocks on the network.
        self.run_worker(self._resolve_context_length(), group="ctx-length", exclusive=True)
        self.query_one("#chat-input", ChatInput).focus()
        if self._initial_prompt:
            self._submit(self._initial_prompt)
            self._initial_prompt = None

    def _replay_history(self, transcript: Transcript) -> None:
        """Render resumed messages (from --continue/--resume) into the view.

        Batched so a long session doesn't repaint per message on startup.
        """
        msgs = self.agent.history.messages
        with self.batch_update():
            for m in msgs:
                if m.role == "user" and m.content:
                    transcript.add_user(m.content.split("\n<system-reminder>")[0])
                elif m.role == "assistant" and m.content:
                    transcript.append_text_delta(m.content)
                    transcript.finish_turn()
        self._persisted = len(msgs)  # already on disk; don't re-append

    # ------------------------------------------------------------------
    # Input handling
    # ------------------------------------------------------------------

    def on_chat_input_submitted(self, message: ChatInput.Submitted) -> None:
        self.query_one(SlashMenu).hide()
        self._submit(message.text, queued=message.queued)

    def on_text_area_changed(self, event) -> None:
        # Show/refresh the slash-command popup as the user types a leading "/".
        try:
            menu = self.query_one(SlashMenu)
        except Exception:
            return
        menu.show_for(self.query_one("#chat-input", ChatInput).text)

    def accept_slash_command(self) -> None:
        """Complete the highlighted slash command: arg-taking commands fill the
        input and wait; the rest run immediately."""
        menu = self.query_one(SlashMenu)
        cmd = menu.selected_command()
        menu.hide()
        if cmd is None:
            return
        chat = self.query_one("#chat-input", ChatInput)
        if command_takes_args(cmd):
            chat.load_text(cmd + " ")
            chat.move_cursor(chat.document.end)
            chat.focus()
        else:
            chat.load_text("")
            self._handle_command(cmd)

    _COMMANDS = {
        "/help": "show this help / keybindings",
        "/model": "open the model picker",
        "/settings": "open settings (models, usage, permissions, profile)",
        "/agents": "show subagent definitions the model can delegate to",
        "/usage": "show token & cost usage for this session",
        "/mode": "/mode <plan|ask|auto-edit|yolo> — set permission mode",
        "/tasks": "toggle the task board sidebar",
        "/compact": "compress the conversation to free up context",
        "/clear": "clear the conversation and start fresh",
        "/quit": "exit QuickCode",
    }

    def _submit(self, text: str, *, queued: bool = False) -> None:
        text = text.strip()
        if not text:
            return
        # Slash commands are UI, not model turns — run immediately even while
        # the agent is mid-stream (so /mode, /compact, /clear take effect now).
        if text.startswith("/"):
            self._handle_command(text)
            return
        if self.agent.busy:
            self._queue.append(text)
            transcript = self.query_one(Transcript)
            transcript.add_queued(text)
            self._refresh_queue_indicator()
            return
        transcript = self.query_one(Transcript)
        transcript.add_user(text)
        self.run_worker(self._run_turn(text), exclusive=False, group="turn")

    def _handle_command(self, text: str) -> None:
        transcript = self.query_one(Transcript)
        parts = text.split()
        cmd, args = parts[0].lower(), parts[1:]
        if cmd in ("/help", "/"):
            lines = "\n".join(f"  {c:<10} {d}" for c, d in self._COMMANDS.items())
            transcript.add_system_note("Commands:\n" + lines)
        elif cmd == "/model":
            self.action_show_model_picker()
        elif cmd == "/settings":
            self.action_show_settings()
        elif cmd == "/agents":
            self.action_show_agents()
        elif cmd == "/usage":
            self.action_show_usage()
        elif cmd == "/tasks":
            self.action_toggle_sidebar()
        elif cmd == "/compact":
            self.run_worker(self._do_compact(manual=True), exclusive=True, group="compact")
        elif cmd == "/clear":
            self.agent.history.messages.clear()
            self._persisted = 0
            transcript.remove_children()
            transcript.add_system_note("(conversation cleared)")
        elif cmd == "/quit":
            self.exit()
        elif cmd == "/mode":
            self._set_mode_by_name(args[0] if args else "")
        else:
            transcript.add_system_note(f"Unknown command: {cmd}. Type /help.")

    def _set_mode_by_name(self, name: str) -> None:
        transcript = self.query_one(Transcript)
        try:
            mode = Mode(name)
        except ValueError:
            transcript.add_system_note(
                f"Usage: /mode <plan|ask|auto-edit|yolo>. Got '{name}'."
            )
            return
        if mode == Mode.yolo and not self.allow_yolo:
            transcript.add_system_note("yolo mode requires launching with --yolo.")
            return
        self.agent.set_mode(mode)
        self.query_one("#status-bar", StatusBar).mode = mode
        transcript.add_system_note(f"mode -> {mode.value}")

    async def _run_turn(self, text: str) -> None:
        status = self.query_one("#status-bar", StatusBar)
        status.agent_state = "sending"
        try:
            await self.agent.run_turn(text)
        finally:
            # Drain any events still queued from the last stream chunk BEFORE
            # finalizing, so the whole answer is present when the streaming
            # Static is swapped for its final Markdown render.
            self._drain_bus()
            transcript = self.query_one(Transcript)
            transcript.finish_turn()
            status.agent_state = "idle"
            self._persist_new_messages()
            self._refresh_sidebar()
            self._refresh_queue_indicator()
            if should_compact(self.agent):
                self.run_worker(self._do_compact(manual=False), exclusive=True, group="compact")
            # If the user queued follow-ups while the agent was busy, send the
            # next one now (FIFO). Compaction (above) may itself run async; it
            # doesn't set agent.busy, so a queued item can interleave safely —
            # it just starts a new turn once the compaction worker yields.
            self._drain_queue()

    def _drain_queue(self) -> None:
        """Send the next queued message if the agent is idle. Called at the end
        of each turn and after a manual interrupt. No-op if the queue is empty
        or the agent is busy again (the next turn-end will retry)."""
        if not self._queue or self.agent.busy:
            return
        next_text = self._queue.pop(0)
        transcript = self.query_one(Transcript)
        transcript.add_user(next_text)
        self._refresh_queue_indicator()
        self.run_worker(self._run_turn(next_text), exclusive=False, group="turn")

    def _refresh_queue_indicator(self) -> None:
        """Keep the status bar's queued-count segment in sync."""
        try:
            status = self.query_one("#status-bar", StatusBar)
        except Exception:
            return
        status.queued = len(self._queue)

    def _persist_new_messages(self) -> None:
        if self._store is None:
            return
        msgs = self.agent.history.messages
        for m in msgs[self._persisted :]:
            try:
                self._store.append_message(m)
            except Exception:
                break
        self._persisted = len(msgs)

    async def _do_compact(self, *, manual: bool) -> None:
        transcript = self.query_one(Transcript)
        transcript.add_system_note("Compacting conversation…")
        try:
            await run_compaction(self.agent)
        except Exception as e:  # noqa: BLE001
            transcript.add_error(f"Compaction failed: {e}")
            return
        # History was rebuilt; re-sync the transcript and the persisted counter.
        transcript.remove_children()
        transcript.add_system_note("(conversation compacted — earlier turns summarized)")
        self._persisted = len(self.agent.history.messages)
        self.query_one("#status-bar", StatusBar).update_usage(
            self.agent.context_pct(), self.agent.ledger.cost_usd
        )

    def _refresh_sidebar(self) -> None:
        board = self.agent.ctx.extra.get("task_board") if self.agent.ctx else None
        sidebar = self.query_one("#sidebar", Static)
        if board is None:
            return
        text = board.render_checklist()
        sidebar.update(f"Tasks\n─────\n{text}")

    # ------------------------------------------------------------------
    # Bus draining (~30fps)
    # ------------------------------------------------------------------

    def _drain_bus(self) -> None:
        if self._bus_queue is None:
            return
        # Pull the whole pending batch first; if there's nothing, don't touch DOM.
        events_batch = []
        while True:
            try:
                events_batch.append(self._bus_queue.get_nowait())
            except Exception:
                break
        if not events_batch:
            return
        # The transcript/status may be gone mid-teardown or during a screen
        # transition; skip this tick rather than crashing the interval.
        try:
            transcript = self.query_one(Transcript)
            status = self.query_one("#status-bar", StatusBar)
        except Exception:
            return
        # Coalesce so a burst of N token deltas becomes ~one DOM write per tick.
        # Scrolling is handled by the transcript's anchor (Transcript.on_mount):
        # it sticks to the bottom until the user scrolls up and re-engages when
        # they scroll back down.
        with self.batch_update():
            for ev in _coalesce(events_batch):
                self._apply_event(ev, transcript, status)

    def _apply_event(self, ev, transcript: Transcript, status: StatusBar) -> None:
        if isinstance(ev, TextDelta):
            transcript.append_text_delta(ev.text)
        elif isinstance(ev, ReasoningDelta):
            transcript.append_reasoning_delta(ev.text)
        elif isinstance(ev, ToolCallStart):
            self._tool_names[ev.id] = ev.name
            transcript.tool_call_start(ev.id, ev.name)
        elif isinstance(ev, ToolCallDelta):
            transcript.tool_call_delta(ev.id, ev.arguments)
        elif isinstance(ev, ToolCallEnd):
            transcript.tool_call_end(ev.id, ev.name, ev.arguments)
        elif isinstance(ev, ToolResultEvent):
            transcript.tool_result(ev.id, ev.name, ev.content, ev.is_error)
        elif isinstance(ev, Usage):
            status.update_usage(self.agent.context_pct(), self.agent.ledger.cost_usd)
        elif isinstance(ev, TurnDone):
            if ev.error:
                transcript.add_error(
                    _explain_error(ev.error, self.agent, self.config.profile.api_key_env)
                )
            status.update_usage(self.agent.context_pct(), self.agent.ledger.cost_usd)
        elif isinstance(ev, AgentStatus):
            status.agent_state = ev.state
            # detail is only used for errors, which TurnDone already renders;
            # do not add a second note here.

    # ------------------------------------------------------------------
    # Permission bridge
    # ------------------------------------------------------------------

    async def _permission_cb(self, request: PermissionRequest) -> PermissionOutcome:
        modal = PermissionModal(request)
        outcome = await self.push_screen_wait(modal)
        return outcome if outcome is not None else PermissionOutcome(allow=False, deny_message="dismissed")

    # ------------------------------------------------------------------
    # Actions / bindings
    # ------------------------------------------------------------------

    def action_cycle_mode(self) -> None:
        # Shift+Tab is overloaded: when you've tabbed into the transcript it
        # walks focus backward; only in the input does it cycle permission mode.
        chat = self.query_one("#chat-input", ChatInput)
        if self.focused is not chat:
            self.screen.focus_previous()
            return
        new_mode = next_mode(self.agent.mode, self.allow_yolo)
        self.agent.set_mode(new_mode)
        status = self.query_one("#status-bar", StatusBar)
        status.mode = new_mode
        transcript = self.query_one(Transcript)
        transcript.add_system_note(f"mode -> {new_mode.value}")

    def action_show_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_show_model_picker(self) -> None:
        self.run_worker(self._open_model_picker(), exclusive=True, group="model-picker")

    async def _open_model_picker(self) -> None:
        catalog_ids = [e.id for e in self.config.profile.catalog]
        picked = await self.push_screen_wait(
            ModelPicker(self.agent.provider, current=self.agent.model, catalog_ids=catalog_ids)
        )
        if picked and picked != self.agent.model:
            had_history = any(m.role != "system" for m in self.agent.history.messages)
            self.agent.model = picked
            # The old window no longer applies; re-resolve for the new model.
            self.agent.context_length = None
            self.run_worker(
                self._resolve_context_length(), group="ctx-length", exclusive=True
            )
            self.query_one("#status-bar", StatusBar).model = picked
            # Refresh the identity in the system prompt so the model reports what
            # it actually is now (the switch already invalidated the cache).
            self._refresh_identity(picked)
            # Persist the choice so the next session starts on this model.
            self.config.last_model = picked
            try:
                self.config.save()
            except Exception:
                pass
            transcript = self.query_one(Transcript)
            transcript.add_system_note(f"model -> {picked} (saved as default)")
            if had_history:
                transcript.add_system_note(
                    "Note: switching model mid-conversation re-reads the entire history "
                    "with the new model and gets no prompt-cache benefit — the next turn "
                    "will be slower and cost more input tokens."
                )

    async def _resolve_context_length(self) -> None:
        """Look up the current model's context window from the provider's model
        list so ``context_pct`` has a denominator (cli builds with None)."""
        try:
            models = await self.agent.provider.list_models()
        except Exception:
            return
        model = self.agent.model
        length = next((m.context_length for m in models if m.id == model), None)
        if length and self.agent.model == model:
            self.agent.context_length = length
            try:
                status = self.query_one("#status-bar", StatusBar)
                status.update_usage(self.agent.context_pct(), self.agent.ledger.cost_usd)
            except Exception:
                pass

    def _refresh_identity(self, model: str) -> None:
        if self._env is None:
            return
        from quickcode.prompts.system import render_system_prompt

        base = self.config.profile.base_url
        provider_name = "OpenRouter" if "openrouter.ai" in base else base
        try:
            self.agent.history.set_system_prompt(
                render_system_prompt(
                    self._env,
                    model=model,
                    provider=provider_name,
                    plan=(self.agent.mode == Mode.plan),
                    # Must match the CLI's initial render — dropping this here
                    # would silently strip the subagent guidance on a switch.
                    orchestration=True,
                )
            )
        except Exception:
            pass

    def action_show_agents(self) -> None:
        deps = self.agent.ctx.extra.get("subagent") if self.agent.ctx else None
        spawned = list(deps.spawned) if deps is not None else []
        self.push_screen(AgentsScreen(cwd=self.agent.ctx.cwd, spawned=spawned))

    def action_show_settings(self) -> None:
        self.push_screen(SettingsScreen(config=self.config, agent=self.agent, app_ref=self))

    def action_show_usage(self) -> None:
        deps = self.agent.ctx.extra.get("subagent") if self.agent.ctx else None
        spawned = list(deps.spawned) if deps is not None else []
        self.push_screen(
            UsageScreen(
                model=self.agent.model,
                ledger=self.agent.ledger,
                context_pct=self.agent.context_pct(),
                spawned=spawned,
            )
        )

    def apply_theme(self, colors: dict[str, str]) -> None:
        """Rebuild and re-apply the theme live from an edited color map.

        Re-registering under the same name overwrites the stored Theme object;
        ``_watch_theme`` then re-reads ``current_theme`` and refreshes the CSS,
        so edits show instantly without restarting the app.
        """
        self.register_theme(build_theme(colors))
        self._watch_theme(THEME_NAME)

    def action_toggle_sidebar(self) -> None:
        sidebar = self.query_one("#sidebar", Static)
        sidebar.display = not sidebar.display
        if sidebar.display:
            self._refresh_sidebar()

    # ------------------------------------------------------------------
    # Subagent panes
    # ------------------------------------------------------------------

    def _on_subagent_pane(self, agent_id: str, agent_type: str, bus) -> None:
        """Runner hook (on the event loop thread): open a live pane for a
        freshly spawned subagent. Best-effort — never raise back into the run."""
        try:
            self.query_one(AgentPanes).add_pane(agent_id, agent_type, bus)
        except Exception:
            pass

    def _panes(self) -> AgentPanes | None:
        try:
            return self.query_one(AgentPanes)
        except Exception:
            return None

    def action_pane_next(self) -> None:
        if (p := self._panes()) is not None:
            p.focus_next()

    def action_pane_prev(self) -> None:
        if (p := self._panes()) is not None:
            p.focus_prev()

    def action_pane_expand(self) -> None:
        if (p := self._panes()) is not None:
            p.toggle_expand()

    def action_pane_close_finished(self) -> None:
        if (p := self._panes()) is not None:
            p.close_finished()

    async def _plan_cb(self, plan_markdown: str) -> PlanOutcome:
        result = await self.push_screen_wait(PlanReviewModal(plan_markdown))
        if result is None or result.decision == PlanDecision.KEEP_PLANNING:
            fb = result.feedback if result else ""
            return PlanOutcome(approved=False, feedback=fb)
        mode_after = (
            Mode.auto_edit if result.decision == PlanDecision.APPROVE_AUTO else Mode.ask
        )
        self.query_one("#status-bar", StatusBar).mode = mode_after
        return PlanOutcome(approved=True, mode_after=mode_after)

    def action_clear_or_quit(self) -> None:
        chat_input = self.query_one("#chat-input", ChatInput)
        if chat_input.text:
            chat_input.text = ""
            self._ctrl_c_armed = False
            return
        if self._ctrl_c_armed:
            self.exit()
        else:
            self._ctrl_c_armed = True
            self.query_one(Transcript).add_system_note("(press Ctrl+C again to quit)")

    def action_interrupt(self) -> None:
        if self.agent.busy:
            self.agent.cancel()
            # Drop any queued follow-ups too: an interrupt means "stop, I've
            # changed my mind", not "stop then run the rest of my plan".
            if self._queue:
                n = len(self._queue)
                self._queue.clear()
                self._refresh_queue_indicator()
                self.query_one(Transcript).add_system_note(
                    f"(interrupted; {n} queued message{'s' if n != 1 else ''} cleared)"
                )
            else:
                self.query_one(Transcript).add_system_note("(interrupted)")
            return
        # Idle: if there are queued messages (agent finished but drain pending),
        # Esc clears them; otherwise Esc pulls focus back to the input.
        if self._queue:
            n = len(self._queue)
            self._queue.clear()
            self._refresh_queue_indicator()
            self.query_one(Transcript).add_system_note(
                f"(cleared {n} queued message{'s' if n != 1 else ''})"
            )
            return
        chat = self.query_one("#chat-input", ChatInput)
        if self.focused is not chat:
            chat.focus()

    def action_scroll_transcript(self, where: str) -> None:
        t = self.query_one(Transcript)
        if where == "pageup":
            t.scroll_page_up(animate=False)
        elif where == "pagedown":
            t.scroll_page_down(animate=False)
        elif where == "home":
            t.scroll_home(animate=False)
        else:
            t.scroll_end(animate=False)

    def action_focus_input_from_transcript(self) -> None:
        """Down from any focused transcript control returns to the composer."""
        transcript = self.query_one(Transcript)
        focused = self.focused
        if focused is transcript or (focused is not None and transcript in focused.ancestors):
            self.query_one("#chat-input", ChatInput).focus()

    def on_resize(self, event: events.Resize) -> None:
        # After a terminal resize the layout reflows via CSS. Only re-pin to the
        # bottom if the user was already there — don't yank them out of scrollback.
        try:
            transcript = self.query_one(Transcript)
        except Exception:
            return
        if transcript.is_vertical_scroll_end:
            transcript.scroll_end(animate=False)
