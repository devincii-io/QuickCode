"""What a child process QuickCode starts gets, and how it ends.

Every program QuickCode runs -- the agent's shell commands, background jobs,
authored command tools, hooks, MCP servers, the terminal panel, git and
ripgrep -- is started through ``quickcode/subproc.py``. These tests start real
processes and look at what they actually received: an environment without
QuickCode's own API keys (a model can ask for ``echo $QUICKCODE_*_API_KEY`` as
easily as for ``ls``), ``/dev/null`` for stdin unless they are fed one, a
process group of their own, and a kill that reaches what they started.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

from quickcode import subproc
from quickcode.hooks.runner import run_command
from quickcode.plugins import mcp
from quickcode.tools import bash as bash_mod
from quickcode.tools.base import ReadRegistry, ToolCtx
from quickcode.tools.bash import BashTool
from quickcode.tools.bash_jobs import BashJobs
from tests.conftest import await_until
from tests.test_command_runtime import run as run_command_tool
from tests.test_command_runtime import tool_for

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
LINUX_ONLY = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")

SECRETS = {
    "QUICKCODE_OPENROUTER_API_KEY": "sk-or-not-for-children-1",
    "QUICKCODE_ANTHROPIC_API_KEY": "sk-ant-not-for-children-2",
    "QUICKCODE_BRAVE_API_KEY": "brave-not-for-children-3",
    "QUICKCODE_TAVILY_API_KEY": "tvly-not-for-children-4",
}
# An ordinary variable, so a test can tell "the key was scrubbed" apart from
# "the child printed nothing at all".
PROBE = ("QC_CHILD_PROBE", "visible-to-children")
PRINT_ENV = [sys.executable, "-c",
             "import os\nfor k, v in os.environ.items(): print(k + '=' + v)"]
STUB = str(Path(__file__).with_name("mcp_stub.py"))


@pytest.fixture
def keys_in_env(monkeypatch):
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv(*PROBE)
    monkeypatch.setenv("QUICKCODE_SEARCH_PROVIDER", "brave")


def assert_scrubbed(text: str) -> None:
    assert f"{PROBE[0]}={PROBE[1]}" in text, f"the child's environment was not printed:\n{text}"
    leaked = [name for name, value in SECRETS.items() if value in text]
    assert not leaked, f"these reached the child: {leaked}"


def ctx_for(root: Path) -> ToolCtx:
    return ToolCtx(cwd=root, read_registry=ReadRegistry(), shell_name="bash",
                   platform=sys.platform, extra={})


@pytest.fixture(params=["pty", "pipes"])
def command_path(request, monkeypatch):
    """Both ways a foreground command runs: the pty (the POSIX default) and
    plain pipes (the Windows default, and the fallback everywhere)."""
    if request.param == "pipes":
        monkeypatch.setattr(bash_mod, "_use_pty", lambda ctx: False)
    return request.param


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return True


# --------------------------------------------------------------- environment


def test_child_env_drops_quickcodes_keys_and_keeps_the_rest(keys_in_env):
    env = subproc.child_env()
    assert not set(SECRETS) & set(env)
    assert env[PROBE[0]] == PROBE[1]
    assert env["QUICKCODE_SEARCH_PROVIDER"] == "brave"  # a choice, not a secret
    assert env.get("PATH") == os.environ.get("PATH")


def test_one_list_of_key_variables_is_scrubbed_redacted_and_reported(monkeypatch):
    """``child_env`` withholds, the session log redacts, and ``doctor`` names
    the same variables -- one list, so a key one of them learns about is never
    one another has forgotten."""
    from quickcode import doctor, secrets
    from quickcode.session import redact

    names = secrets.credential_env_names()
    assert {"QUICKCODE_OPENROUTER_API_KEY", "QUICKCODE_ANTHROPIC_API_KEY",
            "QUICKCODE_BRAVE_API_KEY", "QUICKCODE_EXA_API_KEY"} <= set(names)
    values = {name: f"value-for-{name.lower()}" for name in (*names, "QUICKCODE_SOMEDAY_TOKEN")}
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    assert not set(values) & set(subproc.child_env())
    assert set(values.values()) <= set(redact.known_secrets())
    report = doctor.check_credential_env().detail
    assert all(name in report for name in values)
    assert not any(value in report for value in values.values())


def test_what_a_caller_adds_goes_on_top_of_the_scrubbed_base(keys_in_env):
    env = subproc.child_env({"DECLARED": "yes", PROBE[0]: "overridden"})
    assert env["DECLARED"] == "yes"
    assert env[PROBE[0]] == "overridden"
    assert not set(SECRETS) & set(env)


async def test_the_bash_tool_cannot_print_quickcodes_keys(tmp_path, keys_in_env, command_path):
    tool = BashTool()
    result = await tool.run(
        tool.Input(command='env; echo "key=[$QUICKCODE_OPENROUTER_API_KEY]"'),
        ctx_for(tmp_path),
    )
    assert not result.is_error, result.content
    assert_scrubbed(result.content)
    assert "key=[]" in result.content


async def test_a_background_job_cannot_print_them_either(tmp_path, keys_in_env):
    jobs = BashJobs()
    job = await jobs.start(PRINT_ENV, cwd=str(tmp_path), command="env")
    assert await asyncio.to_thread(job.finished.wait, 30)
    out, _ = job.take()
    assert_scrubbed(out.decode())


async def test_a_command_tool_cannot_ask_for_them_by_name(tmp_path, monkeypatch, keys_in_env):
    """``env_from`` passes named variables through. It is written in a file a
    repository can commit, so it must not be a way to ask for QuickCode's own
    credentials."""
    import quickcode.config as config_module
    from quickcode.security import trust

    monkeypatch.setattr(config_module, "CONFIG_DIR", tmp_path / "home" / ".quickcode")
    monkeypatch.setattr(trust, "is_trusted", lambda cwd: True)
    project = tmp_path / "proj"
    (project / ".quickcode" / "plugins").mkdir(parents=True)
    names = ", ".join([*SECRETS, PROBE[0]])
    tool = tool_for(project, PRINT_ENV, env_from=f"[{names}]")

    result = await run_command_tool(tool, project)

    assert not result.is_error, result.content
    assert_scrubbed(result.content)


async def test_a_hook_cannot_print_them(tmp_path, keys_in_env):
    done = await run_command("env", payload={}, ctx=ctx_for(tmp_path), timeout_s=30)
    assert done.exit_code == 0, done.stderr
    assert_scrubbed(done.stdout)


async def test_an_mcp_server_gets_its_declared_env_on_a_scrubbed_base(keys_in_env):
    server = mcp.MCPServer("stub", sys.executable, [STUB],
                           {"STUB_ENV": "1", "DECLARED_BY_CONFIG": "kept"})
    await server.start()
    try:
        text, is_error = await asyncio.wait_for(server.call_tool("echo", {}), 20)
    finally:
        await server.stop()
    assert not is_error, text
    env = json.loads(text)["env"]
    assert env["DECLARED_BY_CONFIG"] == "kept"
    assert_scrubbed("\n".join(f"{k}={v}" for k, v in env.items()))


def test_git_and_ripgrep_run_without_them_too(keys_in_env):
    """The one-shot helper: git runs programs a repository's config names
    (fsmonitor, diff drivers), so it is a child like any other."""
    done = subproc.run(PRINT_ENV, capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    assert_scrubbed(done.stdout)


@POSIX_ONLY
async def test_a_frozen_app_hands_children_the_loader_path_it_was_started_with(
    tmp_path, monkeypatch, command_path,
):
    """PyInstaller points LD_LIBRARY_PATH at the bundle. A system program
    started from the agent's shell must not load the bundled libraries."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/QuickCode/_internal")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/usr/local/lib")
    monkeypatch.setenv("_PYI_APPLICATION_HOME_DIR", "/opt/QuickCode/_internal")
    tool = BashTool()
    result = await tool.run(
        tool.Input(command='echo "lib=[$LD_LIBRARY_PATH] pyi=[$_PYI_APPLICATION_HOME_DIR]"'),
        ctx_for(tmp_path),
    )
    assert "lib=[/usr/local/lib] pyi=[]" in result.content, result.content


