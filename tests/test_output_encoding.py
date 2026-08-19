"""Output a child process wrote in some other encoding must not kill the turn.

The report was `UnicodeEncodeError: 'utf-8' codec can't encode character
'\\udce4' in position 167: surrogates not allowed`, with the command still
drawn as running eleven minutes later. `\\udce4` is byte 0xE4 -- `ä` in cp1252,
on a German Windows -- carried through `errors="surrogateescape"`, which
survives inside a str and only fails when something encodes it. Three things
downstream do: the session log, the WebSocket frame, and the provider request.
So the tool returned fine and the recorder died holding the result.
"""

from __future__ import annotations

import json

import pytest

from quickcode.tools.base import ToolResult, clean_text, decode_output

WORD = "Größe"          # Grösse, umlaut + eszett
UMLAUT_CP1252 = b"Gr\xf6\xdfe"
LONE_SURROGATE = "a\udce4b"


# ---- decoding what a command wrote ----


def test_utf8_output_is_read_as_utf8() -> None:
    assert decode_output(WORD.encode("utf-8")) == WORD


def test_output_in_the_systems_own_code_page_stays_readable() -> None:
    """The point of the fallback: `Größe`, not `Gr??e`."""
    got = decode_output(UMLAUT_CP1252)
    assert got == WORD or "�" in got  # exact on a cp1252 machine
    assert not _has_surrogate(got)


@pytest.mark.parametrize("raw", [
    b"",
    b"\xff\xfe\x00\x01",
    b"\x80\x81\x82",
    bytes(range(256)),
    b"plain ascii",
    "emoji \U0001f600".encode(),
])
def test_no_byte_sequence_survives_as_a_surrogate(raw: bytes) -> None:
    assert not _has_surrogate(decode_output(raw))


def test_every_single_byte_decodes_to_something_encodable() -> None:
    for i in range(256):
        text = decode_output(bytes([i]) + b"tail")
        text.encode("utf-8")  # must not raise


# ---- the boundary guarantee ----


def test_a_lone_surrogate_is_replaced_rather_than_carried() -> None:
    cleaned = clean_text(LONE_SURROGATE)
    assert not _has_surrogate(cleaned)
    assert cleaned == "a�b"


def test_clean_text_leaves_ordinary_text_alone() -> None:
    for text in ("", "plain", WORD, "emoji \U0001f600", "tab\tand\nnewline"):
        assert clean_text(text) == text


def test_cleaned_output_survives_the_three_encodes_that_used_to_fail() -> None:
    """The log write, the socket frame and the request body, in that order."""
    content = clean_text("banner: " + LONE_SURROGATE)

    json.dumps({"kind": "message", "content": content}, ensure_ascii=False).encode("utf-8")
    json.dumps({"type": "tool_result", "content": content}, ensure_ascii=False).encode("utf-8")
    json.dumps({"messages": [{"role": "tool", "content": content}]}).encode("utf-8")


def test_the_uncleaned_string_really_does_fail_those_encodes() -> None:
    """Without this, the tests above prove nothing."""
    with pytest.raises(UnicodeEncodeError):
        json.dumps({"content": LONE_SURROGATE}, ensure_ascii=False).encode("utf-8")


async def test_a_tool_returning_bad_bytes_does_not_break_the_loop(tmp_path) -> None:
    """The guarantee is at the loop's boundary, so an MCP server or a plugin
    tool gets it too -- not only the tools that decode their own bytes."""
    from pydantic import BaseModel

    from quickcode.core.loop import _run_tool
    from quickcode.tools.base import ReadRegistry, Tool, ToolCtx

    class NoArgs(BaseModel):
        pass

    class Rude(Tool):
        name = "rude"
        description = "returns what a foreign code page left behind"
        is_read_only = True
        Input = NoArgs

        async def run(self, input, ctx):  # noqa: A002
            return ToolResult(content="out: " + LONE_SURROGATE)

    class _Call:
        id = "c1"
        name = "rude"
        arguments = "{}"

    class _Agent:
        hooks: list = []
        ctx = ToolCtx(cwd=tmp_path, read_registry=ReadRegistry())

        class registry:
            @staticmethod
            def get(name):
                return Rude() if name == "rude" else None

        class permissions:
            @staticmethod
            def evaluate_tool(*_a, **_kw):
                from quickcode.core.permissions import Decision

                return Decision.allow, ""

    content, is_error, _meta = await _run_tool(_Agent(), _Call())

    assert not _has_surrogate(content), content
    assert content == "out: a�b", content
    json.dumps({"content": content}, ensure_ascii=False).encode("utf-8")


def _has_surrogate(text: str) -> bool:
    return any(0xD800 <= ord(ch) <= 0xDFFF for ch in text)
