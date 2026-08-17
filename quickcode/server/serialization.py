"""AgentEvent → JSON wire shapes, and the trace-event vocabulary.

Two layers share these shapes:
  - live: every bus event is serialized and pushed to attached WebSockets
  - log:  *assembled* events (whole assistant messages, whole tool calls,
    results, decisions) are appended to the session's event log; replay renders
    the identical transcript without needing the delta stream

Delta events (``text_delta``/``reasoning_delta``/``tool_call_delta``) are
live-only; everything else appears in both places.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from quickcode.core.events import (
    AgentEvent,
    AgentStatus,
    ContextInjection,
    ReasoningDelta,
    TextDelta,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
    ToolResultEvent,
    TurnDone,
    Usage,
)

# Trace log caps: results are stored in full up to this size so the trace stays
# useful without letting one giant read balloon the log file.
LOG_RESULT_CAP = 64 * 1024


def event_to_json(ev: AgentEvent) -> dict[str, Any] | None:
    """Serialize one bus event for the live WebSocket stream."""
    if isinstance(ev, TextDelta):
        return {"type": "text_delta", "text": ev.text}
    if isinstance(ev, ReasoningDelta):
        return {"type": "reasoning_delta", "text": ev.text}
    if isinstance(ev, ToolCallStart):
        return {"type": "tool_call_start", "id": ev.id, "name": ev.name}
    if isinstance(ev, ToolCallDelta):
        return {"type": "tool_call_delta", "id": ev.id, "arguments": ev.arguments}
    if isinstance(ev, ToolCallEnd):
        return {"type": "tool_call", "id": ev.id, "name": ev.name, "arguments": ev.arguments}
    if isinstance(ev, ToolResultEvent):
        content = ev.content
        if len(content) > LOG_RESULT_CAP:
            content = content[:LOG_RESULT_CAP] + f"\n<truncated for trace; total {len(ev.content)} chars>"
        return {
            "type": "tool_result",
            "id": ev.id,
            "name": ev.name,
            "content": content,
            "is_error": ev.is_error,
            "ms": ev.ms,
        }
    if isinstance(ev, Usage):
        return {"type": "usage", **asdict(ev)}
    if isinstance(ev, TurnDone):
        return {"type": "round_done", "finish_reason": ev.finish_reason, "error": ev.error}
    if isinstance(ev, ContextInjection):
        return {"type": "context_injection", "text": ev.text}
    if isinstance(ev, AgentStatus):
        return {"type": "status", "state": ev.state, "detail": ev.detail}
    return None


# Event types that belong in the persistent trace log (assembled shapes the
# manager produces; deltas and transient status flips stay live-only).
LOGGED_TYPES = {
    "user_message",
    "assistant_message",
    "system_prompt",
    "context_injection",
    "tool_call",
    "tool_result",
    "usage",
    "permission_request",
    "permission_resolved",
    "plan_request",
    "plan_resolved",
    "mode_changed",
    "model_changed",
    "compacted",
    "agent_spawned",
    "agent_done",
    "system_note",
    "error",
}


def loggable(ev: dict[str, Any]) -> bool:
    return ev.get("type") in LOGGED_TYPES
