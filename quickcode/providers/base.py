"""Provider protocol and wire-neutral request/response types.

The core only ever constructs a ``ChatRequest`` and consumes an async stream of
``AgentEvent``s. Concrete adapters (``openai_compat``, later ``anthropic``)
translate to and from vendor wire formats behind this Protocol.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from quickcode.core.events import AgentEvent


@dataclass
class ToolSchema:
    """A tool exposed to the model: JSON-Schema parameters + description copy."""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema (object)


@dataclass
class ChatMessage:
    """One message in the conversation, wire-neutral.

    role == "tool" carries ``tool_call_id``. Assistant messages that call tools
    carry ``tool_calls`` (list of dicts: {id, name, arguments}).
    """

    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None
    # Optional cache hint honored by openai_compat (Anthropic-via-OpenRouter).
    cache_control: bool = False


@dataclass
class ChatRequest:
    model: str
    messages: list[ChatMessage]
    tools: list[ToolSchema] = field(default_factory=list)
    reasoning_effort: str | None = None  # "low" | "medium" | "high" | None
    temperature: float | None = None
    max_tokens: int | None = None


@dataclass
class ModelInfo:
    id: str
    name: str = ""
    context_length: int | None = None
    prompt_price: float | None = None  # USD per 1M input tokens
    completion_price: float | None = None
    supports_tools: bool = True


@runtime_checkable
class Provider(Protocol):
    """The one interface the runtime depends on."""

    def stream_chat(self, req: ChatRequest) -> AsyncIterator[AgentEvent]:
        """Yield normalized events for one request. Must be cancellable."""
        ...

    async def list_models(self) -> list[ModelInfo]:
        ...


class ProviderError(RuntimeError):
    """Raised for auth/network/wire errors so the loop can surface them."""
