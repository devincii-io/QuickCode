"""Background shell jobs: ``bash(run_in_background=true)``, ``bash_output``, ``bash_kill``.

The claims worth pinning are the ones a regression would quietly break: the
call comes back before the command does, reads are incremental and say how the
job ended, a kill reaches the whole process tree, the cap refuses instead of
queueing, closing the conversation leaves nothing running, and a background
command meets exactly the permission gate a foreground one does. Every job here
is a real process.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from quickcode import cli
from quickcode.config import Environment, Profile
from quickcode.core.agent import PermissionOutcome
from quickcode.core.events import TextDelta, ToolCallEnd, TurnDone
from quickcode.core.loop import _run_tool
from quickcode.core.permissions import Decision, Mode, PermissionEngine, Rules
from quickcode.kernel import preset as preset_module
from quickcode.kernel.authoring.reserved import reserved_reason
from quickcode.kernel.composition import ORCHESTRATOR_ID, SHELL_JOB_TOOLS
from quickcode.kernel.resolve import resolve_composition
from quickcode.session.store import SessionStore
from quickcode.subagents.definitions import builtin_defs
from quickcode.subagents.runner import SubagentDeps, spawn_subagent
from quickcode.tools.base import ReadRegistry, ToolCtx
from quickcode.tools.bash import BashInput, BashTool
from quickcode.tools.bash_job_tools import (
    BashKillInput,
    BashKillTool,
    BashOutputInput,
    BashOutputTool,
)
from quickcode.tools.bash_jobs import (
    EXITED,
    KILLED,
    MAX_RUNNING,
    RUNNING,
    BashJobs,
    _utf8_boundary,
)
from quickcode.tools.registry import default_registry
from tests.conftest import await_until
from tests.test_headless import _install
from tests.test_server import FakeProvider, make_manager

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


@pytest.fixture
async def jobs():
    table = BashJobs()
    yield table
    await asyncio.to_thread(table.close)


def ctx_for(cwd: Path, table: BashJobs | None) -> ToolCtx:
    extra = {} if table is None else {"bash_jobs": table}
    return ToolCtx(cwd=cwd, read_registry=ReadRegistry(), platform=sys.platform, extra=extra)


async def start(ctx: ToolCtx, command: str, description: str = ""):
    return await BashTool().run(
        BashInput(command=command, description=description, run_in_background=True), ctx
    )


async def read(ctx: ToolCtx, bash_id: str | None = None, **kw) -> str:
    result = await BashOutputTool().run(BashOutputInput(bash_id=bash_id, **kw), ctx)
    assert not result.is_error, result.content
    return result.content


def gate_file(tmp_path: Path) -> tuple[Path, str]:
    """A file the test creates when it wants the command to carry on."""
    go = tmp_path / "go"
    return go, f'while [ ! -f "{go.as_posix()}" ]; do sleep 0.05; done'


# --------------------------------------------------------------------------
# start -> read -> exit
# --------------------------------------------------------------------------


async def test_a_background_command_returns_before_it_finishes(tmp_path, jobs):
    go, wait_for_go = gate_file(tmp_path)
    ctx = ctx_for(tmp_path, jobs)

    result = await start(ctx, f"{wait_for_go}; echo finished")

    assert not result.is_error, result.content
    assert "bash_1" in result.content and "bash_output" in result.content
    job = jobs.get("bash_1")
    assert job is not None and job.status == RUNNING
    go.touch()
    assert await await_until(lambda: not job.running)
    assert "finished" in await read(ctx, "bash_1")


async def test_each_read_returns_only_what_is_new(tmp_path, jobs):
    go, wait_for_go = gate_file(tmp_path)
    ctx = ctx_for(tmp_path, jobs)
    await start(ctx, f"echo first; {wait_for_go}; echo second")
    job = jobs.get("bash_1")
    assert await await_until(lambda: job.unread_bytes() > 0)

    first = await read(ctx, "bash_1")
    assert "first" in first and "still running" in first

    go.touch()
    second = await read(ctx, "bash_1", wait_s=30)
    assert "second" in second and "first" not in second
    assert "exited with code 0" in second

    assert "(no new output)" in await read(ctx, "bash_1")


async def test_the_exit_code_is_reported(tmp_path, jobs):
    ctx = ctx_for(tmp_path, jobs)
    await start(ctx, "echo about to fail; exit 7")

    out = await read(ctx, "bash_1", wait_s=30)

    assert "exited with code 7" in out
    assert jobs.get("bash_1").status == EXITED
    assert jobs.get("bash_1").exit_code == 7


async def test_a_finished_job_lets_go_of_its_pipe(tmp_path, jobs):
    """Its output is in the ring by then. The pipe stayed open until the job
    was garbage-collected: one descriptor per job, 32 kept per conversation."""
    ctx = ctx_for(tmp_path, jobs)
    await start(ctx, "echo done")
    job = jobs.get("bash_1")

    assert await asyncio.to_thread(job.finished.wait, 30)

    assert job._proc.stdout.closed
    assert "done" in await read(ctx, "bash_1")


async def test_a_filter_keeps_matching_lines_and_can_end_the_wait(tmp_path, jobs):
    go, wait_for_go = gate_file(tmp_path)
    ctx = ctx_for(tmp_path, jobs)
    await start(ctx, f"echo compiling; echo 'server ready on 8080'; {wait_for_go}")

    out = await read(ctx, "bash_1", filter=r"ready on \d+", wait_s=30)

    # Returned on the match, not on the job's end: it is still waiting on go.
    assert "still running" in out
    assert "server ready on 8080" in out and "compiling" not in out
    go.touch()


async def test_a_bad_filter_is_refused_without_consuming_output(tmp_path, jobs):
    ctx = ctx_for(tmp_path, jobs)
    await start(ctx, "echo keep me")
    job = jobs.get("bash_1")
    assert await await_until(lambda: not job.running)

    bad = await BashOutputTool().run(BashOutputInput(bash_id="bash_1", filter="("), ctx)

    assert bad.is_error and "invalid filter" in bad.content
    assert "keep me" in await read(ctx, "bash_1")


async def test_listing_every_job(tmp_path, jobs):
    ctx = ctx_for(tmp_path, jobs)
    assert "No background shell jobs" in await read(ctx)
    await start(ctx, "exit 0", description="quick one")
    assert await await_until(lambda: not jobs.get("bash_1").running)

    listing = await read(ctx)

    assert "bash_jobs[1]" in listing and "quick one" in listing


# --------------------------------------------------------------------------
# kill, cap, scope
# --------------------------------------------------------------------------


async def test_kill_ends_the_whole_process_tree(tmp_path, jobs):
    """The shell and what it started: a grandchild that outlives the kill
    would still write its marker."""
    go, wait_for_go = gate_file(tmp_path)
    marker = tmp_path / "survived"
    running = tmp_path / "running"
    ctx = ctx_for(tmp_path, jobs)
    await start(
        ctx,
        f'({wait_for_go}; echo late > "{marker.as_posix()}") & '
        f'echo up > "{running.as_posix()}"; sleep 60',
    )
    assert await await_until(running.exists)

    result = await BashKillTool().run(BashKillInput(bash_id="bash_1"), ctx)

    assert not result.is_error, result.content
    assert "Killed bash_1" in result.content
    assert jobs.get("bash_1").status == KILLED
    go.touch()
    await asyncio.sleep(1.0)
    assert not marker.exists(), "a child of the killed job kept running"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="once its shell has exited, an orphan has no process group on Windows "
           "for a kill to reach (that needs a Job Object)",
)
async def test_a_child_its_shell_left_behind_keeps_the_job_running_and_dies_with_it(
    tmp_path, jobs
):
    """`server &` is how a model most often writes a background command. The
    shell exits at once and the server stays attached to the output pipe:
    calling that job finished would hide a live server from bash_kill and
    from the close that is supposed to stop it."""
    go, wait_for_go = gate_file(tmp_path)
    marker = tmp_path / "survived"
    ctx = ctx_for(tmp_path, jobs)
    await start(ctx, f'({wait_for_go}; echo late > "{marker.as_posix()}") & echo shell done')
    job = jobs.get("bash_1")
    assert await await_until(lambda: job.exit_code is not None)
    await asyncio.sleep(0.1)

    out = await read(ctx, "bash_1")
    assert job.running
    assert "shell done" in out and "still running" in out

    result = await BashKillTool().run(BashKillInput(bash_id="bash_1"), ctx)
    assert "Killed bash_1" in result.content, result.content
    assert not job.running
    go.touch()
    await asyncio.sleep(1.0)
    assert not marker.exists(), "the child its shell left behind outlived the kill"


async def test_killing_a_finished_job_says_so(tmp_path, jobs):
    ctx = ctx_for(tmp_path, jobs)
    await start(ctx, "exit 0")
    assert await await_until(lambda: not jobs.get("bash_1").running)

    result = await BashKillTool().run(BashKillInput(bash_id="bash_1"), ctx)

    assert not result.is_error
    assert "nothing to stop" in result.content


async def test_the_cap_refuses_rather_than_queues(tmp_path):
    table = BashJobs(max_running=2)
    ctx = ctx_for(tmp_path, table)
    try:
        for _ in range(2):
            assert not (await start(ctx, "sleep 60")).is_error

        refused = await start(ctx, "sleep 60")

        assert refused.is_error
        assert "bash_1, bash_2" in refused.content and "bash_kill" in refused.content
        assert len(table.running()) == 2

        await BashKillTool().run(BashKillInput(bash_id="bash_1"), ctx)
        assert not (await start(ctx, "sleep 60")).is_error
    finally:
        await asyncio.to_thread(table.close)


def test_the_default_cap_is_eight():
    assert MAX_RUNNING == 8
    assert BashJobs().max_running == MAX_RUNNING


async def test_a_job_from_another_conversation_cannot_be_named(tmp_path, jobs):
    """The id is a key into this conversation's own table, never a pid."""
    ours = ctx_for(tmp_path, jobs)
    other_table = BashJobs()
    theirs = ctx_for(tmp_path, other_table)
    try:
        await start(theirs, "sleep 60")

        result = await BashKillTool().run(BashKillInput(bash_id="bash_1"), ours)

        assert result.is_error and "unknown background job" in result.content
        assert other_table.get("bash_1").running
    finally:
        await asyncio.to_thread(other_table.close)