# --------------------------------------------------------------------- stdin


@LINUX_ONLY
async def test_a_piped_command_reads_the_null_device_not_the_servers_stdin(
    tmp_path, monkeypatch,
):
    """The pipe path handed the command QuickCode's own stdin. From a console
    that is the console the server runs in: a command that reads stdin (`git
    commit` without -m, a prompt) sat on it until the timeout."""
    monkeypatch.setattr(bash_mod, "_use_pty", lambda ctx: False)
    read_end, write_end = os.pipe()
    saved = os.dup(0)
    os.dup2(read_end, 0)
    try:
        tool = BashTool()
        result = await tool.run(tool.Input(command="readlink /proc/self/fd/0",
                                           timeout_ms=10_000), ctx_for(tmp_path))
    finally:
        os.dup2(saved, 0)
        for fd in (saved, read_end, write_end):
            os.close(fd)
    assert result.content.strip() == "/dev/null", result.content


@LINUX_ONLY
async def test_spawn_async_gives_a_child_null_stdin_and_a_group_of_its_own(tmp_path):
    probe = ("import os; print(os.readlink('/proc/self/fd/0'), "
             "os.getsid(0) == os.getpid(), os.getpgrp() == os.getpid())")
    proc = await subproc.spawn_async([sys.executable, "-c", probe], cwd=str(tmp_path))
    out, err, timed_out = await subproc.communicate(proc, timeout=30)
    assert not timed_out
    assert out.decode().split() == ["/dev/null", "True", "True"], err


