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
class ReasoningBlock:
    """A finished reasoning block to hand back verbatim on the next request.

    Anthropic signs each thinking block, and a tool-use turn replayed without
    its thinking -- or with it edited -- is refused. The text already went out
    as ``ReasoningDelta``; this carries the opaque whole. Provider-to-loop
    only: it never reaches the UI or the event log.
    """

    block: dict


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
    # Prompt tokens written to the provider's cache on this request (billed at
    # a premium). Like ``cached_tokens``, already counted in ``input_tokens``.
    cache_write_tokens: int = 0
    # The part of ``output_tokens`` spent thinking. Billed, but never sent
    # back to the model, so it is spend without being context.
    reasoning_tokens: int = 0


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
    # Structured extras the tool attached for the UI (a diff, "the task board
    # changed"). Stays server-side: some of it repeats the tool's own input,
    # so it is not put on the wire or into the session log.
    ui_meta: dict = field(default_factory=dict)


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


@dataclass
class Compacted:
    """The context guard summarized the history between two rounds of a turn.

    ``messages`` is the rebuilt history as it stood at that moment, for the
    recorder: a session log replaces everything before a compaction with the
    messages it records, and by the time the recorder reads this event the
    loop may already have appended the next round. It never reaches the wire.
    """

    summary_chars: int
    messages: list = field(default_factory=list, repr=False)


@dataclass
class SystemNote:
    """A harness remark for the transcript. The model is not sent it."""

    text: str


AgentEvent = (
    TextDelta
    | ReasoningDelta
    | ReasoningBlock
    | ToolCallStart
    | ToolCallDelta
    | ToolCallEnd
    | Usage
    | TurnDone
    | ToolResultEvent
    | ContextInjection
    | AgentStatus
    | Compacted
    | SystemNote
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
    reasoning_blocks: list[dict] = field(default_factory=list)