async def test_without_a_job_table_background_is_refused_plainly(tmp_path):
    ctx = ctx_for(tmp_path, None)
    started = await start(ctx, "echo hi")
    assert started.is_error and "not available" in started.content
    for tool, inp in ((BashOutputTool(), BashOutputInput(bash_id="bash_1")),
                      (BashKillTool(), BashKillInput(bash_id="bash_1"))):
        result = await tool.run(inp, ctx)
        assert result.is_error and "not available" in result.content


# --------------------------------------------------------------------------
# bounded output and encoding
# --------------------------------------------------------------------------


async def test_the_ring_is_bounded_and_says_what_it_dropped(tmp_path):
    table = BashJobs(buffer_bytes=4096)
    ctx = ctx_for(tmp_path, table)
    try:
        await start(ctx, "for i in $(seq 1 2000); do echo line-$i; done")
        assert await await_until(lambda: not table.get("bash_1").running)

        out = await read(ctx, "bash_1")

        assert "bytes were dropped" in out
        assert "line-2000" in out and "line-1\n" not in out
        assert len(out.encode()) < 4096 + 512
    finally:
        await asyncio.to_thread(table.close)


async def test_a_stray_non_utf8_byte_does_not_break_a_read(tmp_path, jobs):
    ctx = ctx_for(tmp_path, jobs)
    await start(ctx, r"printf 'before \377\376 after\n'")
    assert await await_until(lambda: not jobs.get("bash_1").running)

    out = await read(ctx, "bash_1")

    assert "before" in out and "after" in out
    json.dumps({"content": out}, ensure_ascii=False).encode("utf-8")


