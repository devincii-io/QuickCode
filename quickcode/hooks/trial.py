"""A test run: one hook, a sample payload, and what its answer would do.

Built from the same parts a session uses -- ``runner.run_command`` starts the
command with the same shell, environment and deadline, and
``protocol.interpret`` reads its answer -- so a hook that behaves here behaves
in a session. What is left out is the session: no conversation is opened, no
``hook_run`` record is written, nothing reaches a model, and the payload says
so (``session_id`` is ``test-run``, ``transcript_path`` is empty).

The caller decides whether the hook may run at all; a project hook in an
untrusted project may not, and ``server/hooks_api.py`` refuses before this is
reached.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from quickcode.hooks.config import EVENTS, TOOL_EVENTS, HookCommand, matcher_matches
from quickcode.hooks.protocol import Verdict, interpret
from quickcode.hooks.runner import run_command
from quickcode.tools.base import ReadRegistry, ToolCtx

TEST_SESSION_ID = "test-run"

# What the page shows of each stream. The runner already stops reading at
# 256 KiB; a result panel needs far less than that to be useful.
SHOWN_CHARS = 16_000


def sample_input(tool_name: str, cwd: Path) -> dict[str, Any]:
    """Arguments shaped like the tool's own, for the tools a guard usually watches.

    Only sent to the hook: a test run never runs the tool.
    """
    example = str(Path(cwd) / "notes" / "example.txt")
    return {
        "bash": {"command": "echo hello", "description": "A test run from the Hooks page"},
        "write": {"file_path": example, "content": "hello\n"},
        "edit": {"file_path": example, "old_string": "hello", "new_string": "goodbye"},
        "read": {"file_path": str(Path(cwd) / "README.md")},
    }.get(tool_name, {})


class TrialError(ValueError):
    pass


@dataclass(frozen=True)
class Sample:
    event: str
    tool_name: str = ""
    tool_input: dict[str, Any] | None = None
    prompt: str = ""


def default_tool(matcher: str) -> str:
    """A tool the matcher selects, when it names one outright; else ``bash``."""
    for alt in (matcher or "").split("|"):
        alt = alt.strip().lower()
        if alt and alt != "*" and not any(c in alt for c in "*?["):
            return alt
    return "bash"


def sample_from(hook: HookCommand, body: dict[str, Any]) -> Sample:
    event = body.get("event") or hook.event
    if event not in EVENTS:
        raise TrialError(f"{event!r} is not a hook event; use one of {', '.join(EVENTS)}")
    tool_name = body.get("tool_name") or default_tool(hook.matcher)
    if not isinstance(tool_name, str):
        raise TrialError("tool_name must be a string")
    tool_input = body.get("tool_input")
    if tool_input is not None and not isinstance(tool_input, dict):
        raise TrialError("tool_input must be a JSON object of the tool's arguments")
    prompt = body.get("prompt")
    if prompt is not None and not isinstance(prompt, str):
        raise TrialError("prompt must be a string")
    return Sample(event=event, tool_name=tool_name.strip(), tool_input=tool_input,
                  prompt=prompt or "")


def payload(sample: Sample, *, cwd: Path, mode: str) -> dict[str, Any]:
    """What the hook reads on stdin: the fields a session sends for this event."""
    out: dict[str, Any] = {
        "session_id": TEST_SESSION_ID,
        "transcript_path": "",
        "cwd": str(cwd),
        "hook_event_name": sample.event,
        "permission_mode": mode,
        "agent_name": "main",
    }
    if sample.event in TOOL_EVENTS:
        tool_input = sample.tool_input
        if tool_input is None:
            tool_input = sample_input(sample.tool_name, cwd)
        out.update(tool_name=sample.tool_name, tool_input=tool_input,
                   tool_use_id="test_call")
        if sample.event == "PostToolUse":
            out["tool_response"] = {"content": "hello\n", "is_error": False}
    elif sample.event == "UserPromptSubmit":
        out["prompt"] = sample.prompt or "Hello from a test run on the Hooks page."
    elif sample.event == "Stop":
        out.update(stop_hook_active=False,
                   last_assistant_message="A test run: no model was called.")
    elif sample.event == "SessionStart":
        out["source"] = "startup"
    return out


_CONTEXT_GOES = {
    "PostToolUse": "Its context would be appended to the tool's result.",
    "UserPromptSubmit": "Its context would reach the model with your message.",
    "SessionStart": "Its context would reach the model with the first message.",
}


def effect(event: str, verdict: Verdict) -> str:
    """What this answer would do in a session, in one or two sentences."""
    said: list[str] = []
    if verdict.outcome in ("error", "timeout"):
        goes_ahead = {
            "PreToolUse": "the call would go ahead",
            "PostToolUse": "the result would reach the model unchanged",
            "UserPromptSubmit": "the message would be sent",
        }.get(event, "you would be told")
        said.append(f"The hook failed ({verdict.reason}). A failing hook fails open, so "
                    f"{goes_ahead}.")
    elif verdict.outcome == "block":
        said.append({
            "PreToolUse": "The call would be refused, and the model would read the reason "
                          "as the tool's result.",
            "PostToolUse": "The tool has already run by then; the reason would be sent to "
                           "the model as feedback.",
            "UserPromptSubmit": "The message would not be sent, and you would be shown the "
                                "reason.",
        }.get(event, f"A {event} hook cannot block anything; you would only be told."))
    elif verdict.outcome == "ask":
        said.append("You would be asked to confirm the call, even where the permission "
                    "engine would have let it run. In dontask mode it would be refused.")
    elif verdict.decision == "allow":
        said.append("The hook says allow. That is recorded and changes nothing: a hook "
                    "never skips a prompt the permission engine would show.")
    else:
        said.append({
            "PreToolUse": "The call would go ahead as the permission engine decides.",
            "PostToolUse": "The result would reach the model as it is.",
            "UserPromptSubmit": "The message would be sent.",
        }.get(event, "The hook ran; nothing else would happen."))
    if verdict.context:
        said.append(_CONTEXT_GOES.get(event, "Its context is not read for this event."))
    if verdict.message:
        said.append("Its systemMessage would appear in the transcript for you.")
    return " ".join(said)


def _mismatch(hook: HookCommand, sample: Sample) -> str:
    """Why a session would not have run this hook for this sample, or ""."""
    if sample.event != hook.event:
        return (f"In a session this hook runs on {hook.event} only; it was sent a "
                f"{sample.event} payload because the test asked for one.")
    if sample.event in TOOL_EVENTS and not matcher_matches(hook.matcher, sample.tool_name):
        return (f"In a session this hook would not see a {sample.tool_name} call: its "
                f"matcher is {hook.matcher!r}.")
    return ""


def _shown(text: str) -> tuple[str, bool]:
    if len(text) <= SHOWN_CHARS:
        return text, False
    return text[:SHOWN_CHARS], True


async def run_trial(
    hook: HookCommand, sample: Sample, *, cwd: Path, mode: str, platform: str,
    shell_name: str = "bash",
) -> dict[str, Any]:
    data = payload(sample, cwd=cwd, mode=mode)
    ctx = ToolCtx(cwd=cwd, read_registry=ReadRegistry(), shell_name=shell_name,
                  platform=platform)
    done = await run_command(hook.command, payload=data, ctx=ctx, timeout_s=hook.timeout_s)
    verdict = interpret(sample.event, exit_code=done.exit_code, stdout=done.stdout,
                        stderr=done.stderr, timed_out=done.timed_out,
                        timeout_s=hook.timeout_s, spawn_error=done.spawn_error)
    stdout, stdout_cut = _shown(done.stdout)
    stderr, stderr_cut = _shown(done.stderr)
    return {
        "event": sample.event,
        "payload": data,
        "note": _mismatch(hook, sample),
        "exit_code": done.exit_code,
        "stdout": stdout,
        "stdout_truncated": stdout_cut,
        "stderr": stderr,
        "stderr_truncated": stderr_cut,
        "ms": done.ms,
        "timed_out": done.timed_out,
        "spawn_error": done.spawn_error,
        "verdict": {
            "outcome": verdict.outcome,
            "decision": verdict.decision,
            "reason": verdict.reason,
            "context": verdict.context,
            "message": verdict.message,
        },
        "effect": effect(sample.event, verdict),
    }
