"""What a child process QuickCode starts gets.

Every program QuickCode runs -- the agent's shell commands, background jobs,
authored command tools, hooks, MCP servers, the terminal panel, git and
ripgrep -- takes its environment from ``subproc.child_env``. These tests start
real processes and look at what they actually received: an environment without
QuickCode's own API keys, because a model can ask for ``echo
$QUICKCODE_*_API_KEY`` as easily as for ``ls``.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from quickcode import subproc
from quickcode.hooks.runner import run_command
from quickcode.plugins import mcp
from quickcode.tools import bash as bash_mod
from quickcode.tools.base import ReadRegistry, ToolCtx
from quickcode.tools.bash import BashTool
from quickcode.tools.bash_jobs import BashJobs
from tests.test_command_runtime import run as run_command_tool
from tests.test_command_runtime import tool_for

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")

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