async def test_a_character_split_across_two_reads_arrives_whole(tmp_path, jobs):
    """Half of `é` must wait for its other half rather than turning the whole
    first read into the system code page's idea of it."""
    go, wait_for_go = gate_file(tmp_path)
    ctx = ctx_for(tmp_path, jobs)
    await start(ctx, rf"printf 'caf\303'; {wait_for_go}; printf '\251\n'")
    job = jobs.get("bash_1")
    assert await await_until(lambda: job.unread_bytes() >= 4)

    first = await read(ctx, "bash_1")
    go.touch()
    second = await read(ctx, "bash_1", wait_s=30)

    assert "caf" in first and "�" not in first
    assert "é" in second


@pytest.mark.parametrize(("data", "keep"), [
    (b"abc", 3),
    (b"caf\xc3", 3),
    (b"caf\xc3\xa9", 5),
    (b"\xe2\x82", 0),
    (b"\xe2\x82\xac", 3),
    (b"x\xf0\x9f\x98", 1),
    (b"", 0),
])
def test_utf8_boundary(data, keep):
    assert _utf8_boundary(data) == keep


# --------------------------------------------------------------------------
# permissions: the same gate, word for word
# --------------------------------------------------------------------------


def _agent(tmp_path: Path, engine: PermissionEngine, table: BashJobs):
    requests: list = []

    async def deny(req):
        requests.append(req)
        return PermissionOutcome(allow=False, deny_message="the test says no")

    agent = SimpleNamespace(
        name="main", hooks=[], registry=default_registry(), permissions=engine,
        permission_cb=deny, ctx=ctx_for(tmp_path, table),
    )
    return agent, requests


