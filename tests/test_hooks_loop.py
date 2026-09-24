"""Command hooks at the loop's seams, run for real.

Every hook here is a real process -- a small Python script started through the
same shell the bash tool uses -- driven by a real ``AgentInstance``, a real
``PermissionEngine`` and a scripted provider. The claims that matter are about
what reaches the model and what the permission engine still decides, so those
are what is asserted, not the hook plumbing in between.
"""

from __future__ import annotations

import asyncio
import json
import platform
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from quickcode.config import Config, Environment
from quickcode.core.agent import AgentInstance, PermissionOutcome
from quickcode.core.events import (
    AssembledToolCall,
    TextDelta,
    ToolCallEnd,
    ToolCallStart,
    TurnDone,
)
from quickcode.core.history import History
from quickcode.core.hooks import default_hooks
from quickcode.core.permissions import Mode, PermissionEngine, Rules
from quickcode.hooks.config import HookCommand, HookConfig
from quickcode.hooks.events import HookRun
from quickcode.hooks.plugin import CommandHooks, child_hooks
from quickcode.kernel.state import user_settings_path
from quickcode.server.manager import ConversationManager
from quickcode.session.recorder import TranscriptRecorder
from quickcode.session.store import SessionStore
from quickcode.tools.base import ReadRegistry, ToolCtx
from quickcode.tools.registry import default_registry
from tests.conftest import await_until

HOOK_SCRIPT = r'''
import json, os, subprocess, sys, time
mode, log = sys.argv[1], sys.argv[2]
payload = json.load(sys.stdin)
with open(log, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(payload) + "\n")
if mode == "block":
    print("not in this repo", file=sys.stderr)
    sys.exit(2)
if mode == "fail":
    print("boom", file=sys.stderr)
    sys.exit(1)
if mode == "print":
    with open(sys.argv[3], encoding="utf-8") as fh:
        sys.stdout.write(fh.read())
if mode == "sleep":
    time.sleep(float(sys.argv[3]))
'''


