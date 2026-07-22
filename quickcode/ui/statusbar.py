"""Status bar: model, context %, cost, mode, hints."""

from __future__ import annotations

from textual.reactive import reactive
from textual.widgets import Static

from quickcode.core.permissions import Mode

_MODE_LABEL = {
    Mode.plan: "PLAN",
    Mode.ask: "ASK",
    Mode.auto_edit: "AUTO-EDIT",
    Mode.dontask: "DONTASK",
    Mode.yolo: "YOLO",
}

_MODE_CLASS = {
    Mode.plan: "mode-plan",
    Mode.ask: "mode-ask",
    Mode.auto_edit: "mode-auto-edit",
    Mode.dontask: "mode-ask",
    Mode.yolo: "mode-yolo",
}


class StatusBar(Static):
    """A single-line footer showing live agent state."""

    model: reactive[str] = reactive("")
    ctx_pct: reactive[float | None] = reactive(None)
    cost_usd: reactive[float] = reactive(0.0)
    mode: reactive[Mode] = reactive(Mode.ask)
    agent_state: reactive[str] = reactive("idle")

    def render(self) -> str:
        ctx = f"{self.ctx_pct:.0f}%" if self.ctx_pct is not None else "--"
        mode_label = _MODE_LABEL.get(self.mode, str(self.mode))
        return (
            f" {self.model}  ·  ctx {ctx}  ·  ${self.cost_usd:.4f}  ·  "
            f"[{mode_label}]  ·  {self.agent_state}  ·  Esc interrupt "
        )

    def watch_mode(self, mode: Mode) -> None:
        for cls in _MODE_CLASS.values():
            self.remove_class(cls)
        self.add_class(_MODE_CLASS.get(mode, "mode-ask"))

    def update_usage(self, ctx_pct: float | None, cost_usd: float) -> None:
        self.ctx_pct = ctx_pct
        self.cost_usd = cost_usd