async def _gate(agent, requests, args: dict) -> tuple:
    before = len(requests)
    call = SimpleNamespace(id="c1", name="bash", arguments=json.dumps(args))
    content, _is_error, _meta = await _run_tool(agent, call)
    if len(requests) > before:
        req = requests[-1]
        assert args["command"].splitlines()[0] in req.preview
        return ("ask", req.tool, req.arg, req.rule_suggestion)
    if content.startswith("Blocked by permission rules"):
        return ("deny",)
    return ("allow",)


@pytest.mark.parametrize(("mode", "rules", "command", "expected"), [
    (Mode.ask, {}, "npm install left-pad", "ask"),
    (Mode.ask, {}, "ls", "allow"),
    (Mode.ask, {}, "cat .env", "ask"),  # protected path
    (Mode.ask, {}, "echo ok && npm publish", "ask"),  # decomposed per subcommand
    (Mode.ask, {"allow": ["bash(true)"]}, "true", "allow"),
    (Mode.ask, {"deny": ["bash(curl **)"]}, "curl http://example.invalid/x", "deny"),
    (Mode.plan, {}, "touch planned.txt", "deny"),
    (Mode.dontask, {}, "npm install left-pad", "deny"),
    (Mode.yolo, {}, "git push --force origin main", "ask"),  # circuit breaker
])
async def test_a_background_command_meets_the_foreground_gate(
    tmp_path, mode, rules, command, expected
):
    table = BashJobs()
    engine = PermissionEngine(mode=mode, rules=Rules(**rules), root=tmp_path,
                              yolo_accepted=True)
    agent, requests = _agent(tmp_path, engine, table)
    try:
        foreground = await _gate(agent, requests, {"command": command})
        background = await _gate(agent, requests,
                                 {"command": command, "run_in_background": True})
    finally:
        await asyncio.to_thread(table.close)

    assert foreground[0] == expected
    assert background == foreground
    if foreground[0] == "ask":
        assert "background" in requests[-1].preview
        assert "background" not in requests[-2].preview


def test_the_job_tools_never_prompt_and_only_bash_output_batches():
    engine = PermissionEngine(mode=Mode.plan, rules=Rules(), root=Path.cwd())
    output, kill = BashOutputTool(), BashKillTool()
    assert output.is_read_only is True and kill.is_read_only is False
    for tool, args in ((output, {"bash_id": "bash_1"}), (kill, {"bash_id": "bash_1"})):
        assert tool.permission.mutates is False
        assert tool.permission.target_field == "bash_id"
        assert not tool.permission.shell and not tool.permission.path_target
        assert engine.evaluate_tool(tool, args)[0] is Decision.allow


# --------------------------------------------------------------------------
# where the tools reach
# --------------------------------------------------------------------------


def test_the_job_tools_ship_and_their_names_are_reserved():
    registry = default_registry()
    for name in SHELL_JOB_TOOLS:
        assert name in registry.tools
        assert reserved_reason(f"tool.custom_{name}", "tool", name)


def test_a_composition_granting_bash_also_grants_its_job_tools():
    pool = list(default_registry().tools.values())
    presets = preset_module.builtin_presets()
    minimal = resolve_composition(ORCHESTRATOR_ID, pool=pool, preset=presets["minimal"],
                                  defs=builtin_defs(), cwd=None)
    explore = resolve_composition(ORCHESTRATOR_ID, pool=pool, preset=presets["explore"],
                                  defs=builtin_defs(), cwd=None)

    assert {"bash", *SHELL_JOB_TOOLS} <= set(minimal.tools)
    assert "bash" not in explore.tools
    assert not set(SHELL_JOB_TOOLS) & set(explore.tools)


class _Quiet:
    async def stream_chat(self, _req):
        yield TextDelta("done")
        yield TurnDone("stop")

    async def list_models(self):
        return []