class Script:
    """Writes the hook script once and builds command lines that run it."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / "hook.py"
        self.path.write_text(HOOK_SCRIPT, encoding="utf-8")
        self.log = root / "hook-log.jsonl"
        self._n = 0

    def command(self, mode: str, output: object | None = None, extra: str = "") -> str:
        args = [sys.executable, str(self.path), mode, str(self.log)]
        if output is not None:
            self._n += 1
            out = self.root / f"out-{self._n}.txt"
            out.write_text(output if isinstance(output, str) else json.dumps(output),
                           encoding="utf-8")
            args.append(str(out))
        if extra:
            args.append(extra)
        return " ".join(f'"{Path(a).as_posix()}"' if "/" in a or "\\" in a else a
                        for a in args)

    def payloads(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]


def hook(event: str, command: str, matcher: str = "", timeout_s: float = 30) -> HookCommand:
    return HookCommand(event=event, command=command, scope="user", matcher=matcher,
                       timeout_s=timeout_s)


class ScriptedProvider:
    """One tool-call round per entry in ``rounds``, then a plain answer."""

    def __init__(self, rounds: list[list[AssembledToolCall]] | None = None) -> None:
        self.rounds = list(rounds or [])
        self.requests: list = []

    async def stream_chat(self, req) -> AsyncIterator:
        self.requests.append(req)
        if self.rounds:
            for call in self.rounds.pop(0):
                yield ToolCallStart(call.id, call.name)
                yield ToolCallEnd(call.id, call.name, call.arguments)
            yield TurnDone("tool_calls")
            return
        yield TextDelta("all done")
        yield TurnDone("stop")

    async def list_models(self):
        return []


class Asked:
    """A permission callback that answers ``allow`` and remembers being asked."""

    def __init__(self, allow: bool = False) -> None:
        self.allow = allow
        self.requests: list = []

    async def __call__(self, req):
        self.requests.append(req)
        return PermissionOutcome(allow=self.allow)


def make_agent(root: Path, provider, hooks: list[HookCommand], *, mode=Mode.ask,
               asked: Asked | None = None, rules: Rules | None = None,
               resumed: bool = False) -> AgentInstance:
    config = HookConfig(hooks=tuple(hooks))
    command_hooks = CommandHooks(lambda: config, session_id="conv-1",
                                 transcript_path=str(root / "log.jsonl"), resumed=resumed)
    ctx = ToolCtx(cwd=root, read_registry=ReadRegistry(), platform=sys.platform, extra={})
    registry = default_registry()
    return AgentInstance(
        name="main",
        provider=provider,
        registry=registry,
        history=History("SYS"),
        ctx=ctx,
        permissions=PermissionEngine(mode, rules or Rules(), root,
                                     specs=registry.permission_specs()),
        model="test/model",
        permission_cb=asked or Asked(),
        hooks=[*default_hooks(), command_hooks],
    )


def write_call(path: str, content: str = "hello", cid: str = "c1") -> AssembledToolCall:
    return AssembledToolCall(cid, "write", json.dumps({"file_path": path, "content": content}))


def runs(agent: AgentInstance) -> list[HookRun]:
    """Every HookRun the agent emitted, read off a subscription taken first."""
    return [ev for ev in agent._seen if isinstance(ev, HookRun)]


def watch(agent: AgentInstance) -> AgentInstance:
    q = agent.bus.subscribe(maxsize=0)
    agent._seen = []  # type: ignore[attr-defined]

    def drain():
        while not q.empty():
            agent._seen.append(q.get_nowait())  # type: ignore[attr-defined]

    agent._drain = drain  # type: ignore[attr-defined]
    return agent


def turn(agent: AgentInstance, text: str = "go") -> str:
    out = asyncio.run(agent.run_turn(text))
    agent._drain()  # type: ignore[attr-defined]
    return out


def tool_messages(agent: AgentInstance) -> list[str]:
    return [m.content for m in agent.history.messages if m.role == "tool"]


# ------------------------------------------------------------------ PreToolUse


def test_pre_tool_use_exit_2_blocks_the_call_and_tells_the_model_why(tmp_path):
    script = Script(tmp_path)
    agent = watch(make_agent(
        tmp_path, ScriptedProvider([[write_call("out.txt")]]),
        [hook("PreToolUse", script.command("block"), matcher="write")],
        mode=Mode.auto_edit,
    ))
    turn(agent)

    assert not (tmp_path / "out.txt").exists(), "the hook said no; the write still ran"
    [result] = tool_messages(agent)
    assert result.startswith("[error]")
    assert "not in this repo" in result

    [run] = runs(agent)
    assert (run.event, run.outcome, run.tool, run.call_id, run.exit_code) == (
        "PreToolUse", "block", "write", "c1", 2)
    [payload] = script.payloads()
    assert payload["hook_event_name"] == "PreToolUse"
    assert payload["tool_name"] == "write"
    assert payload["tool_input"] == {"file_path": "out.txt", "content": "hello"}
    assert payload["session_id"] == "conv-1"
    assert payload["cwd"] == str(tmp_path)
    assert payload["permission_mode"] == "auto-edit"


def test_a_hook_that_allows_does_not_skip_the_permission_prompt(tmp_path):
    """The engine asks for a write in ask mode; a hook's allow changes nothing."""
    script = Script(tmp_path)
    allow = {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                    "permissionDecision": "allow"}}
    asked = Asked(allow=False)
    agent = watch(make_agent(
        tmp_path, ScriptedProvider([[write_call("out.txt")]]),
        [hook("PreToolUse", script.command("print", allow))], mode=Mode.ask, asked=asked,
    ))
    turn(agent)

    assert len(asked.requests) == 1, "an allow from a hook skipped the prompt"
    assert not (tmp_path / "out.txt").exists()
    [run] = runs(agent)
    assert (run.outcome, run.decision) == ("ok", "allow")


def test_a_hook_that_allows_does_not_open_a_protected_path(tmp_path):
    """Protected paths prompt before any allow rule -- and before any hook."""
    script = Script(tmp_path)
    asked = Asked(allow=False)
    agent = make_agent(
        tmp_path, ScriptedProvider([[write_call(".env", "SECRET=1")]]),
        [hook("PreToolUse", script.command("print", {"decision": "approve"}))],
        mode=Mode.auto_edit, asked=asked, rules=Rules(allow=["write"]),
    )
    watch(agent)
    turn(agent)

    assert len(asked.requests) == 1
    assert not (tmp_path / ".env").exists()