async def test_communicate_feeds_stdin_and_keeps_both_streams(tmp_path):
    echo = "import sys; data = sys.stdin.read(); print(data.upper()); print('e', file=sys.stderr)"
    proc = await subproc.spawn_async([sys.executable, "-c", echo], cwd=str(tmp_path),
                                     stdin=subproc.PIPE)
    out, err, timed_out = await subproc.communicate(proc, b"hello", timeout=30)
    assert (out.strip(), err.strip(), timed_out) == (b"HELLO", b"e", False)
    assert proc.returncode == 0


async def test_communicate_keeps_what_arrived_before_a_timeout(tmp_path):
    slow = "import sys, time; print('early', flush=True); time.sleep(60)"
    proc = await subproc.spawn_async([sys.executable, "-c", slow], cwd=str(tmp_path))
    started = time.monotonic()
    out, _, timed_out = await subproc.communicate(proc, timeout=1.5)
    assert timed_out
    assert out.strip() == b"early"
    assert time.monotonic() - started < 5


# ---------------------------------------------------------------- the tree


GRANDCHILD = "sleep 60 > /dev/null 2>&1 & echo $!"


@POSIX_ONLY
async def test_kill_tree_reaches_a_grandchild(tmp_path):
    proc = await subproc.spawn_async(["/bin/sh", "-c", f"{GRANDCHILD}; wait"],
                                     cwd=str(tmp_path))
    grandchild = int((await proc.stdout.readline()).decode())
    try:
        subproc.kill_tree(proc.pid)
        assert await await_until(lambda: not _alive(grandchild), timeout_s=5)
        await asyncio.wait_for(proc.wait(), 5)
    finally:
        if _alive(grandchild):
            os.kill(grandchild, 9)


@POSIX_ONLY
def test_kill_tree_still_reaches_it_once_the_shell_itself_is_gone(tmp_path):
    """``npm run dev &`` as a whole command: the shell exits at once and the
    server lives on in its group. Looking the group up from the shell's pid
    finds nothing once the shell has been reaped, so nothing was killed."""
    proc = subproc.spawn(["/bin/sh", "-c", GRANDCHILD], cwd=str(tmp_path),
                         stdout=subproc.PIPE, stderr=subproc.DEVNULL)
    grandchild = int(proc.stdout.readline().decode())
    proc.wait()
    proc.stdout.close()
    try:
        assert _alive(grandchild)
        subproc.kill_tree(proc.pid)
        assert wait_for_death(grandchild)
    finally:
        if _alive(grandchild):
            os.kill(grandchild, 9)


@POSIX_ONLY
async def test_a_command_tool_timeout_kills_what_outlived_its_program(tmp_path, monkeypatch):
    """The program exits and leaves a child holding its output pipe. The
    timeout's kill looked the group up from a pid already reaped, found
    nothing, and the child ran on after the tool had given up."""
    import quickcode.config as config_module
    from quickcode.security import trust

    monkeypatch.setattr(config_module, "CONFIG_DIR", tmp_path / "home" / ".quickcode")
    monkeypatch.setattr(trust, "is_trusted", lambda cwd: True)
    project = tmp_path / "proj"
    (project / ".quickcode" / "plugins").mkdir(parents=True)
    pidfile = tmp_path / "gc.pid"
    tool = tool_for(project, ["/bin/sh", "-c", f"sleep 60 & echo $! > {pidfile}"],
                    timeout_ms=1000)

    result = await run_command_tool(tool, project)

    grandchild = int(pidfile.read_text())
    try:
        assert result.is_error and "timed out" in result.content, result.content
        assert await await_until(lambda: not _alive(grandchild), timeout_s=5)
    finally:
        if _alive(grandchild):
            os.kill(grandchild, 9)


def wait_for_death(pid: int, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.02)
    return not _alive(pid)


def test_a_search_key_kept_in_config_json_is_redacted_from_the_log():
    """``search.providers.<name>.api_key`` is stored in plain text in
    config.json; a provider error that echoes it must not carry it into the
    session log."""
    from quickcode.config import CONFIG_PATH
    from quickcode.session import redact

    key = "tvly-" + "k" * 32
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps({"search": {"providers": {"tavily": {"api_key": key}}}}),
                           encoding="utf-8")
    assert key in redact.known_secrets()
    line = json.dumps({"type": "error", "message": f"401 for key {key}"})
    assert key not in redact.scrub_serialized(line, redact.known_secrets())
