"""``ChatRequest`` -> Messages API request body.

Two properties matter more than anything else here:

* **Byte stability.** The prompt cache is a prefix match over tools, system and
  messages, so the same conversation must serialize to the same bytes on every
  request. Nothing in this module reads a clock, a random source or a set.
* **Replay fidelity.** Thinking blocks go back exactly as they arrived, in the
  position they arrived in -- the API rejects a tool-use turn whose thinking
  was dropped or edited.
"""

from __future__ import annotations

import json
from typing import Any

from quickcode.providers.anthropic.models import FALLBACK_MAX_TOKENS, Traits
from quickcode.providers.base import ChatMessage, ChatRequest, ToolSchema

EPHEMERAL = {"type": "ephemeral"}

# History writes a failed tool's result with this prefix (core/history.py);
# the Messages API has a flag for it, which the model reads more reliably.
ERROR_PREFIX = "[error] "

EMPTY_RESULT = "(no output)"

# Old models take a fixed thinking budget; these are what the effort names map
# to. Kept well under the 16384-token default response budget.
_BUDGETS = {"low": 2048, "medium": 6144, "high": 12288}
_MIN_BUDGET = 1024

_REASONING_TYPES = ("thinking", "redacted_thinking")


def build_body(
    req: ChatRequest,
    *,
    model: str,
    traits: Traits,
    eager_tools: bool = False,
    strip_reasoning: bool = False,
) -> dict[str, Any]:
    system, rest = _split_system(req.messages)
    # A request with tool traffic in its history but no tools declared (the
    # compaction summary) is refused by the API, so that history is sent as
    # plain text instead -- and without reasoning blocks, which may not survive
    # having the tool_use they preceded rewritten.
    flatten = not req.tools and any(m.tool_calls or m.role == "tool" for m in rest)
    messages = translate_messages(
        rest, flatten_tools=flatten, strip_reasoning=strip_reasoning or flatten
    )

    max_tokens = req.max_tokens or FALLBACK_MAX_TOKENS
    if traits.max_output:
        max_tokens = min(max_tokens, traits.max_output)

    body: dict[str, Any] = {"model": model, "max_tokens": max_tokens, "stream": True}
    if system:
        body["system"] = system
    body["messages"] = messages
    if req.tools:
        body["tools"] = [translate_tool(t, eager=eager_tools) for t in req.tools]

    thinking = _thinking(req, traits, max_tokens)
    if thinking is not None:
        body["thinking"] = thinking
    effort = req.reasoning_effort
    if traits.adaptive and effort and effort in traits.effort:
        body["output_config"] = {"effort": effort}
    # Sampling parameters are rejected by the current models and are not
    # allowed alongside thinking on the older ones, so they only travel when
    # thinking is off. The API's range is 0-1; config allows up to 2.
    if thinking is None and req.temperature is not None:
        body["temperature"] = max(0.0, min(float(req.temperature), 1.0))
    return body


def _thinking(req: ChatRequest, traits: Traits, max_tokens: int) -> dict[str, Any] | None:
    if traits.adaptive:
        # Summarized, because the reasoning pane is empty otherwise: current
        # models default to omitting the text.
        return {"type": "adaptive", "display": "summarized"}
    if traits.budget and req.reasoning_effort:
        budget = min(_BUDGETS.get(req.reasoning_effort, _BUDGETS["medium"]), max_tokens - 1)
        if budget >= _MIN_BUDGET:
            return {"type": "enabled", "budget_tokens": budget}
    return None


def _split_system(messages: list[ChatMessage]) -> tuple[list[dict[str, Any]], list[ChatMessage]]:
    """Leading system messages become the top-level ``system`` blocks, and the
    last of them carries the breakpoint that caches tools + system together."""
    blocks: list[dict[str, Any]] = []
    i = 0
    while i < len(messages) and messages[i].role == "system":
        if messages[i].content:
            blocks.append({"type": "text", "text": messages[i].content})
        i += 1
    if blocks:
        blocks[-1]["cache_control"] = dict(EPHEMERAL)
    return blocks, messages[i:]


def translate_tool(tool: ToolSchema, *, eager: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.parameters or {"type": "object", "properties": {}},
    }
    if eager:
        out["eager_input_streaming"] = True
    return out


def tool_input(arguments: Any) -> dict[str, Any]:
    """A recorded call's arguments as the object ``tool_use.input`` must be.

    Arguments that never parsed (the loop already answered those with an error
    result) are kept visible rather than silently emptied, so the model can see
    what it sent.
    """
    if isinstance(arguments, dict):
        return arguments
    try:
        parsed = json.loads(arguments or "{}")
    except (TypeError, ValueError):
        return {"_invalid_json": str(arguments)}
    return parsed if isinstance(parsed, dict) else {"_invalid_json": str(arguments)}


def replayable(block: Any) -> bool:
    """A reasoning block the API will accept back: signed thinking, or a
    redacted block with its opaque payload."""
    if not isinstance(block, dict):
        return False
    if block.get("type") == "thinking":
        return bool(block.get("signature"))
    if block.get("type") == "redacted_thinking":
        return bool(block.get("data"))
    return False


def translate_messages(
    messages: list[ChatMessage],
    *,
    flatten_tools: bool = False,
    strip_reasoning: bool = False,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for msg in messages:
        role = "assistant" if msg.role == "assistant" else "user"
        blocks = _blocks(msg, flatten_tools=flatten_tools, strip_reasoning=strip_reasoning)
        if all(b["type"] in _REASONING_TYPES for b in blocks):
            # Nothing but reasoning (a turn cut off while thinking) is not a
            # turn the API will take back.
            continue
        if msg.cache_control:
            blocks[-1]["cache_control"] = dict(EPHEMERAL)
        if out and out[-1]["role"] == role:
            # Consecutive same-role messages are one turn on this API: tool
            # results followed by a reminder, or a user turn after a round that
            # failed before the model answered.
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": blocks})
    if out and out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": [{"type": "text", "text": "(continued)"}]})
    return out


def _blocks(
    msg: ChatMessage, *, flatten_tools: bool, strip_reasoning: bool
) -> list[dict[str, Any]]:
    if msg.role == "tool":
        body = msg.content or ""
        is_error = body.startswith(ERROR_PREFIX)
        if is_error:
            body = body[len(ERROR_PREFIX):]
        if flatten_tools:
            label = "error" if is_error else "result"
            return [{"type": "text", "text": f"[tool {label} {msg.name or ''}]\n{body}".rstrip()}]
        block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": msg.tool_call_id or "",
            "content": body or EMPTY_RESULT,
        }
        if is_error:
            block["is_error"] = True
        return [block]

    blocks: list[dict[str, Any]] = []
    if msg.role == "assistant" and not strip_reasoning:
        blocks.extend(dict(b) for b in msg.reasoning_blocks if replayable(b))
    if msg.content:
        blocks.append({"type": "text", "text": msg.content})
    if msg.role == "assistant":
        for tc in msg.tool_calls:
            if flatten_tools:
                args = tc.get("arguments")
                args = args if isinstance(args, str) else json.dumps(args)
                blocks.append({"type": "text", "text": f"[tool call {tc.get('name', '')}] {args}"})
            else:
                blocks.append({
                    "type": "tool_use",
                    "id": tc.get("id", ""),
                    "name": tc.get("name", ""),
                    "input": tool_input(tc.get("arguments")),
                })
    return blocks