def test_a_hook_can_demand_a_prompt_the_engine_would_have_skipped(tmp_path):
    script = Script(tmp_path)
    ask = {"hookSpecificOutput": {"permissionDecision": "ask",
                                  "permissionDecisionReason": "double-check writes"}}
    asked = Asked(allow=True)
    agent = watch(make_agent(
        tmp_path, ScriptedProvider([[write_call("out.txt")]]),
        [hook("PreToolUse", script.command("print", ask))], mode=Mode.auto_edit, asked=asked,
    ))
    turn(agent)

    assert len(asked.requests) == 1, "auto-edit allows writes; the hook's ask was ignored"
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "hello"
    [run] = runs(agent)
    assert run.outcome == "ask"
    assert "double-check writes" in run.notice


def test_a_demanded_prompt_is_a_refusal_in_dontask(tmp_path):
    """dontask never waits for a person, so an ask there is a no."""
    script = Script(tmp_path)
    asked = Asked(allow=True)
    agent = watch(make_agent(
        tmp_path, ScriptedProvider([[write_call("out.txt")]]),
        [hook("PreToolUse", script.command("print", {"decision": "ask"}))],
        mode=Mode.dontask, asked=asked, rules=Rules(allow=["write"]),
    ))
    turn(agent)

    assert asked.requests == []
    assert not (tmp_path / "out.txt").exists()
    assert "dontask" in tool_messages(agent)[0]


def test_a_call_the_engine_denies_never_reaches_the_hooks(tmp_path):
    script = Script(tmp_path)
    agent = watch(make_agent(
        tmp_path, ScriptedProvider([[write_call("out.txt")]]),
        [hook("PreToolUse", script.command("record"))], rules=Rules(deny=["write"]),
    ))
    turn(agent)
    assert script.payloads() == []
    assert "Blocked by permission rules" in tool_messages(agent)[0]


def test_a_matcher_selects_which_tools_a_hook_sees(tmp_path):
    script = Script(tmp_path)
    agent = watch(make_agent(
        tmp_path, ScriptedProvider([[write_call("out.txt")]]),
        [hook("PreToolUse", script.command("block"), matcher="bash|mcp__*")],
        mode=Mode.auto_edit,
    ))
    turn(agent)
    assert (tmp_path / "out.txt").exists()
    assert script.payloads() == []


def test_a_crashing_hook_fails_open_and_says_so(tmp_path):
    script = Script(tmp_path)
    agent = watch(make_agent(
        tmp_path, ScriptedProvider([[write_call("out.txt")]]),
        [hook("PreToolUse", script.command("fail"))], mode=Mode.auto_edit,
    ))
    turn(agent)
    assert (tmp_path / "out.txt").exists()
    [run] = runs(agent)
    assert run.outcome == "error" and run.exit_code == 1
    assert "boom" in run.notice and "went ahead" in run.notice


def test_a_hook_that_outlives_its_timeout_is_stopped_and_fails_open(tmp_path):
    script = Script(tmp_path)
    agent = watch(make_agent(
        tmp_path, ScriptedProvider([[write_call("out.txt")]]),
        [hook("PreToolUse", script.command("sleep", extra="30"), timeout_s=0.5)],
        mode=Mode.auto_edit,
    ))
    turn(agent)
    [run] = runs(agent)
    assert run.outcome == "timeout"
    assert run.ms < 10_000
    assert (tmp_path / "out.txt").exists()


# ------------------------------------------------------------------ PostToolUse


def test_post_tool_use_feedback_follows_the_result_it_does_not_replace_it(tmp_path):
    script = Script(tmp_path)
    feedback = {"decision": "block", "reason": "run the formatter on out.txt"}
    agent = watch(make_agent(
        tmp_path, ScriptedProvider([[write_call("out.txt")]]),
        [hook("PostToolUse", script.command("print", feedback), matcher="write")],
        mode=Mode.auto_edit,
    ))
    turn(agent)

    assert (tmp_path / "out.txt").exists()
    [result] = tool_messages(agent)
    assert not result.startswith("[error]")
    body, _, note = result.partition("<system-reminder>")
    assert body.strip(), "the tool's own answer is gone"
    assert "run the formatter on out.txt" in note
    [payload] = script.payloads()
    assert payload["tool_response"]["is_error"] is False
    assert payload["tool_response"]["content"] == body.strip()