async def test_a_subagent_shares_the_conversation_s_job_table(tmp_path):
    table = BashJobs()
    deps = SubagentDeps(
        provider=_Quiet(), profile=Profile(), env=Environment.detect(tmp_path),
        mode_getter=lambda: Mode.ask, cwd=tmp_path, depth=0, bash_jobs=table,
    )
    agent_id, _report, _ = await spawn_subagent(deps, agent_type="general", prompt="p")

    child = deps.roster[agent_id]
    assert child.ctx.extra["bash_jobs"] is table
    assert {"bash", *SHELL_JOB_TOOLS} <= set(child.registry.tools)
    engine = PermissionEngine(Mode.ask, Rules(), tmp_path)
    assert deps.child(1, engine, self_id=agent_id).bash_jobs is table


# --------------------------------------------------------------------------
# the conversation: log, reminders, cleanup
# --------------------------------------------------------------------------


async def test_start_and_exit_land_in_the_session_log_in_order(tmp_path):
    manager = make_manager(tmp_path, FakeProvider([]))
    conv = manager.open()
    try:
        ctx = conv.agent.ctx
        await start(ctx, "echo hello; exit 3", description="say hello")
        job = ctx.extra["bash_jobs"].get("bash_1")
        assert await await_until(lambda: not job.running)
        await asyncio.sleep(0.05)

        events = conv.store.load_events()
        kinds = [e["type"] for e in events if e["type"].startswith("bash_job")]
        assert kinds == ["bash_job_started", "bash_job_done"]
        started = next(e for e in events if e["type"] == "bash_job_started")
        done = next(e for e in events if e["type"] == "bash_job_done")
        assert started["job_id"] == done["job_id"] == "bash_1"
        assert started["description"] == "say hello"
        assert (done["status"], done["exit_code"]) == (EXITED, 3)
        assert any(e["type"] == "system_note" and "bash_1 exited with code 3" in e["text"]
                   for e in events)
    finally:
        await manager.close()


async def test_an_exit_the_model_did_not_see_is_waiting_for_it_next_turn(tmp_path):
    provider = FakeProvider([])
    manager = make_manager(tmp_path, provider)
    conv = manager.open()
    try:
        ctx = conv.agent.ctx
        await start(ctx, "echo unread; exit 1")
        await start(ctx, "exit 0")
        table = ctx.extra["bash_jobs"]
        assert await await_until(lambda: not table.running())
        # The second job's ending has been read, so only the first is news.
        await read(ctx, "bash_2")

        conv.submit("what happened?")
        assert await await_until(lambda: bool(provider.requests) and not conv.agent.busy)

        sent = "\n".join(str(m.content) for m in provider.requests[0].messages
                         if m.role == "user")
        assert "bash_1" in sent and "code 1" in sent and "bash_output" in sent
        assert "bash_2" not in sent
        # Said once, not at the top of every turn from now on.
        assert table.exit_notices() == []
    finally:
        await manager.close()


def test_a_headless_run_leaves_no_job_running(tmp_path, monkeypatch, capsys):
    """A `-p` process ends with its one turn, so its jobs end with it -- and
    both brackets still reach the log."""
    script = [
        [ToolCallEnd(id="c1", name="bash", arguments=json.dumps(
            {"command": "sleep 60", "run_in_background": True})),
         TurnDone("tool_calls")],
        [TextDelta("started it"), TurnDone("stop")],
    ]
    built: dict = {}
    _install(monkeypatch, FakeProvider(script), built)

    cli.main(["--print", "--cwd", str(tmp_path), "--mode", "yolo", "--yolo", "serve it"])

    assert capsys.readouterr().out.strip() == "started it"
    job = built["agent"].ctx.extra["bash_jobs"].get("bash_1")
    assert job is not None and job.status == KILLED
    kinds = [e["type"] for e in SessionStore(tmp_path, built["store"].conv_id).load_events()
             if e["type"].startswith("bash_job")]
    assert kinds == ["bash_job_started", "bash_job_done"]


async def test_closing_a_conversation_leaves_no_job_running(tmp_path):
    manager = make_manager(tmp_path, FakeProvider([]))
    conv = manager.open()
    ctx = conv.agent.ctx
    running = tmp_path / "running"
    await start(ctx, f'echo up > "{running.as_posix()}"; sleep 60')
    table = ctx.extra["bash_jobs"]
    job = table.get("bash_1")
    assert await await_until(running.exists)

    await manager.close()

    assert job.status == KILLED
    assert not table.running()
    refused = await start(ctx, "echo too late")
    assert refused.is_error and "closing" in refused.content
