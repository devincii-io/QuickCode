"""Resolution edges the composition suite does not reach.

Each test here pins a case where what a person wrote and what the resolver
answered used to disagree: a tool alias the selector honoured and the resolver
did not, a preset ``base`` whose restrictions vanished the moment the preset
stated anything of its own, a frozen snapshot that could take a resume down,
and a workbench view of "this session's" subagent that was really the live one.
"""

from __future__ import annotations

import json
from pathlib import Path

from starlette.testclient import TestClient

from quickcode.config import Config, Environment
from quickcode.core.events import TextDelta, TurnDone
from quickcode.kernel import build_registry
from quickcode.kernel import preset as preset_module
from quickcode.kernel.resolve import resolve_composition
from quickcode.providers.base import ModelInfo
from quickcode.server.app import create_app
from quickcode.server.manager import ConversationManager
from quickcode.server.projects import ProjectHub
from quickcode.subagents.definitions import AgentDef, builtin_defs
from quickcode.tools.registry import default_registry


class ScriptedProvider:
    async def stream_chat(self, req):
        yield TextDelta("(done)")
        yield TurnDone("stop")

    async def list_models(self):
        return [ModelInfo(id="test/model", name="Test", context_length=100_000)]


def make_manager(cwd: Path) -> ConversationManager:
    cfg = Config()
    cfg.last_model = "test/model"
    env = Environment(
        cwd=str(cwd), platform="Windows", os_version="10", shell_name="bash",
        session_date="2026-08-17", is_git_repo=False, git_branch="",
    )
    return ConversationManager(cwd=cwd, config=cfg, env=env, provider=ScriptedProvider())


def make_client(manager: ConversationManager) -> TestClient:
    app = create_app(ProjectHub.from_manager(manager), host="127.0.0.1", port=8642,
                     token="")
    return TestClient(app, base_url="http://127.0.0.1:8642")


def write_settings(cwd: Path, body: dict) -> None:
    path = cwd / ".quickcode" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, indent=2), encoding="utf-8")


def pool():
    return list(default_registry().tools.values())


def task_tools() -> set[str]:
    return {t.name for t in pool() if t.name.startswith("task_")}


# --------------------------------------------------------------------------
# the `task` alias
# --------------------------------------------------------------------------

def test_the_task_alias_grants_the_task_board_the_way_select_does():
    """``tools: [read, task]`` is documented shorthand for ``task_*``.

    ``select()`` has always expanded it; the resolver matched it literally, so
    the agent lost every task tool and got a "matches no tool" warning for a
    word the selector itself defines.
    """
    defs = dict(builtin_defs())
    defs["planner"] = AgentDef("planner", "p", tools=["read", "task"])
    resolved = resolve_composition(
        "planner", pool=pool(), preset=preset_module.builtin_presets()["standard"],
        defs=defs, cwd=None,
    )
    assert task_tools() <= set(resolved.tools)
    assert "read" in resolved.tools and "write" not in resolved.tools
    assert not [p for p in resolved.problems if p.field == "tools"], resolved.problems
    # The chain keeps the word the author wrote, so the workbench can say
    # "matched by `task`" rather than a pattern nobody typed.
    some_task = sorted(task_tools())[0]
    assert resolved.chain[f"tools.{some_task}"][-1].rule == "task"


def test_used_by_follows_the_task_alias_too(tmp_path):
    agents = tmp_path / ".quickcode" / "agents"
    agents.mkdir(parents=True)
    (agents / "planner.md").write_text(
        "---\nname: planner\ndescription: plans\ntools: [read, task]\n---\nPlan.\n",
        encoding="utf-8",
    )
    registry = build_registry(tmp_path, tools=pool())
    some_task = sorted(task_tools())[0]
    uses = registry.used_by(f"tool.{some_task}")
    assert any(u.kind == "agent" and u.id == "planner" for u in uses), uses
