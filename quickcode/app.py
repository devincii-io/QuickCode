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
from textual.containers import Vertical
from textual.widgets import TextArea

from quickcode.config import Config
from quickcode.core.agent import AgentInstance, PermissionOutcome, PermissionRequest
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
from quickcode.core.permissions import next_mode
from quickcode.ui.modals import HelpScreen, ModelPicker, PermissionModal
from quickcode.ui.settings import SettingsScreen
from quickcode.ui.statusbar import StatusBar
from quickcode.ui.transcript import Transcript

_THEME_PATH = Path(__file__).parent / "ui" / "theme.tcss"


class ChatInput(TextArea):
    """A TextArea where Enter submits and Ctrl+J inserts a newline."""

    class Submitted(events.Event):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            text = self.text
            self.text = ""
            self.post_message(ChatInput.Submitted(text))
            return
        if event.key == "ctrl+j":
            event.prevent_default()
            event.stop()
            self.insert("\n")
            return
        await super()._on_key(event)


class QuickCodeApp(App[None]):
    """The QuickCode terminal UI."""

    CSS_PATH = str(_THEME_PATH)

    BINDINGS = [
        Binding("shift+tab", "cycle_mode", "Cycle mode", show=True, priority=True),
        Binding("f1", "show_help", "Help", show=True),
        Binding("f2", "show_model_picker", "Model", show=True),
        Binding("f3", "show_settings", "Settings", show=True),
        Binding("ctrl+c", "clear_or_quit", "Clear/Quit", show=True),
        Binding("escape", "interrupt", "Interrupt", show=True),
    ]

    def __init__(self, agent: AgentInstance, config: Config, *, allow_yolo: bool = False,
                 startup_notice: str | None = None, initial_prompt: str | None = None) -> None:
        super().__init__()
        self.agent = agent
        self.config = config
        self.allow_yolo = allow_yolo
        self._startup_notice = startup_notice
        self._initial_prompt = initial_prompt
        self._ctrl_c_armed = False
        self._bus_queue = None
        self._tool_names: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Transcript()
        with Vertical(id="input-area"):
            yield ChatInput(id="chat-input", show_line_numbers=False)
        yield StatusBar(id="status-bar")

    def on_mount(self) -> None:
        self.agent.permission_cb = self._permission_cb
        self._bus_queue = self.agent.bus.subscribe()
        status = self.query_one("#status-bar", StatusBar)
        status.model = self.agent.model
        status.mode = self.agent.mode
        transcript = self.query_one(Transcript)
        if self._startup_notice:
            transcript.add_banner(self._startup_notice)
        self.set_interval(1 / 30, self._drain_bus)
        self.query_one("#chat-input", ChatInput).focus()
        if self._initial_prompt:
            self._submit(self._initial_prompt)
            self._initial_prompt = None

    # ------------------------------------------------------------------
    # Input handling
    # ------------------------------------------------------------------

    def on_chat_input_submitted(self, message: ChatInput.Submitted) -> None:
        self._submit(message.text)

    def _submit(self, text: str) -> None:
        text = text.strip()
        if not text or self.agent.busy:
            return
        transcript = self.query_one(Transcript)
        transcript.add_user(text)
        self.run_worker(self._run_turn(text), exclusive=False, group="turn")

    async def _run_turn(self, text: str) -> None:
        status = self.query_one("#status-bar", StatusBar)
        status.agent_state = "sending"
        try:
            await self.agent.run_turn(text)
        finally:
            transcript = self.query_one(Transcript)
            transcript.finish_turn()
            status.agent_state = "idle"

    # ------------------------------------------------------------------
    # Bus draining (~30fps)
    # ------------------------------------------------------------------

    def _drain_bus(self) -> None:
        if self._bus_queue is None:
            return
        transcript = self.query_one(Transcript)
        status = self.query_one("#status-bar", StatusBar)
        drained = 0
        while drained < 200:
            try:
                ev = self._bus_queue.get_nowait()
            except Exception:
                break
            drained += 1
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
                transcript.add_system_note(f"[error] {ev.error}")
            status.update_usage(self.agent.context_pct(), self.agent.ledger.cost_usd)
        elif isinstance(ev, AgentStatus):
            status.agent_state = ev.state
            if ev.detail:
                transcript.add_system_note(ev.detail)

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
        try:
            models = await self.agent.provider.list_models()
            ids = [m.id for m in models if m.id]
        except Exception:
            ids = []
        if not ids:
            profile = self.config.profile
            ids = sorted({e.id for e in profile.catalog} | {
                profile.orchestrator_model, profile.worker_model,
            })
        picked = await self.push_screen_wait(ModelPicker(ids, current=self.agent.model))
        if picked:
            self.agent.model = picked
            self.query_one("#status-bar", StatusBar).model = picked
            self.query_one(Transcript).add_system_note(f"model -> {picked}")

    def action_show_settings(self) -> None:
        self.push_screen(SettingsScreen(config=self.config, agent=self.agent))

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
            self.query_one(Transcript).add_system_note("(interrupted)")
