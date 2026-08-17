"""The normalized internal event protocol.

The agent runtime speaks only in these events; provider adapters translate wire
formats into this stream, and UI panes subscribe to it. This is the single
contract that decouples the model wire format from the UI and the loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class TextDelta:
    """A chunk of assistant-visible text."""

    text: str


@dataclass
class ReasoningDelta:
    """A chunk of model reasoning/thinking (rendered dim, collapsed)."""

    text: str


@dataclass
class ToolCallStart:
    """A tool call has begun streaming; arguments arrive via ToolCallDelta."""

    id: str
    name: str


@dataclass
class ToolCallDelta:
    """A chunk of a tool call's JSON arguments (accumulate by id)."""

    id: str
    arguments: str


@dataclass
class ToolCallEnd:
    """A tool call is fully assembled."""

    id: str
    name: str
    arguments: str


@dataclass
class Usage:
    """Token accounting for a turn; feeds the ledger + status bar."""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float | None = None


@dataclass
class TurnDone:
    """The provider stream finished for this request."""

    finish_reason: Literal["stop", "tool_calls", "length", "error"]
    error: str | None = None


# ---- Runtime-only events (not emitted by providers, but flow on the same bus) ----


@dataclass
class ToolResultEvent:
    """A tool finished executing (harness-side)."""

    id: str
    name: str
    content: str
    is_error: bool = False
    ms: int = 0  # wall-clock execution time, for the trajectory view


@dataclass
class ContextInjection:
    """A system-reminder spliced into the user turn (mode note, post-compaction
    handoff). Emitted so the trace shows everything the model sees."""

    text: str


@dataclass
class AgentStatus:
    """Lifecycle notification for a UI pane."""

    state: Literal["idle", "sending", "streaming", "executing_tools", "interrupted", "error"]
    detail: str = ""


AgentEvent = (
    TextDelta
    | ReasoningDelta
    | ToolCallStart
    | ToolCallDelta
    | ToolCallEnd
    | Usage
    | TurnDone
    | ToolResultEvent
    | ContextInjection
    | AgentStatus
)


@dataclass
class AssembledToolCall:
    """A fully assembled tool call, ready to execute."""

    id: str
    name: str
    arguments: str  # raw JSON string


@dataclass
class AssistantMessage:
    """The final assistant message produced by one provider request."""

    text: str = ""
    reasoning: str = ""
    tool_calls: list[AssembledToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: Usage = field(default_factory=Usage)
