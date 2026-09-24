"""``hook_run``: one record per hook execution, in the session log.

A new event type rather than a new field on an old one -- the log's record
format is locked, and widening it additively is the only change it admits.

What is recorded is what a reader needs to audit a decision: which hook (by
plugin id, which names the command without repeating it), for which event and
tool, what it decided, how long it took, and the reason it gave. Never the
payload it was sent -- that repeats the tool's input, which is already in the
log once -- and never the command line, which is the user's own configuration
and may carry a token. The reason is the hook's own output, capped; it is text
the user or the model was shown anyway.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, ClassVar

from quickcode.session.wire import register_event

LOG_TEXT_CAP = 1000


def _capped(text: str) -> str:
    return text if len(text) <= LOG_TEXT_CAP else text[:LOG_TEXT_CAP] + " […]"


@dataclass
class HookRun:
    wire_type: ClassVar[str] = "hook_run"

    event: str
    # ``outcome`` is protocol.Outcome, plus "refused" for the one record that
    # says an untrusted project's hooks were not run.
    outcome: str
    hook_id: str = ""
    scope: str = ""
    tool: str = ""
    # The tool call this run was about, so a reader can put it beside the call.
    call_id: str = ""
    # The decision the hook stated, verbatim ("allow" | "deny" | "ask" | "block").
    decision: str = ""
    exit_code: int | None = None
    ms: int = 0
    reason: str = ""
    # A sentence for the transcript when the user should hear about this run;
    # empty when there is nothing to say.
    notice: str = ""

    def to_json(self) -> dict[str, Any]:
        out = asdict(self)
        out["reason"] = _capped(self.reason)
        out["notice"] = _capped(self.notice)
        return {"type": self.wire_type, **out}


register_event(HookRun, HookRun.to_json, logged=True)
