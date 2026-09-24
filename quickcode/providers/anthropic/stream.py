"""Messages API stream events -> QuickCode ``AgentEvent``s.

One ``StreamTranslator`` per response. It is fed each decoded SSE payload and
returns the events that payload produces, so the provider can tell "nothing
has reached the caller yet" (safe to retry) from "the caller has seen output"
(an error now has to be surfaced, not hidden behind a retry).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from quickcode.core.events import (
    AgentEvent,
    ReasoningBlock,
    ReasoningDelta,
    TextDelta,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
    TurnDone,
    Usage,
)
from quickcode.providers.anthropic.models import cost_usd
from quickcode.providers.anthropic.wire import replayable

_STOP_REASONS = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "pause_turn": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
    "model_context_window_exceeded": "length",
}

# Mid-stream error types worth a fresh request when no output has been shown.
RETRYABLE_ERROR_TYPES = {"overloaded_error", "api_error", "rate_limit_error", "timeout_error"}


class StreamError(Exception):
    """An ``error`` event inside a 200 response."""

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.message = message

    @property
    def retryable(self) -> bool:
        return self.error_type in RETRYABLE_ERROR_TYPES


@dataclass
class _Block:
    kind: str
    data: dict[str, Any] = field(default_factory=dict)
    parts: list[str] = field(default_factory=list)


@dataclass
class StreamTranslator:
    model: str
    blocks: dict[int, _Block] = field(default_factory=dict)
    usage: dict[str, int] = field(default_factory=dict)
    stop_reason: str | None = None
    stop_details: dict[str, Any] | None = None
    served_model: str = ""
    finished: bool = False

    def feed(self, event: dict[str, Any]) -> list[AgentEvent]:
        kind = event.get("type")
        if kind == "message_start":
            message = event.get("message") or {}
            self.served_model = str(message.get("model") or "")
            self._take_usage(message.get("usage"))
            return []
        if kind == "content_block_start":
            return self._start(int(event.get("index", 0)), event.get("content_block") or {})
        if kind == "content_block_delta":
            return self._delta(int(event.get("index", 0)), event.get("delta") or {})
        if kind == "content_block_stop":
            return self._stop(int(event.get("index", 0)))
        if kind == "message_delta":
            delta = event.get("delta") or {}
            if delta.get("stop_reason"):
                self.stop_reason = delta["stop_reason"]
            if isinstance(delta.get("stop_details"), dict):
                self.stop_details = delta["stop_details"]
            self._take_usage(event.get("usage"))
            return []
        if kind == "message_stop":
            self.finished = True
            return []
        if kind == "error":
            err = event.get("error") or {}
            raise StreamError(str(err.get("type") or "error"), str(err.get("message") or ""))
        # ping, and any event type added after this was written.
        return []

    # ---- content blocks ----

    def _start(self, index: int, block: dict[str, Any]) -> list[AgentEvent]:
        kind = str(block.get("type") or "")
        if kind == "text":
            self.blocks[index] = _Block("text")
            text = block.get("text") or ""
            return [TextDelta(text)] if text else []
        if kind == "thinking":
            self.blocks[index] = _Block("thinking", {"signature": block.get("signature") or ""})
            text = block.get("thinking") or ""
            if text:
                self.blocks[index].parts.append(text)
                return [ReasoningDelta(text)]
            return []
        if kind == "redacted_thinking":
            self.blocks[index] = _Block("redacted_thinking", {"data": block.get("data") or ""})
            return []
        if kind == "tool_use":
            call_id = str(block.get("id") or f"toolu_{index}")
            name = str(block.get("name") or "")
            self.blocks[index] = _Block("tool_use", {"id": call_id, "name": name})
            return [ToolCallStart(call_id, name)]
        # Server tools and anything newer: not part of this app's loop.
        self.blocks[index] = _Block("ignored")
        return []

    def _delta(self, index: int, delta: dict[str, Any]) -> list[AgentEvent]:
        block = self.blocks.get(index)
        if block is None:
            return []
        kind = delta.get("type")
        if kind == "text_delta" and block.kind == "text":
            text = delta.get("text") or ""
            return [TextDelta(text)] if text else []
        if kind == "thinking_delta" and block.kind == "thinking":
            text = delta.get("thinking") or ""
            if not text:
                return []
            block.parts.append(text)
            return [ReasoningDelta(text)]
        if kind == "signature_delta" and block.kind == "thinking":
            block.data["signature"] = block.data.get("signature", "") + (delta.get("signature") or "")
            return []
        if kind == "input_json_delta" and block.kind == "tool_use":
            chunk = delta.get("partial_json") or ""
            if not chunk:
                return []
            block.parts.append(chunk)
            return [ToolCallDelta(block.data["id"], chunk)]
        return []

    def _stop(self, index: int) -> list[AgentEvent]:
        block = self.blocks.get(index)
        if block is None:
            return []
        if block.kind == "tool_use":
            return [ToolCallEnd(block.data["id"], block.data["name"], "".join(block.parts) or "{}")]
        if block.kind == "thinking":
            whole = {
                "type": "thinking",
                "thinking": "".join(block.parts),
                "signature": block.data.get("signature", ""),
            }
            return [ReasoningBlock(whole)] if replayable(whole) else []
        if block.kind == "redacted_thinking":
            whole = {"type": "redacted_thinking", "data": block.data.get("data", "")}
            return [ReasoningBlock(whole)] if replayable(whole) else []
        return []

    # ---- the end ----

    def _take_usage(self, usage: Any) -> None:
        if not isinstance(usage, dict):
            return
        # message_delta counts are cumulative, so a later value replaces an
        # earlier one rather than adding to it.
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        ):
            value = usage.get(key)
            if isinstance(value, int):
                self.usage[key] = value

    def usage_event(self) -> Usage | None:
        if not self.usage:
            return None
        uncached = self.usage.get("input_tokens", 0)
        written = self.usage.get("cache_creation_input_tokens", 0)
        read = self.usage.get("cache_read_input_tokens", 0)
        output = self.usage.get("output_tokens", 0)
        return Usage(
            # The whole prompt, the way the ledger and the context meter read
            # it; the API reports only the part after the last cache hit.
            input_tokens=uncached + written + read,
            output_tokens=output,
            cached_tokens=read,
            cache_write_tokens=written,
            cost_usd=cost_usd(
                self.served_model or self.model,
                input_tokens=uncached,
                output_tokens=output,
                cache_write_tokens=written,
                cache_read_tokens=read,
            ),
        )

    def turn_done(self) -> TurnDone:
        if self.stop_reason == "refusal":
            category = (self.stop_details or {}).get("category")
            detail = f" ({category})" if category else ""
            return TurnDone(
                "error", f"The model declined to continue this request{detail}."
            )
        return TurnDone(_STOP_REASONS.get(self.stop_reason or "", "stop"))  # type: ignore[arg-type]
