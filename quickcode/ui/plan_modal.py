"""Plan review modal: shown when the agent submits a plan in plan mode."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Markdown


class PlanDecision(Enum):
    """Outcome of a plan review."""

    APPROVE_AUTO = "approve_auto"
    APPROVE_MANUAL = "approve_manual"
    KEEP_PLANNING = "keep_planning"


@dataclass
class PlanReviewResult:
    """Result dismissed by PlanReviewModal."""

    decision: PlanDecision
    feedback: str = ""


class PlanReviewModal(ModalScreen[PlanReviewResult | None]):
    """Presents the agent's plan and asks the user how to proceed.

    Keys: a = approve & drop to auto-edit, m = approve & drop to ask mode,
    k = reveal (then submit) the "keep planning" feedback box,
    escape = keep planning with no feedback.
    """

    DEFAULT_CSS = """
    PlanReviewModal {
        align: center middle;
    }

    PlanReviewModal > Vertical {
        width: 80%;
        height: 80%;
        border: thick $primary;
        background: $panel;
        padding: 1 2;
    }

    PlanReviewModal #plan-scroll {
        height: 1fr;
        border: solid $primary-darken-2;
        background: $surface;
        margin: 1 0;
    }

    PlanReviewModal #feedback-input {
        display: none;
        margin: 1 0 0 0;
    }
    """

    BINDINGS = [
        ("a", "approve_auto", "Approve + auto-edit"),
        ("m", "approve_manual", "Approve, manual"),
        ("k", "keep_planning", "Keep planning"),
        ("escape", "cancel", "Keep planning (no feedback)"),
    ]

    def __init__(self, plan_markdown: str) -> None:
        super().__init__()
        self._plan_markdown = plan_markdown
        self._feedback_revealed = False

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Plan review", classes="tool-header")
            with VerticalScroll(id="plan-scroll"):
                yield Markdown(self._plan_markdown)
            with Horizontal():
                yield Button("Approve + auto-edit (a)", id="approve-auto", variant="success")
                yield Button("Approve, manual (m)", id="approve-manual", variant="primary")
                yield Button("Keep planning (k)", id="keep-planning", variant="warning")
            yield Input(placeholder="Feedback for the agent, Enter to submit", id="feedback-input")

    def on_mount(self) -> None:
        self.query_one("#feedback-input", Input).display = False

    def action_approve_auto(self) -> None:
        self.dismiss(PlanReviewResult(PlanDecision.APPROVE_AUTO))

    def action_approve_manual(self) -> None:
        self.dismiss(PlanReviewResult(PlanDecision.APPROVE_MANUAL))

    def action_keep_planning(self) -> None:
        feedback_input = self.query_one("#feedback-input", Input)
        if not self._feedback_revealed:
            self._feedback_revealed = True
            feedback_input.display = True
            feedback_input.focus()
            return
        self.dismiss(PlanReviewResult(PlanDecision.KEEP_PLANNING, feedback_input.value))

    def action_cancel(self) -> None:
        self.dismiss(PlanReviewResult(PlanDecision.KEEP_PLANNING, ""))

    @on(Input.Submitted, "#feedback-input")
    def _submit_feedback(self, event: Input.Submitted) -> None:
        self.dismiss(PlanReviewResult(PlanDecision.KEEP_PLANNING, event.value))

    @on(Button.Pressed, "#approve-auto")
    def _approve_auto(self) -> None:
        self.action_approve_auto()

    @on(Button.Pressed, "#approve-manual")
    def _approve_manual(self) -> None:
        self.action_approve_manual()

    @on(Button.Pressed, "#keep-planning")
    def _keep_planning(self) -> None:
        self.action_keep_planning()
