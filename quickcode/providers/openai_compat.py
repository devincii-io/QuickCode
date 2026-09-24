"""OpenAI-compatible streaming provider (also used for OpenRouter).

Translates the wire-neutral ``ChatRequest``/``ChatMessage`` types into
OpenAI chat-completion payloads, streams the response, and normalizes
each chunk into the ``AgentEvent`` union defined in ``quickcode.core.events``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # the SDK is imported lazily -- see OpenAICompatProvider.client
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


class _ToolCalls:
    """Tool-call deltas into whole calls, however the server numbers them.

    OpenAI streams each call under a stable ``index`` and sends its ``id``
    once. Compatible servers vary: some number every parallel call 0 and tell
    them apart only by id, some send no index, some send no id. Keying on the
    index alone concatenated two calls' arguments into one unparseable string,
    and ``call_{index}`` for a missing id repeated in every round -- the same
    id twice in one conversation, which the UI and the log key results by.
    """

    def __init__(self) -> None:
        # [id, name, argument chunks] per call, in the order they began.
        self._calls: list[tuple[str, list[str], list[str]]] = []
        self._raw_ids: list[str | None] = []
        self._by_index: dict[Any, int] = {}
        self._by_raw_id: dict[str, int] = {}

    def feed(
        self, index: Any, raw_id: str | None, name: str | None, arguments: str | None
    ) -> list[AgentEvent]:
        raw_id = raw_id or None
        pos: int | None = None
        if index is not None:
            pos = self._by_index.get(index)
            if pos is not None and raw_id and self._raw_ids[pos] not in (None, raw_id):
                pos = None  # a new id under a reused index is a new call
        elif raw_id:
            pos = self._by_raw_id.get(raw_id)
        elif self._calls:
            pos = len(self._calls) - 1

        events: list[AgentEvent] = []
        if pos is None:
            call_id = raw_id if raw_id and raw_id not in self._by_raw_id else _fresh_call_id()
            pos = len(self._calls)
            self._calls.append((call_id, [name or ""], []))
            self._raw_ids.append(raw_id)
            if raw_id:
                self._by_raw_id.setdefault(raw_id, pos)
            if index is not None:
                self._by_index[index] = pos
            events.append(ToolCallStart(call_id, name or ""))
        else:
            if raw_id and self._raw_ids[pos] is None:
                self._raw_ids[pos] = raw_id
                self._by_raw_id.setdefault(raw_id, pos)
            if name and not self._calls[pos][1][0]:
                self._calls[pos][1][0] = name

        if arguments:
            call_id, _, args = self._calls[pos]
            args.append(arguments)
            events.append(ToolCallDelta(call_id, arguments))
        return events

    def ends(self) -> list[ToolCallEnd]:
        return [ToolCallEnd(cid, name[0], "".join(args)) for cid, name, args in self._calls]


def _fresh_call_id() -> str:
    return f"call_{uuid.uuid4().hex[:24]}"


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
                # OpenRouter attributes requests to whatever this names. It
                # pointed at github.com/quickcode, which is not this project
                # and does not resolve -- so the app was identifying itself to
                # a third party as someone else's dead URL.
                "HTTP-Referer": "https://github.com/devincii-io/QuickCode",
                "X-Title": app_name,
            }

        self._api_key = api_key
        self._default_headers = default_headers
        self._client: AsyncOpenAI | None = None

    @property
    def client(self) -> AsyncOpenAI:
        """The SDK client, built on first use.

        Importing ``openai`` costs ~800 ms of the ~1.2 s it takes to import
        this application at all, and a large multiple of that on a cold file
        cache -- it pulls in the Pydantic model trees for Assistants, graders,
        evals, batches and responses, none of which this adapter touches.
        Paying it before the window exists is what made a cold start look like
        nothing happening; paying it on the first request hides it behind model
        latency the user is already waiting on.

        AsyncOpenAI raises on an empty key at construction, so a harmless
        placeholder stands in when none is set: the app still launches and can
        show a "set your key" notice, and the real auth failure surfaces
        per-request rather than as a crash on startup.
        """
        if self._client is None:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(
                base_url=self.base_url,
                api_key=self._api_key or "no-key-set",
                default_headers=self._default_headers,
            )
        return self._client

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

        calls = _ToolCalls()
        usage_event: Usage | None = None
        finish_reason = "stop"
        stream = None

        try:
            stream = await self.client.chat.completions.create(**kwargs)
            async for chunk in stream:
                usage = getattr(chunk, "usage", None)
                if usage is not None:
                    cached = 0
                    details = getattr(usage, "prompt_tokens_details", None)
                    if details is not None:
                        cached = getattr(details, "cached_tokens", 0) or 0
                    out_details = getattr(usage, "completion_tokens_details", None)
                    cost = getattr(usage, "cost", None)
                    usage_event = Usage(
                        input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                        output_tokens=getattr(usage, "completion_tokens", 0) or 0,
                        cached_tokens=cached,
                        cost_usd=cost,
                        reasoning_tokens=getattr(out_details, "reasoning_tokens", 0) or 0,
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

                for tc in getattr(delta, "tool_calls", None) or ():
                    func = getattr(tc, "function", None)
                    for ev in calls.feed(
                        getattr(tc, "index", None),
                        getattr(tc, "id", None),
                        getattr(func, "name", None) if func else None,
                        getattr(func, "arguments", None) if func else None,
                    ):
                        yield ev

            for ev in calls.ends():
                yield ev

            if usage_event is not None:
                yield usage_event

            yield TurnDone(finish_reason)

        except Exception as e:  # noqa: BLE001 - normalize any provider/network error
            if usage_event is not None:
                yield usage_event
            raise ProviderError(str(e)) from e
        finally:
            # A reader that stops early (an interrupt) leaves the response
            # open until the generator is collected, and the server keeps
            # generating -- and billing -- into it until then.
            if stream is not None:
                close = getattr(stream, "close", None)
                if close is not None:
                    try:
                        await close()
                    except Exception:  # noqa: BLE001 - already on the way out
                        pass

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
            try:
                model_id = getattr(m, "id", "") or ""
                if not model_id:
                    continue
                name = getattr(m, "name", None) or model_id

                # OpenRouter (and other extended OpenAI-compatible backends)
                # attach extra per-model fields the openai SDK stashes on
                # `model_extra` since they aren't part of the base schema.
                extra = getattr(m, "model_extra", None) or {}
                if not isinstance(extra, dict):
                    extra = {}

                context_length = getattr(m, "context_length", None) or extra.get(
                    "context_length"
                )
                try:
                    context_length = (
                        int(context_length) if context_length is not None else None
                    )
                except (TypeError, ValueError):
                    context_length = None

                pricing = extra.get("pricing")
                if not isinstance(pricing, dict):
                    pricing = {}

                prompt_price = _price_per_million(pricing.get("prompt"))
                completion_price = _price_per_million(pricing.get("completion"))

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
            except Exception:  # noqa: BLE001 - one bad entry shouldn't break listing
                continue

        return models


def _price_per_million(raw: Any) -> float | None:
    """OpenRouter reports USD-per-token pricing as strings; convert to USD/1M
    tokens. Negative values are OpenRouter's "variable/router" sentinel (e.g.
    ``-1`` for openrouter/auto) — treat those as unknown rather than showing a
    nonsensical negative price."""
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    return value * 1_000_000
