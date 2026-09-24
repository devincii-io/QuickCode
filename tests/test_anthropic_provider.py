"""The native Anthropic adapter, against scripted Messages API responses.

No network: every response is an ``httpx.MockTransport`` answer built from the
documented SSE event shapes, and every retry wait goes to a recording stub
instead of the clock.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from quickcode.core.agent import AgentInstance, Ledger, PermissionOutcome
from quickcode.core.events import (
    AssistantMessage,
    ReasoningBlock,
    ReasoningDelta,
    TextDelta,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
    TurnDone,
    Usage,
)
from quickcode.core.history import History
from quickcode.core.loop import run_turn
from quickcode.core.permissions import Mode, PermissionEngine, Rules
from quickcode.providers.anthropic import AnthropicProvider, resolve_base_url
from quickcode.providers.anthropic.models import normalize_model, traits_for
from quickcode.providers.anthropic.retry import RetryPolicy, parse_retry_after
from quickcode.providers.anthropic.wire import build_body
from quickcode.providers.base import ChatMessage, ChatRequest, ProviderError, ToolSchema
from quickcode.session.store import message_from_dict, message_to_dict
from quickcode.tools.base import ReadRegistry, ToolCtx
from quickcode.tools.registry import default_registry

KEY = "sk-ant-test-key"


# ---------------------------------------------------------------------------
# Scripted API
# ---------------------------------------------------------------------------


def sse(*events: dict) -> bytes:
    return "".join(
        f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events
    ).encode("utf-8")


def stream_response(*events: dict, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        content=sse(*events),
        headers={"content-type": "text/event-stream", **(headers or {})},
    )


def error_response(status: int, kind: str, message: str, **headers: str) -> httpx.Response:
    return httpx.Response(
        status,
        json={"type": "error", "error": {"type": kind, "message": message},
              "request_id": "req_test"},
        headers={"request-id": "req_test", **headers},
    )


def start(input_tokens: int = 12, model: str = "claude-opus-5", **usage: int) -> dict:
    return {
        "type": "message_start",
        "message": {
            "id": "msg_1", "type": "message", "role": "assistant", "content": [],
            "model": model, "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": input_tokens, "output_tokens": 1, **usage},
        },
    }


def text_block(index: int, *chunks: str) -> list[dict]:
    return [
        {"type": "content_block_start", "index": index,
         "content_block": {"type": "text", "text": ""}},
        *({"type": "content_block_delta", "index": index,
           "delta": {"type": "text_delta", "text": c}} for c in chunks),
        {"type": "content_block_stop", "index": index},
    ]


def thinking_block(index: int, text: str, signature: str) -> list[dict]:
    return [
        {"type": "content_block_start", "index": index,
         "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
        {"type": "content_block_delta", "index": index,
         "delta": {"type": "thinking_delta", "thinking": text}},
        {"type": "content_block_delta", "index": index,
         "delta": {"type": "signature_delta", "signature": signature}},
        {"type": "content_block_stop", "index": index},
    ]


def tool_block(index: int, call_id: str, name: str, *json_chunks: str) -> list[dict]:
    return [
        {"type": "content_block_start", "index": index,
         "content_block": {"type": "tool_use", "id": call_id, "name": name, "input": {}}},
        {"type": "content_block_delta", "index": index,
         "delta": {"type": "input_json_delta", "partial_json": ""}},
        *({"type": "content_block_delta", "index": index,
           "delta": {"type": "input_json_delta", "partial_json": c}} for c in json_chunks),
        {"type": "content_block_stop", "index": index},
    ]


def finish(stop_reason: str = "end_turn", output_tokens: int = 20, **extra) -> list[dict]:
    return [
        {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None,
                                            **extra},
         "usage": {"output_tokens": output_tokens}},
        {"type": "message_stop"},
    ]


class Api:
    """Answers each request with the next scripted response and keeps them."""

    def __init__(self, *answers: httpx.Response | Callable[[httpx.Request], httpx.Response]):
        self.answers = list(answers)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.answers.pop(0)
        return answer(request) if callable(answer) else answer

    def body(self, i: int = -1) -> dict:
        return json.loads(self.requests[i].content)


class Sleeps:
    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def provider(api: Api, *, base_url: str = "", sleep: Sleeps | None = None,
             retries: int = 3) -> AnthropicProvider:
    return AnthropicProvider(
        base_url, KEY,
        transport=httpx.MockTransport(api),
        retry=RetryPolicy(max_retries=retries),
        sleep=sleep or Sleeps(),
    )


GLOB = ToolSchema("glob", "find files", {"type": "object", "properties": {}})


def request(*messages: ChatMessage, model: str = "claude-opus-5", **kw) -> ChatRequest:
    msgs = [ChatMessage(role="system", content="SYS", cache_control=True), *messages]
    return ChatRequest(model=model, messages=msgs, **kw)


async def collect(p: AnthropicProvider, req: ChatRequest) -> list:
    return [ev async for ev in p.stream_chat(req)]


# ---------------------------------------------------------------------------
# Streaming text
# ---------------------------------------------------------------------------


async def test_a_streamed_answer_arrives_as_text_then_usage_then_done() -> None:
    api = Api(stream_response(start(), *text_block(0, "Hel", "lo"), *finish()))
    events = await collect(provider(api), request(ChatMessage(role="user", content="hi")))

    assert [e.text for e in events if isinstance(e, TextDelta)] == ["Hel", "lo"]
    usage = next(e for e in events if isinstance(e, Usage))
    assert (usage.input_tokens, usage.output_tokens) == (12, 20)
    assert events[-1] == TurnDone("stop")

    sent = api.requests[0]
    assert sent.url == "https://api.anthropic.com/v1/messages"
    assert sent.headers["x-api-key"] == KEY
    assert sent.headers["anthropic-version"] == "2023-06-01"
    body = api.body()
    assert body["model"] == "claude-opus-5"
    assert body["stream"] is True
    assert body["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert "temperature" not in body


async def test_the_system_prompt_and_the_conversation_tail_are_cache_breakpoints() -> None:
    history = History("SYSTEM PROMPT")
    history.push_user("first")
    history.push_assistant(AssistantMessage(text="answer"))
    history.push_user("second")
    api = Api(stream_response(start(), *text_block(0, "ok"), *finish()))
    await collect(provider(api), ChatRequest(model="claude-opus-5",
                                             messages=history.build_messages()))

    body = api.body()
    assert body["system"] == [{"type": "text", "text": "SYSTEM PROMPT",
                               "cache_control": {"type": "ephemeral"}}]
    marked = [
        (m["role"], b.get("text")) for m in body["messages"] for b in m["content"]
        if "cache_control" in b
    ]
    assert marked == [("user", "second")]


async def test_the_same_conversation_serializes_to_the_same_bytes() -> None:
    """The cache is a prefix match: a byte that moves between two requests of
    the same session is a cache miss on everything after it."""
    history = History("SYS")
    history.push_user("hi")
    req = ChatRequest(model="claude-opus-5", messages=history.build_messages(), tools=[GLOB])
    api = Api(*(stream_response(start(), *text_block(0, "x"), *finish()) for _ in range(2)))
    p = provider(api)
    await collect(p, req)
    await collect(p, req)
    assert api.requests[0].content == api.requests[1].content


# ---------------------------------------------------------------------------
# Tool use, through the real loop
# ---------------------------------------------------------------------------


async def _allow(_req) -> PermissionOutcome:
    return PermissionOutcome(allow=True)


def _agent(p: AnthropicProvider, cwd: Path) -> AgentInstance:
    return AgentInstance(
        name="main",
        provider=p,
        registry=default_registry(),
        history=History("SYS"),
        ctx=ToolCtx(cwd=cwd, read_registry=ReadRegistry()),
        permissions=PermissionEngine(Mode.ask, Rules(), cwd),
        model="anthropic/claude-opus-5",
        permission_cb=_allow,
    )


async def test_a_tool_round_trip_replays_signed_thinking_and_answers_with_results(
    tmp_path: Path,
) -> None:
    (tmp_path / "notes.txt").write_text("hello", encoding="utf-8")
    api = Api(
        stream_response(
            start(),
            *thinking_block(0, "Look for text files.", "sig-abc"),
            *text_block(1, "Searching."),
            *tool_block(2, "toolu_01", "glob", '{"pattern": ', '"*.txt"}'),
            *finish("tool_use"),
        ),
        stream_response(start(), *text_block(0, "Found notes.txt."), *finish()),
    )
    agent = _agent(provider(api), tmp_path)
    seen: list = []
    queue = agent.bus.subscribe(maxsize=0)

    reply = await run_turn(agent, "which text files are here?")
    while not queue.empty():
        seen.append(queue.get_nowait())

    assert reply == "Found notes.txt."
    assert any(isinstance(e, ReasoningDelta) and e.text == "Look for text files." for e in seen)
    assert not any(isinstance(e, ReasoningBlock) for e in seen), "signatures stay off the bus"
    assert [type(e) for e in seen if isinstance(e, (ToolCallStart, ToolCallEnd))] == [
        ToolCallStart, ToolCallEnd,
    ]
    assert any(isinstance(e, ToolCallDelta) for e in seen)

    second = api.body(1)
    assistant, results = second["messages"][1], second["messages"][2]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == [
        {"type": "thinking", "thinking": "Look for text files.", "signature": "sig-abc"},
        {"type": "text", "text": "Searching."},
        {"type": "tool_use", "id": "toolu_01", "name": "glob", "input": {"pattern": "*.txt"}},
    ]
    assert results["role"] == "user"
    [result] = results["content"]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "toolu_01"
    assert "notes.txt" in result["content"]
    assert result["cache_control"] == {"type": "ephemeral"}, "the tail moves with the loop"
    assert second["tools"][0]["eager_input_streaming"] is True

    # The signed block is part of the history a resume rebuilds.
    replayed = message_from_dict(json.loads(json.dumps(message_to_dict(agent.history.messages[1]))))
    assert replayed.reasoning_blocks == assistant["content"][:1]


async def test_a_failed_tool_result_is_flagged_as_an_error() -> None:
    body = build_body(
        request(
            ChatMessage(role="user", content="go"),
            ChatMessage(role="assistant", tool_calls=[
                {"id": "t1", "name": "bash", "arguments": '{"command": "false"}'}]),
            ChatMessage(role="tool", content="[error] exit 1", tool_call_id="t1", name="bash"),
            tools=[GLOB],
        ),
        model="claude-opus-5", traits=traits_for("claude-opus-5"),
    )
    [result] = body["messages"][2]["content"]
    assert result == {"type": "tool_result", "tool_use_id": "t1", "content": "exit 1",
                      "is_error": True}


async def test_tool_results_and_a_following_reminder_share_one_user_turn() -> None:
    body = build_body(
        request(
            ChatMessage(role="user", content="go"),
            ChatMessage(role="assistant", tool_calls=[
                {"id": "t1", "name": "glob", "arguments": "{}"}]),
            ChatMessage(role="tool", content="a.txt", tool_call_id="t1"),
            ChatMessage(role="user", content="<system-reminder>wrap up</system-reminder>"),
            tools=[GLOB],
        ),
        model="claude-opus-5", traits=traits_for("claude-opus-5"),
    )
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "user"]
    assert [b["type"] for b in body["messages"][2]["content"]] == ["tool_result", "text"]


async def test_a_summary_request_without_tools_sends_tool_history_as_text() -> None:
    """The API refuses tool blocks in a request that declares no tools, which
    is exactly what the compaction summary sends."""
    body = build_body(
        request(
            ChatMessage(role="user", content="go"),
            ChatMessage(role="assistant", content="Looking.", tool_calls=[
                {"id": "t1", "name": "glob", "arguments": '{"pattern": "*"}'}],
                reasoning_blocks=[{"type": "thinking", "thinking": "x", "signature": "s"}]),
            ChatMessage(role="tool", content="a.txt", tool_call_id="t1", name="glob"),
            ChatMessage(role="user", content="Summarize."),
        ),
        model="claude-opus-5", traits=traits_for("claude-opus-5"),
    )
    kinds = {b["type"] for m in body["messages"] for b in m["content"]}
    assert kinds == {"text"}
    assert "tools" not in body


async def test_a_refusal_ends_the_round_as_an_error() -> None:
    api = Api(stream_response(
        start(), *text_block(0, "I"),
        *finish("refusal", stop_details={"type": "refusal", "category": "cyber"}),
    ))
    events = await collect(provider(api), request(ChatMessage(role="user", content="x")))
    done = events[-1]
    assert done.finish_reason == "error" and "cyber" in done.error


async def test_max_tokens_is_reported_as_length() -> None:
    api = Api(stream_response(start(), *text_block(0, "cut"), *finish("max_tokens")))
    events = await collect(provider(api), request(ChatMessage(role="user", content="x")))
    assert events[-1] == TurnDone("length")


# ---------------------------------------------------------------------------
# Thinking
# ---------------------------------------------------------------------------


async def test_thinking_streams_as_reasoning_and_is_kept_whole_with_its_signature() -> None:
    api = Api(stream_response(
        start(),
        *thinking_block(0, "Step one.", "sig-1"),
        {"type": "content_block_start", "index": 1,
         "content_block": {"type": "redacted_thinking", "data": "opaque"}},
        {"type": "content_block_stop", "index": 1},
        *text_block(2, "Done."),
        *finish(),
    ))
    events = await collect(provider(api), request(ChatMessage(role="user", content="x")))
    assert [e.text for e in events if isinstance(e, ReasoningDelta)] == ["Step one."]
    assert [e.block for e in events if isinstance(e, ReasoningBlock)] == [
        {"type": "thinking", "thinking": "Step one.", "signature": "sig-1"},
        {"type": "redacted_thinking", "data": "opaque"},
    ]


async def test_an_unsigned_thinking_block_is_never_replayed() -> None:
    api = Api(stream_response(
        start(),
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
        {"type": "content_block_stop", "index": 0},
        *finish("max_tokens"),
    ))
    events = await collect(provider(api), request(ChatMessage(role="user", content="x")))
    assert not any(isinstance(e, ReasoningBlock) for e in events)


def test_older_models_get_a_thinking_budget_only_when_reasoning_is_asked_for() -> None:
    legacy = traits_for("claude-haiku-4-5")
    plain = build_body(request(ChatMessage(role="user", content="x"), temperature=1.7),
                       model="claude-haiku-4-5", traits=legacy)
    assert "thinking" not in plain
    assert plain["temperature"] == 1.0, "the API's range is 0-1"

    thinking = build_body(
        request(ChatMessage(role="user", content="x"), reasoning_effort="high", temperature=0.2),
        model="claude-haiku-4-5", traits=legacy,
    )
    assert thinking["thinking"]["type"] == "enabled"
    assert 1024 <= thinking["thinking"]["budget_tokens"] < thinking["max_tokens"]
    assert "temperature" not in thinking, "sampling is not allowed alongside thinking"
    assert "output_config" not in thinking


def test_current_models_think_adaptively_and_take_effort() -> None:
    body = build_body(
        request(ChatMessage(role="user", content="x"), reasoning_effort="low", temperature=0.5),
        model="claude-sonnet-5", traits=traits_for("claude-sonnet-5"),
    )
    assert body["thinking"]["type"] == "adaptive"
    assert body["output_config"] == {"effort": "low"}
    assert "temperature" not in body


def test_the_models_api_capabilities_override_the_static_guess() -> None:
    entry = {"id": "claude-future", "max_tokens": 8000, "capabilities": {
        "thinking": {"supported": True, "types": {"adaptive": {"supported": False},
                                                 "enabled": {"supported": True}}},
        "effort": {"supported": False},
    }}
    traits = traits_for("claude-future", entry)
    assert (traits.adaptive, traits.budget, traits.max_output) == (False, True, 8000)
    body = build_body(request(ChatMessage(role="user", content="x"), max_tokens=16384),
                      model="claude-future", traits=traits)
    assert body["max_tokens"] == 8000


async def test_a_rejected_thinking_signature_is_resent_once_without_reasoning() -> None:
    api = Api(
        error_response(400, "invalid_request_error",
                       "messages.1.content.0: Invalid `signature` in `thinking` block. "
                       "The block is bound to a different conversation."),
        stream_response(start(), *text_block(0, "ok"), *finish()),
    )
    req = request(
        ChatMessage(role="user", content="go"),
        ChatMessage(role="assistant", content="a",
                    reasoning_blocks=[{"type": "thinking", "thinking": "t", "signature": "s"}]),
        ChatMessage(role="user", content="again"),
    )
    events = await collect(provider(api), req)
    assert events[-1] == TurnDone("stop")
    first, second = api.body(0), api.body(1)
    assert first["messages"][1]["content"][0]["type"] == "thinking"
    assert [b["type"] for b in second["messages"][1]["content"]] == ["text"]


# ---------------------------------------------------------------------------
# Usage and cost
# ---------------------------------------------------------------------------


async def test_cache_reads_and_writes_count_toward_the_prompt_and_are_priced() -> None:
    api = Api(stream_response(
        start(input_tokens=10, cache_creation_input_tokens=1000, cache_read_input_tokens=5000),
        *text_block(0, "x"),
        *finish(output_tokens=200),
    ))
    events = await collect(provider(api), request(ChatMessage(role="user", content="x")))
    usage = next(e for e in events if isinstance(e, Usage))

    assert usage.input_tokens == 6010, "the whole prompt, as the context meter reads it"
    assert usage.cached_tokens == 5000
    assert usage.cache_write_tokens == 1000
    # claude-opus-5: $5 in, $25 out, writes 1.25x input, reads 0.1x input.
    expected = (10 * 5 + 1000 * 5 * 1.25 + 5000 * 0.5 + 200 * 25) / 1_000_000
    assert usage.cost_usd == pytest.approx(expected)

    ledger = Ledger()
    ledger.add(usage)
    ledger.add(usage)
    assert (ledger.cached_tokens, ledger.cache_write_tokens) == (10000, 2000)
    assert ledger.cost_usd == pytest.approx(2 * expected)
    assert ledger.last_input_tokens == 6010


async def test_a_model_without_a_known_price_reports_no_cost_rather_than_a_guess() -> None:
    api = Api(stream_response(start(model="claude-experimental-9"), *text_block(0, "x"),
                              *finish()))
    events = await collect(provider(api), request(ChatMessage(role="user", content="x"),
                                                  model="claude-experimental-9"))
    assert next(e for e in events if isinstance(e, Usage)).cost_usd is None


# ---------------------------------------------------------------------------
# Errors and retries
# ---------------------------------------------------------------------------


async def test_an_overloaded_api_is_retried_after_the_wait_it_asks_for() -> None:
    sleeps = Sleeps()
    api = Api(
        error_response(529, "overloaded_error", "Overloaded", **{"retry-after": "3"}),
        error_response(429, "rate_limit_error", "slow down", **{"retry-after": "7"}),
        stream_response(start(), *text_block(0, "fine"), *finish()),
    )
    events = await collect(provider(api, sleep=sleeps), request(ChatMessage(role="user",
                                                                            content="x")))
    assert events[-1] == TurnDone("stop")
    assert sleeps.waits == [3.0, 7.0]
    assert len(api.requests) == 3


async def test_a_server_error_without_retry_after_backs_off_exponentially() -> None:
    sleeps = Sleeps()
    api = Api(
        error_response(500, "api_error", "boom"),
        error_response(503, "api_error", "boom"),
        stream_response(start(), *text_block(0, "fine"), *finish()),
    )
    await collect(provider(api, sleep=sleeps), request(ChatMessage(role="user", content="x")))
    assert len(sleeps.waits) == 2
    assert 1.0 <= sleeps.waits[0] <= 1.25 and 2.0 <= sleeps.waits[1] <= 2.5


async def test_retries_run_out_and_the_last_error_is_surfaced() -> None:
    api = Api(*(error_response(529, "overloaded_error", "Overloaded") for _ in range(3)))
    with pytest.raises(ProviderError) as info:
        await collect(provider(api, retries=2), request(ChatMessage(role="user", content="x")))
    assert "overloaded_error" in str(info.value) and "3 attempts" in str(info.value)
    assert len(api.requests) == 3


async def test_an_overloaded_event_before_any_output_is_retried() -> None:
    api = Api(
        stream_response(start(), {"type": "error",
                                  "error": {"type": "overloaded_error", "message": "Overloaded"}}),
        stream_response(start(), *text_block(0, "second try"), *finish()),
    )
    events = await collect(provider(api), request(ChatMessage(role="user", content="x")))
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["second try"]


async def test_an_error_after_output_is_surfaced_not_retried_under_the_reader() -> None:
    """Retrying now would show the reader the same sentence twice."""
    api = Api(
        stream_response(start(), *text_block(0, "partial"),
                        {"type": "error", "error": {"type": "overloaded_error",
                                                    "message": "Overloaded"}}),
    )
    seen: list = []
    with pytest.raises(ProviderError, match="overloaded_error"):
        async for ev in provider(api).stream_chat(request(ChatMessage(role="user",
                                                                      content="x"))):
            seen.append(ev)
    assert [e.text for e in seen if isinstance(e, TextDelta)] == ["partial"]
    assert any(isinstance(e, Usage) for e in seen), "what was spent still reaches the ledger"
    assert len(api.requests) == 1


async def test_a_stream_that_stops_short_is_not_taken_for_a_finished_answer() -> None:
    api = Api(stream_response(start(), *text_block(0, "half")))
    with pytest.raises(ProviderError, match="ended early"):
        await collect(provider(api), request(ChatMessage(role="user", content="x")))


async def test_a_bad_request_surfaces_the_apis_own_message_and_is_not_retried() -> None:
    api = Api(error_response(400, "invalid_request_error", "max_tokens: too large"))
    with pytest.raises(ProviderError) as info:
        await collect(provider(api), request(ChatMessage(role="user", content="x")))
    message = str(info.value)
    assert "400" in message and "max_tokens: too large" in message and "req_test" in message
    assert KEY not in message
    assert len(api.requests) == 1


async def test_no_key_fails_before_anything_is_sent() -> None:
    api = Api()
    p = AnthropicProvider("", None, transport=httpx.MockTransport(api))
    with pytest.raises(ProviderError, match="QUICKCODE_ANTHROPIC_API_KEY"):
        await collect(p, request(ChatMessage(role="user", content="x")))
    assert api.requests == []
    assert await p.list_models() == []


def test_retry_after_reads_seconds_and_http_dates() -> None:
    assert parse_retry_after("2.5") == 2.5
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0
    assert parse_retry_after("soon") is None
    assert RetryPolicy(max_retry_after_s=60).delay(0, "3600") == 60


# ---------------------------------------------------------------------------
# Configuration edges
# ---------------------------------------------------------------------------


def test_openrouter_slugs_translate_and_other_vendors_are_refused() -> None:
    assert normalize_model("anthropic/claude-opus-4.8") == "claude-opus-4-8"
    assert normalize_model("anthropic/claude-sonnet-4.5:thinking") == "claude-sonnet-4-5"
    assert normalize_model("claude-opus-5") == "claude-opus-5"
    with pytest.raises(ProviderError, match="not an Anthropic model"):
        normalize_model("openai/gpt-5")


def test_an_openrouter_base_url_never_receives_the_anthropic_key() -> None:
    assert resolve_base_url("https://openrouter.ai/api/v1") == "https://api.anthropic.com"
    assert resolve_base_url("") == "https://api.anthropic.com"
    assert resolve_base_url("https://proxy.example/v1/") == "https://proxy.example"


async def test_a_gateway_gets_no_first_party_only_tool_fields() -> None:
    api = Api(stream_response(start(), *text_block(0, "x"), *finish()))
    req = request(ChatMessage(role="user", content="x"), tools=[GLOB])
    await collect(provider(api, base_url="https://gateway.example"), req)
    assert api.requests[0].url.host == "gateway.example"
    assert "eager_input_streaming" not in api.body()["tools"][0]


async def test_models_are_listed_across_pages_with_context_and_price() -> None:
    def page(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == KEY
        if "after_id" not in request.url.params:
            return httpx.Response(200, json={
                "data": [{"id": "claude-opus-5", "display_name": "Claude Opus 5",
                          "max_input_tokens": 1_000_000, "max_tokens": 128000,
                          "capabilities": None}],
                "has_more": True, "first_id": "claude-opus-5", "last_id": "claude-opus-5"})
        assert request.url.params["after_id"] == "claude-opus-5"
        return httpx.Response(200, json={
            "data": [{"id": "claude-haiku-4-5", "display_name": "Claude Haiku 4.5",
                      "max_input_tokens": 200_000}],
            "has_more": False, "first_id": "claude-haiku-4-5", "last_id": "claude-haiku-4-5"})

    api = Api(page, page)
    models = await provider(api).list_models()
    assert [(m.id, m.name, m.context_length) for m in models] == [
        ("claude-opus-5", "Claude Opus 5", 1_000_000),
        ("claude-haiku-4-5", "Claude Haiku 4.5", 200_000),
    ]
    assert (models[0].prompt_price, models[0].completion_price) == (5.0, 25.0)


async def test_a_failed_model_listing_is_an_empty_catalog_not_a_crash() -> None:
    api = Api(error_response(401, "authentication_error", "invalid x-api-key"))
    assert await provider(api).list_models() == []


def test_the_openrouter_adapter_marks_the_same_two_breakpoints_for_claude() -> None:
    """Anthropic models behind OpenRouter get the cache controls too: the
    OpenAI-compatible adapter already sends them on the system message and the
    conversation tail, and OpenRouter forwards them."""
    from quickcode.providers.openai_compat import OpenAICompatProvider

    history = History("SYS")
    history.push_user("hi")
    wire = [OpenAICompatProvider._translate_message(m) for m in history.build_messages()]
    assert wire[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert wire[-1]["content"][0]["cache_control"] == {"type": "ephemeral"}


async def test_a_dropped_connection_before_any_output_is_retried() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    sleeps = Sleeps()
    api = Api(refuse, stream_response(start(), *text_block(0, "back"), *finish()))
    events = await collect(provider(api, sleep=sleeps), request(ChatMessage(role="user",
                                                                            content="x")))
    assert events[-1] == TurnDone("stop")
    assert len(sleeps.waits) == 1


async def test_undecodable_tool_output_costs_a_character_not_the_request() -> None:
    api = Api(stream_response(start(), *text_block(0, "ok"), *finish()))
    events = await collect(provider(api), request(ChatMessage(role="user",
                                                              content="bad byte: \udcff")))
    assert events[-1] == TurnDone("stop")
    assert api.body()["messages"][0]["content"][-1]["text"] == "bad byte: ?"
