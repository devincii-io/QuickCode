"""What a hook's exit code and output mean.

Exit code first, because every shell script can produce one:

* ``0``  fine. Stdout may carry a JSON object with a structured answer.
* ``2``  block. Stderr is the reason, and it goes where the event sends it:
  back to the model for a tool call, to the user for a refused prompt.
* anything else, a crash, a missing program: a non-blocking error the user is
  shown. The call goes ahead -- a broken hook fails open, as in Claude Code, so
  a guard that must hold should exit 2 rather than trust a crash.

The JSON keys are Claude Code's, so a hook written for one runs on the other.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

BLOCK_EXIT = 2

# What may reach the model from one hook. Generous for context, but bounded:
# a hook that cats a log file should not be able to fill the context window.
TEXT_CAP = 10_000

Outcome = Literal["ok", "block", "ask", "error", "timeout", "interrupted"]

# Events whose plain (non-JSON) stdout on exit 0 becomes context for the model.
_STDOUT_IS_CONTEXT = frozenset({"UserPromptSubmit", "SessionStart"})


@dataclass(frozen=True)
class Verdict:
    outcome: Outcome
    # block / ask: why. error / timeout: what went wrong.
    reason: str = ""
    # Additional context for the model.
    context: str = ""
    # ``systemMessage``: for the user, never the model.
    message: str = ""
    # The decision the hook stated, verbatim, when it stated one. An ``allow``
    # is recorded and acted on by nobody -- see ``core.hooks.tighten``.
    decision: str = ""


def cap(text: str, limit: int = TEXT_CAP) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n[... {len(text) - limit} more characters from the hook cut]"


def structured(stdout: str) -> dict[str, Any] | None:
    """The JSON object a hook printed, if it printed one.

    The whole of stdout first. Then its last non-empty line, because hooks run
    in a login shell and a profile that echoes something would otherwise turn
    a well-formed answer into plain text -- and for a guard, plain text means
    no decision at all.
    """
    text = stdout.strip()
    if not text:
        return None
    candidates = [text]
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) > 1:
        candidates.append(lines[-1].strip())
    for candidate in candidates:
        if not candidate.startswith("{"):
            continue
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def interpret(
    event: str, *, exit_code: int | None, stdout: str, stderr: str,
    timed_out: bool = False, timeout_s: float = 0.0, spawn_error: str = "",
) -> Verdict:
    if spawn_error:
        return Verdict("error", reason=cap(f"could not start: {spawn_error}", 500))
    if timed_out:
        return Verdict("timeout", reason=f"timed out after {timeout_s:g}s; its process "
                                         "tree was stopped")
    if exit_code == BLOCK_EXIT:
        return Verdict("block", reason=cap(stderr) or "(the hook exited 2 without a reason)",
                       decision="block")
    if exit_code != 0:
        detail = cap(stderr or stdout, 500)
        return Verdict("error", reason=f"exit {exit_code}" + (f": {detail}" if detail else ""))

    data = structured(stdout)
    if data is None:
        context = cap(stdout) if event in _STDOUT_IS_CONTEXT else ""
        return Verdict("ok", context=context)

    specific = data.get("hookSpecificOutput")
    specific = specific if isinstance(specific, dict) else {}
    message = cap(_text(data.get("systemMessage")), 1000)
    context = cap(_text(specific.get("additionalContext"))
                  or _text(data.get("additionalContext")))

    decision = ""
    reason = ""
    pd = _text(specific.get("permissionDecision")).lower()
    if event == "PreToolUse" and pd in ("allow", "deny", "ask"):
        decision, reason = pd, _text(specific.get("permissionDecisionReason"))
    else:
        stated = _text(data.get("decision")).lower()
        if stated in ("block", "deny"):
            decision = "block"
        elif stated in ("approve", "allow"):
            decision = "allow"
        elif stated == "ask" and event == "PreToolUse":
            decision = "ask"
        reason = _text(data.get("reason"))

    if decision in ("block", "deny"):
        return Verdict("block", reason=cap(reason) or "(the hook gave no reason)",
                       context=context, message=message, decision=decision)
    if decision == "ask":
        return Verdict("ask", reason=cap(reason), context=context, message=message,
                       decision=decision)
    return Verdict("ok", context=context, message=message, decision=decision)