def test_post_tool_use_exit_2_sends_stderr_to_the_model(tmp_path):
    script = Script(tmp_path)
    agent = watch(make_agent(
        tmp_path, ScriptedProvider([[write_call("out.txt")]]),
        [hook("PostToolUse", script.command("block"))], mode=Mode.auto_edit,
    ))
    turn(agent)
    assert "not in this repo" in tool_messages(agent)[0]


# ------------------------------------------------------------- UserPromptSubmit


def test_a_refused_prompt_never_reaches_the_model(tmp_path):
    script = Script(tmp_path)
    provider = ScriptedProvider()
    agent = watch(make_agent(tmp_path, provider,
                             [hook("UserPromptSubmit", script.command("block"))]))
    assert turn(agent, "deploy to prod") == ""

    assert provider.requests == []
    assert agent.history.messages == []
    [run] = runs(agent)
    assert run.outcome == "block"
    assert "not in this repo" in run.notice
    assert script.payloads()[0]["prompt"] == "deploy to prod"


def test_prompt_context_is_delivered_with_the_message(tmp_path):
    script = Script(tmp_path)
    provider = ScriptedProvider()
    agent = watch(make_agent(tmp_path, provider, [
        hook("UserPromptSubmit", script.command("print", "branch: feature/x")),
    ]))
    turn(agent, "hello")

    [user] = [m.content for m in provider.requests[0].messages if m.role == "user"]
    assert user.startswith("hello")
    assert "branch: feature/x" in user


# ------------------------------------------------------------ SessionStart, Stop


def test_session_start_runs_once_and_its_context_reaches_the_first_turn(tmp_path):
    script = Script(tmp_path)
    provider = ScriptedProvider()
    agent = watch(make_agent(tmp_path, provider, [
        hook("SessionStart", script.command("print", "remember the style guide")),
    ], resumed=True))
    turn(agent, "one")
    turn(agent, "two")

    starts = [p for p in script.payloads() if p["hook_event_name"] == "SessionStart"]
    assert len(starts) == 1
    assert starts[0]["source"] == "resume"
    first = [m.content for m in provider.requests[0].messages if m.role == "user"][0]
    assert "remember the style guide" in first


def test_stop_runs_when_a_turn_finishes_and_sees_the_answer(tmp_path):
    script = Script(tmp_path)
    agent = watch(make_agent(tmp_path, ScriptedProvider(),
                             [hook("Stop", script.command("record"))]))
    turn(agent)
    [payload] = script.payloads()
    assert payload["hook_event_name"] == "Stop"
    assert payload["last_assistant_message"] == "all done"


def test_interrupting_a_turn_stops_a_running_hook(tmp_path):
    script = Script(tmp_path)
    agent = watch(make_agent(tmp_path, ScriptedProvider(), [
        hook("Stop", script.command("sleep", extra="30"), timeout_s=60),
    ]))

    async def run_and_interrupt():
        task = asyncio.create_task(agent.run_turn("go"))
        for _ in range(500):
            await asyncio.sleep(0.02)
            if script.payloads():
                break
        agent.cancel()
        return await asyncio.wait_for(task, timeout=15)

    asyncio.run(run_and_interrupt())
    agent._drain()  # type: ignore[attr-defined]
    assert [r.outcome for r in runs(agent)] == ["interrupted"]


# ------------------------------------------------------------------- subagents


