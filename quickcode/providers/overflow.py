"""Recognising a provider's "this request is longer than the context window".

``ProviderError`` carries only a message, and every backend words this refusal
its own way. The context guard (``core/context_guard.py``) needs two answers
from it: whether shrinking the request could help at all, and -- when the
message says -- how large the window is, so an agent that started without a
catalog entry (a subagent on another model, ``-p`` before its catalog arrives)
learns its own.
"""

from __future__ import annotations

import re

_OVERFLOW = re.compile(
    r"context_length_exceeded"  # OpenAI, Groq, OpenRouter: the error code
    r"|maximum context length"  # OpenAI, OpenRouter, vLLM, Mistral
    r"|prompt is too long"  # Anthropic
    # "exceeds the context window" (OpenAI), "exceed context limit" (Anthropic,
    # input + max_tokens), "exceeds the available context size" (llama.cpp)
    r"|exceed\w*\s+(?:the\s+)?(?:available\s+|model'?s\s+|maximum\s+)?context"
    r"|input token count\b.{0,80}\bexceeds"  # Gemini
    r"|request_too_large",  # Anthropic 413: the body, not the tokens -- cutting helps too
    re.IGNORECASE,
)
# A rate limit also talks about too many tokens, and its cure is waiting.
_NOT_OVERFLOW = re.compile(r"rate.?limit|tokens per min|\bTPM\b", re.IGNORECASE)

_N = r"(\d[\d,]*)"
_LIMITS = (
    re.compile(r"maximum context length (?:is|of) " + _N, re.IGNORECASE),
    re.compile(r"with " + _N + r" maximum context length", re.IGNORECASE),
    re.compile(r">\s*" + _N + r"\s+maximum", re.IGNORECASE),
    re.compile(r"context limit:\s*\d[\d,]*\s*\+\s*\d[\d,]*\s*>\s*" + _N, re.IGNORECASE),
    re.compile(r"maximum number of tokens allowed \(" + _N + r"\)", re.IGNORECASE),
    re.compile(r"available context size \(" + _N, re.IGNORECASE),
)


def is_context_overflow(message: str) -> bool:
    """Whether ``message`` refuses a request for being too long for the model."""
    return bool(_OVERFLOW.search(message or "")) and not _NOT_OVERFLOW.search(message or "")


def overflow_limit(message: str) -> int | None:
    """The context window a refusal names, in tokens, if it names one."""
    for pattern in _LIMITS:
        m = pattern.search(message or "")
        if m:
            n = int(m.group(1).replace(",", ""))
            if n > 0:
                return n
    return None
