"""What a subagent's report may carry back into its spawner's context.

A child may have read anything, so its final message is untrusted input by the
time the parent sees it. Two layers: ``neutralize`` defuses text that could be
read as the harness (or the chat template) speaking, and ``sanitize_report``
adds the marker that says the text came from a subagent at all.

The order against ``artifacts.maybe_offload`` matters. A long report is cut to
a head plus a file path, and the parent is told to read the file for the rest
-- so the file has to hold neutralized text too, or length alone is a way past
the sanitizer.
"""

from __future__ import annotations

import re

MARKER = "[quickcode: sanitized subagent report]"

# Tags that read as the harness talking, or as the delegation template, or as
# the wrapper the collector puts around a report (closing it early would put
# whatever follows outside the child's voice). ``worktree`` is the block the
# harness appends naming where an isolated child's work went; a child writing
# its own could point the spawner at some other branch. Matched whatever the
# case and wherever a tokenizer would forgive whitespace.
_HARNESS_TAGS = (
    "system-reminder", "subagent", "compaction-summary",
    "task", "objective", "context", "boundaries", "output_format", "worktree",
)
_TAG = re.compile(
    r"<(\s*/?\s*(?:" + "|".join(re.escape(t) for t in _HARNESS_TAGS) + r")\b[^<>]*)(>?)",
    re.IGNORECASE,
)
# Chat-template control tokens (``<|im_start|>``, ``<|eot_id|>`` ...). No
# legitimate report needs the literal spelling.
_CONTROL = re.compile(r"<\|([^<>|]{0,40})\|>")


def _defuse(match: re.Match[str]) -> str:
    return f"‹{match.group(1)}{'›' if match.group(2) else ''}"


def neutralize(text: str) -> str:
    """Defuse harness-impersonating syntax, leaving everything else alone.

    TOON is deliberately *not* on the list. What this mangles are tags that
    carry no author -- a ``<system-reminder>`` in a report reads as the harness
    speaking, and nothing in the surrounding text says otherwise. A TOON table
    carries no such authority: it is data, it arrives inside the marker and the
    ``<subagent id=... status=...>`` wrapper the collector adds, and a forged
    ``matches[3]{path,line,text}:`` block is worth exactly what the sentence
    "I found three matches" is worth from the same child. Mangling it would
    cost more than it buys: the subagents most likely to emit a TOON block are
    the search-and-report ones, whose findings *are* tool output they are
    quoting back.
    """
    text = _TAG.sub(_defuse, text)
    return _CONTROL.sub(lambda m: f"‹|{m.group(1)}|›", text)


def sanitize_report(text: str) -> str:
    """Neutralize ``text`` and mark it as a sanitized subagent report."""
    return f"{MARKER}\n" + neutralize(text).strip()
