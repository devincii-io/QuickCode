"""OpenAI-compatible streaming provider (also used for OpenRouter).

Translates the wire-neutral ``ChatRequest``/``ChatMessage`` types into
OpenAI chat-completion payloads, streams the response, and normalizes
each chunk into the ``AgentEvent`` union defined in ``quickcode.core.events``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from openai import AsyncOpenAI

from quickcode.core.events import (
    AgentEvent,
    ReasoningDelta,
    TextDelta,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
    TurnDone,
    Usage,
)
from quickcode.providers.base import (
    ChatMessage,
    ChatRequest,
    ModelInfo,
    ProviderError,
    ToolSchema,
)

_FINISH_REASON_MAP = {
    "stop": "stop",
    "tool_calls": "tool_calls",
    "length": "length",
}


class OpenAICompatProvider:
    """Provider adapter for any OpenAI-compatible chat-completions endpoint.

    Works with OpenAI itself, OpenRouter, and any local/self-hosted server
    that speaks the same wire format (vLLM, LM Studio, etc.).
    """

    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        *,
        app_name: str = "QuickCode",
    ) -> None:
        self.base_url = base_url
        self.app_name = app_name
        self._is_openrouter = "openrouter.ai" in base_url

        default_headers: dict[str, str] | None = None
        if self._is_openrouter:
            default_headers = {
                "HTTP-Referer": "https://github.com/quickcode",
                "X-Title": app_name,
            }

        # AsyncOpenAI raises on an empty key at construction. Pass a harmless
        # placeholder when none is set so the app can still launch and show a
        # "set your key" notice; the real auth failure then surfaces per-request.
        self.client = AsyncOpenAI(
            base_url=base_url,
            api_key=api_key or "no-key-set",
            default_headers=default_headers,
        )

    # ------------------------------------------------------------------
    # Message / tool translation
    # ------------------------------------------------------------------

    @staticmethod
    def _translate_message(msg: ChatMessage) -> dict[str, Any]:
        out: dict[str, Any] = {"role": msg.role}

        content: Any = msg.content
        if msg.cache_control and msg.content:
            content = [
                {
                    "type": "text",
                    "text": msg.content,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        out["content"] = content

        if msg.role == "assistant" and msg.tool_calls:
            out["tool_calls"] = [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": (
                            tc["arguments"]
                            if isinstance(tc["arguments"], str)
                            else json.dumps(tc["arguments"])
                        ),
                    },
                }
                for tc in msg.tool_calls
            ]

        if msg.role == "tool":
            out["tool_call_id"] = msg.tool_call_id

        if msg.name:
            out["name"] = msg.name

        return out

    @staticmethod
    def _translate_tool(tool: ToolSchema) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        }

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    async def stream_chat(self, req: ChatRequest) -> AsyncIterator[AgentEvent]:
        messages = [self._translate_message(m) for m in req.messages]

        kwargs: dict[str, Any] = {
            "model": req.model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }

        if req.tools:
            kwargs["tools"] = [self._translate_tool(t) for t in req.tools]

        if req.temperature is not None:
            kwargs["temperature"] = req.temperature
        if req.max_tokens is not None:
            kwargs["max_tokens"] = req.max_tokens

        extra_body: dict[str, Any] = {}
        if self._is_openrouter:
            extra_body["usage"] = {"include": True}
        if req.reasoning_effort:
            extra_body["reasoning"] = {"effort": req.reasoning_effort}
        if extra_body:
            kwargs["extra_body"] = extra_body

        # index -> synthesized/assigned tool call id
        tool_ids: dict[int, str] = {}
        tool_names: dict[int, str] = {}
        tool_args: dict[int, list[str]] = {}
        usage_event: Usage | None = None
        finish_reason = "stop"

        try:
            stream = await self.client.chat.completions.create(**kwargs)
            async for chunk in stream:
                usage = getattr(chunk, "usage", None)
                if usage is not None:
                    cached = 0
                    details = getattr(usage, "prompt_tokens_details", None)
                    if details is not None:
                        cached = getattr(details, "cached_tokens", 0) or 0
                    cost = getattr(usage, "cost", None)
                    usage_event = Usage(
                        input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                        output_tokens=getattr(usage, "completion_tokens", 0) or 0,
                        cached_tokens=cached,
                        cost_usd=cost,
                    )

                choices = getattr(chunk, "choices", None)
                if not choices:
                    continue

                choice = choices[0]
                delta = getattr(choice, "delta", None)
                chunk_finish_reason = getattr(choice, "finish_reason", None)
                if chunk_finish_reason:
                    finish_reason = _FINISH_REASON_MAP.get(chunk_finish_reason, "stop")

                if delta is None:
                    continue

                content = getattr(delta, "content", None)
                if content:
                    yield TextDelta(content)

                reasoning = getattr(delta, "reasoning", None) or getattr(
                    delta, "reasoning_content", None
                )
                if reasoning:
                    yield ReasoningDelta(reasoning)

                delta_tool_calls = getattr(delta, "tool_calls", None)
                if delta_tool_calls:
                    for tc in delta_tool_calls:
                        index = getattr(tc, "index", 0)
                        func = getattr(tc, "function", None)
                        name = getattr(func, "name", None) if func else None
                        arguments = getattr(func, "arguments", None) if func else None
                        raw_id = getattr(tc, "id", None)

                        if index not in tool_ids:
                            call_id = raw_id or f"call_{index}"
                            tool_ids[index] = call_id
                            tool_names[index] = name or ""
                            tool_args[index] = []
                            yield ToolCallStart(call_id, name or "")
                        elif name and not tool_names.get(index):
                            tool_names[index] = name

                        if arguments:
                            tool_args.setdefault(index, []).append(arguments)
                            yield ToolCallDelta(tool_ids[index], arguments)

            for index, call_id in tool_ids.items():
                yield ToolCallEnd(
                    call_id,
                    tool_names.get(index, ""),
                    "".join(tool_args.get(index, [])),
                )

            if usage_event is not None:
                yield usage_event

            yield TurnDone(finish_reason)

        except Exception as e:  # noqa: BLE001 - normalize any provider/network error
            if usage_event is not None:
                yield usage_event
            raise ProviderError(str(e)) from e

    # ------------------------------------------------------------------
    # Model listing
    # ------------------------------------------------------------------

    async def list_models(self) -> list[ModelInfo]:
        try:
            resp = await self.client.models.list()
        except Exception:  # noqa: BLE001 - never crash on model listing
            return []

        models: list[ModelInfo] = []
        for m in getattr(resp, "data", []) or []:
            model_id = getattr(m, "id", "")
            name = getattr(m, "name", None) or model_id
            context_length = getattr(m, "context_length", None)

            pricing = getattr(m, "pricing", None)
            prompt_price = None
            completion_price = None
            if pricing is not None:
                prompt_price = getattr(pricing, "prompt", None)
                completion_price = getattr(pricing, "completion", None)
                if prompt_price is None and isinstance(pricing, dict):
                    prompt_price = pricing.get("prompt")
                if completion_price is None and isinstance(pricing, dict):
                    completion_price = pricing.get("completion")

            models.append(
                ModelInfo(
                    id=model_id,
                    name=name,
                    context_length=context_length,
                    prompt_price=prompt_price,
                    completion_price=completion_price,
                    supports_tools=True,
                )
            )

        return models