def test_a_subagent_is_gated_by_the_same_tool_hooks_and_nothing_else(tmp_path):
    script = Script(tmp_path)
    parent = make_agent(tmp_path, ScriptedProvider(), [
        hook("PreToolUse", script.command("block")),
        hook("UserPromptSubmit", script.command("block")),
        hook("Stop", script.command("block")),
    ])
    hooks = child_hooks(parent.hooks)
    ctx = ToolCtx(cwd=tmp_path, read_registry=ReadRegistry(), platform=sys.platform)
    registry = default_registry()
    child = watch(AgentInstance(
        name="explore-1", provider=ScriptedProvider([[write_call("child.txt")]]),
        registry=registry, history=History("SYS"), ctx=ctx,
        permissions=PermissionEngine(Mode.auto_edit, Rules(), tmp_path,
                                     specs=registry.permission_specs()),
        model="test/model", permission_cb=Asked(), hooks=hooks,
    ))
    turn(child)

    assert not (tmp_path / "child.txt").exists()
    events = [p["hook_event_name"] for p in script.payloads()]
    assert events == ["PreToolUse"], "a delegation is not a user prompt, nor its end a Stop"
    assert script.payloads()[0]["agent_name"] == "explore-1"


# ------------------------------------------------------------------ the log


def test_hook_runs_are_logged_without_the_command_or_the_tool_input(tmp_path):
    script = Script(tmp_path)
    secret_command = script.command("block") + " TOKEN=abc123"
    agent = make_agent(
        tmp_path, ScriptedProvider([[write_call("out.txt", content="PAYLOAD-XYZ")]]),
        [hook("PreToolUse", secret_command)], mode=Mode.auto_edit,
    )
    store = SessionStore(tmp_path, "conv-1")
    rec = TranscriptRecorder(store)
    asyncio.run(rec.record_turn(agent, "go"))

    events = store.load_events()
    [logged] = [e for e in events if e["type"] == "hook_run"]
    assert logged["event"] == "PreToolUse" and logged["outcome"] == "block"
    assert logged["hook_id"].startswith("hook.cmd.user.pre_tool_use.")
    raw = json.dumps(logged)
    assert "abc123" not in raw
    assert "PAYLOAD-XYZ" not in raw
    assert logged["seq"] > 0


@pytest.mark.parametrize("resumed, source", [(False, "startup"), (True, "resume")])
def test_session_start_names_its_source(tmp_path, resumed, source):
    script = Script(tmp_path)
    agent = watch(make_agent(tmp_path, ScriptedProvider(),
                             [hook("SessionStart", script.command("record"))],
                             resumed=resumed))
    turn(agent)
    assert script.payloads()[0]["source"] == source


def test_an_untrusted_project_s_hooks_are_reported_once_and_never_run(tmp_path):
    script = Script(tmp_path)
    refused = HookCommand("UserPromptSubmit", script.command("block"), "project")
    config = HookConfig(refused=(refused,))
    agent = make_agent(tmp_path, ScriptedProvider(), [])
    agent.hooks[-1] = CommandHooks(lambda: config)
    watch(agent)
    turn(agent, "one")
    turn(agent, "two")

    assert script.payloads() == []
    [report] = runs(agent)
    assert (report.outcome, report.scope) == ("refused", "project")
    assert "not trusted" in report.notice
    assert len(agent.history.messages) == 4, "both turns ran as if the hook were absent"


# ------------------------------------------------------------ the real session


async def test_a_ui_session_runs_the_user_s_hooks_and_logs_them(tmp_path):
    script = Script(tmp_path)
    user = user_settings_path()
    user.parent.mkdir(parents=True, exist_ok=True)
    user.write_text(json.dumps({"hooks": {"Stop": [
        {"hooks": [{"type": "command", "command": script.command("record")}]}]}}),
        encoding="utf-8")
    config = Config()
    config.last_model = "test/model"
    env = Environment(cwd=str(tmp_path), platform=platform.system(), os_version="1",
                      shell_name="bash", session_date="2026-09-24", is_git_repo=False,
                      git_branch="")
    manager = ConversationManager(cwd=tmp_path, config=config, env=env,
                                  provider=ScriptedProvider())
    conv = manager.open()
    try:
        assert any(isinstance(h, CommandHooks) for h in conv.agent.hooks)
        assert conv.agent.ctx.extra["subagent"].hooks is conv.agent.hooks
        conv.submit("hello")
        assert await await_until(
            lambda: any(e["type"] == "hook_run" for e in conv.store.load_events()),
            timeout_s=30,
        )
    finally:
        await manager.close()

    [payload] = script.payloads()
    assert payload["hook_event_name"] == "Stop"
    assert payload["session_id"] == conv.conv_id
    assert payload["transcript_path"] == str(conv.store.path)
