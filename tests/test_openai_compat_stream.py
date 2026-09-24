"""How the OpenAI-compatible adapter turns wire chunks into agent events.

Driven with a fake SDK client, so every chunk shape here is one a real
compatible server has been seen to send -- not only the shape OpenAI does.
"""

from __future__ import annotations

from types import SimpleNamespace as NS

from quickcode.core.events import ToolCallEnd, ToolCallStart, TurnDone
from quickcode.providers.base import ChatMessage, ChatRequest
from quickcode.providers.openai_compat import OpenAICompatProvider


class _Stream:
    def __init__(self, chunks: list) -> None:
        self.chunks = chunks
        self.closed = False

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for c in self.chunks:
            yield c

    async def close(self) -> None:
        self.closed = True


class _Completions:
    def __init__(self, streams: list[_Stream]) -> None:
        self.streams = list(streams)

    async def create(self, **_kwargs):
        return self.streams.pop(0)


def _provider(*streams: _Stream) -> OpenAICompatProvider:
    p = OpenAICompatProvider("https://example.test/v1", "key")
    p._client = NS(chat=NS(completions=_Completions(list(streams))))
    return p


def _req() -> ChatRequest:
    return ChatRequest(model="m", messages=[ChatMessage(role="user", content="hi")])


def _tc(index, cid, name, args):
    return NS(index=index, id=cid, function=NS(name=name, arguments=args))


def _chunk(*calls, finish=None, content=None):
    delta = NS(content=content, tool_calls=list(calls) or None)
    return NS(choices=[NS(delta=delta, finish_reason=finish)], usage=None)


async def _events(provider) -> list:
    return [ev async for ev in provider.stream_chat(_req())]


def _ends(events) -> list[tuple[str, str, str]]:
    return [(e.id, e.name, e.arguments) for e in events if isinstance(e, ToolCallEnd)]


async def test_parallel_calls_reusing_one_index_are_kept_apart():
    """Some servers number every parallel call 0 and tell them apart by id."""
    provider = _provider(_Stream([
        _chunk(_tc(0, "a", "read", '{"file_path": "x"}')),
        _chunk(_tc(0, "b", "glob", '{"pattern": "*"}')),
        _chunk(finish="tool_calls"),
    ]))

    ends = _ends(await _events(provider))

    assert ends == [("a", "read", '{"file_path": "x"}'), ("b", "glob", '{"pattern": "*"}')]


async def test_an_id_repeated_on_every_chunk_is_still_one_call():
    provider = _provider(_Stream([
        _chunk(_tc(0, "a", "read", '{"file_')),
        _chunk(_tc(0, "a", None, 'path": "x"}')),
        _chunk(finish="tool_calls"),
    ]))

    assert _ends(await _events(provider)) == [("a", "read", '{"file_path": "x"}')]


async def test_calls_without_an_index_are_told_apart_by_id():
    provider = _provider(_Stream([
        _chunk(_tc(None, "a", "read", '{"file_path": "x"}')),
        _chunk(_tc(None, "b", "glob", '{"pattern": "*"}')),
        _chunk(finish="tool_calls"),
    ]))

    ends = _ends(await _events(provider))

    assert [(cid, name) for cid, name, _ in ends] == [("a", "read"), ("b", "glob")]


async def test_calls_the_server_sent_no_id_for_get_ids_unique_to_the_session():
    """``call_0`` in every round made round two's results land on round one's cards."""
    provider = _provider(
        _Stream([_chunk(_tc(0, None, "read", "{}")), _chunk(finish="tool_calls")]),
        _Stream([_chunk(_tc(0, None, "read", "{}")), _chunk(finish="tool_calls")]),
    )

    first = [e.id for e in await _events(provider) if isinstance(e, ToolCallStart)]
    second = [e.id for e in await _events(provider) if isinstance(e, ToolCallStart)]

    assert first and second and all(first + second)
    assert set(first).isdisjoint(second)


async def test_the_http_stream_is_closed_when_the_reader_stops_early():
    stream = _Stream([_chunk(content="one"), _chunk(content="two"), _chunk(finish="stop")])
    provider = _provider(stream)

    gen = provider.stream_chat(_req())
    await gen.__anext__()
    await gen.aclose()

    assert stream.closed


async def test_the_http_stream_is_closed_after_a_full_read():
    stream = _Stream([_chunk(content="one"), _chunk(finish="stop")])
    provider = _provider(stream)

    events = await _events(provider)

    assert isinstance(events[-1], TurnDone)
    assert stream.closed
