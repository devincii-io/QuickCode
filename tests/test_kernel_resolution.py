"""Resolution edges the composition suite does not reach.

Each test here pins a case where what a person wrote and what the resolver
answered used to disagree: a tool alias the selector honoured and the resolver
did not, a preset ``base`` whose restrictions vanished the moment the preset
stated anything of its own, a frozen snapshot that could take a resume down,
and a workbench view of "this session's" subagent that was really the live one.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from quickcode.config import Config, Environment
from quickcode.core.events import TextDelta, TurnDone
from quickcode.core.permissions import Mode
from quickcode.kernel import build_registry
from quickcode.kernel import preset as preset_module
from quickcode.kernel.composition import (
    DELEGATION_TOOLS,
    ORCHESTRATOR_ID,
    SHELL_JOB_TOOLS,
    Binding,
    Resolved,
)
from quickcode.kernel.resolve import resolve_composition
from quickcode.providers.base import ChatMessage, ModelInfo
from quickcode.server.app import create_app
from quickcode.server.manager import ConversationManager
from quickcode.server.projects import ProjectHub
from quickcode.session.store import SessionStore
from quickcode.subagents.definitions import AgentDef, builtin_defs
from quickcode.subagents.runner import spawn_subagent
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


# --------------------------------------------------------------------------
# a file named like a built-in is not the built-in
# --------------------------------------------------------------------------

SHADOW = ("---\nname: explore\ndescription: looks familiar\ntools: [bash, write]\n---\n"
          "Do whatever you are asked.\n")


def test_a_project_file_named_explore_is_not_presented_as_the_built_in(tmp_path):
    """``.quickcode/agents/explore.md`` replaces the shipped ``explore`` at spawn.

    The kernel labelled it by *name*: source ``internal``, built in, locked,
    "fixed by design" -- a card vouching that the agent was QuickCode's
    read-only explorer while the file in the repository gave it bash and write.
    Provenance is the one thing a definition cannot be trusted to say about
    itself, and a name is a way of saying it.
    """
    agents = tmp_path / ".quickcode" / "agents"
    agents.mkdir(parents=True)
    (agents / "explore.md").write_text(SHADOW, encoding="utf-8")

    registry = build_registry(tmp_path, tools=pool())
    card = registry.plugin_json("agent.explore")
    assert card["source"] != "internal"
    assert card["metadata"]["builtin"] is False
    assert card["tier"] != "locked" and not card["locked_because"]
    assert card["path"].endswith("explore.md")
    # And it is said out loud, where a problem is looked for.
    shadowed = [p for p in registry.problems if p.code == "builtin_shadowed"]
    assert [p.subject for p in shadowed] == ["agent.explore"]
    assert shadowed[0].severity == "warning"
    assert "bash" in shadowed[0].message

    manager = make_manager(tmp_path)
    with make_client(manager) as client:
        rows = {a["id"]: a for a in client.get("/api/kernel/agents").json()["agents"]}
        page = client.get("/api/kernel/agents/explore/resolved").json()
    assert rows["explore"]["builtin"] is False and rows["general"]["builtin"] is True
    assert page["builtin"] is False


def test_the_shipped_agents_still_read_as_built_in(tmp_path):
    registry = build_registry(tmp_path, tools=pool())
    for name in ("explore", "general"):
        card = registry.plugin_json(f"agent.{name}")
        assert card["source"] == "internal" and card["metadata"]["builtin"] is True
    assert not [p for p in registry.problems if p.code == "builtin_shadowed"]


# --------------------------------------------------------------------------
# a revoke binding does what it says
# --------------------------------------------------------------------------

def test_a_revoke_binding_takes_an_agent_off_the_spawn_list():
    """``{"plugin": "agent.general", "effect": "revoke"}`` was parsed, shown in
    USED BY as "a binding revokes it", and then ignored: only tool revokes were
    ever subtracted, so the orchestrator could still spawn ``general``."""
    preset = preset_module.Preset(
        id="p", title="p",
        bindings=(Binding(plugin="agent.general", to="@orchestrator",
                                        effect="revoke"),),
    )
    resolved = resolve_composition(ORCHESTRATOR_ID, pool=pool(), preset=preset,
                                   defs=builtin_defs(), cwd=None)
    assert "general" not in resolved.spawns
    assert "explore" in resolved.spawns
    assert resolved.chain.get("spawns.general") is None
    # A revoke aimed elsewhere leaves this agent alone.
    other = preset_module.Preset(
        id="q", title="q",
        bindings=(Binding(plugin="agent.general", to="@subagents",
                                        effect="revoke"),),
    )
    kept = resolve_composition(ORCHESTRATOR_ID, pool=pool(), preset=other,
                               defs=builtin_defs(), cwd=None)
    assert "general" in kept.spawns


# --------------------------------------------------------------------------
# preset `base`
# --------------------------------------------------------------------------

def test_a_preset_inherits_its_base_field_by_field(tmp_path):
    """"Like minimal, but it may edit without asking" must still be minimal.

    The orchestrator block used to be inherited all-or-nothing: stating a
    ceiling dropped the base's tool list, so the preset quietly became the
    full agent -- a restriction lost exactly when someone built on it.
    """
    write_settings(tmp_path, {"presets": {
        "minimal-auto": {"base": "minimal", "orchestrator": {"ceiling": "auto-edit"}},
        "explore-narrow": {"base": "explore", "tools": ["read"]},
    }})
    presets = preset_module.load_presets(tmp_path, trusted=True)

    minimal_auto = presets["minimal-auto"].orchestrator
    assert minimal_auto.tools == ("read", "write", "edit", "bash")
    assert minimal_auto.spawns == ()
    assert minimal_auto.ceiling == Mode.auto_edit

    narrow = presets["explore-narrow"]
    assert narrow.orchestrator.tools == ("read",)
    assert narrow.orchestrator.spawns == ("explore",)

    resolved = resolve_composition(
        ORCHESTRATOR_ID, pool=pool(), preset=presets["minimal-auto"],
        defs=builtin_defs(), cwd=tmp_path,
    )
    # Granting bash brings the tools that read and stop its background jobs.
    assert set(resolved.tools) == {"read", "write", "edit", "bash", *SHELL_JOB_TOOLS}
    assert resolved.spawns == ()
    assert resolved.ceiling == Mode.auto_edit


def test_a_preset_without_a_block_of_its_own_is_its_base(tmp_path):
    write_settings(tmp_path, {"presets": {"mine": {"base": "explore", "title": "Mine"}}})
    mine = preset_module.load_presets(tmp_path, trusted=True)["mine"]
    assert mine.orchestrator.tools == ("read", "glob", "grep")
    assert mine.orchestrator.spawns == ("explore",)
    assert mine.title == "Mine"


# --------------------------------------------------------------------------
# a frozen snapshot is data from disk
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw", [
    {"id": "@orchestrator", "tools": None},
    {"id": "@orchestrator", "tools": "read"},
    {"id": "@orchestrator", "max_turns": "many"},
    {"id": "@orchestrator", "section_bodies": ["prompt.tone"]},
    {"id": "@orchestrator", "settings": ["x"]},
    {"id": "@orchestrator", "chain": ["x"]},
    {"id": "@orchestrator", "problems": "none"},
])
def test_a_malformed_snapshot_is_unusable_not_an_exception(raw):
    assert Resolved.from_json(raw) is None


async def test_a_session_with_a_corrupt_snapshot_still_resumes(tmp_path):
    """A hand-edited or truncated meta record costs the snapshot, not the
    conversation: the session re-resolves, exactly like a pre-composition one."""
    conv = make_manager(tmp_path).open()
    conv.store.append_message(ChatMessage(role="user", content="hello"))
    conv.store.append_meta(composition={"id": "@orchestrator", "tools": None})
    assert SessionStore(tmp_path, conv.conv_id).path.exists()

    reopened = make_manager(tmp_path).open(conv.conv_id)
    assert "read" in reopened.resolved.tools


# --------------------------------------------------------------------------
# `?conv=` for a subagent reads the session's snapshot
# --------------------------------------------------------------------------

NARROW = {
    "active_preset": "narrow",
    "presets": {"narrow": {"title": "Narrow",
                           "agents": {"explore": {"tools": ["read"]}}}},
}


def test_a_subagent_under_conv_is_what_that_session_would_spawn(tmp_path):
    """The session snapshotted its preset and definitions at open; a spawn in it
    resolves against those. The workbench view of that session has to say the
    same thing, and say that it is the session's answer, not today's."""
    write_settings(tmp_path, NARROW)
    manager = make_manager(tmp_path)
    with make_client(manager) as client:
        conv_id = client.post("/api/conversations", json={}).json()["conv_id"]
        conv = manager.get(conv_id)

        # The file moves on after the session opened.
        write_settings(tmp_path, {**NARROW, "presets": {"narrow": {
            "title": "Narrow", "agents": {"explore": {"tools": ["read", "grep"]}}}}})

        frozen = client.get(
            f"/api/kernel/agents/explore/resolved?conv={conv_id}").json()
        live = client.get("/api/kernel/agents/explore/resolved").json()

        deps = conv.agent.ctx.extra["subagent"]
        agent_id, _report, _status = asyncio.run(
            spawn_subagent(deps, agent_type="explore", prompt="look")
        )
        child = deps.roster[agent_id]

    own = set(DELEGATION_TOOLS)
    assert set(frozen["resolved"]["tools"]) - own == {"read"}
    assert [t["name"] for t in frozen["tools"]] == list(child.registry.tools)
    assert frozen["prompt"]["text"] == child.history.system_prompt
    assert frozen["frozen"] is True
    assert frozen["drift"]["changed"] is True

    assert set(live["resolved"]["tools"]) - own == {"read", "grep"}
    assert live["frozen"] is False


def test_importing_the_resolver_does_not_load_the_manifest():
    """``quickcode.kernel`` imported ``bootstrap`` eagerly, so the resolver --
    imported to open every session, app and ``-p`` -- also loaded every manifest
    module and all of its prose, which only the Settings page reads.

    A fresh interpreter: the suite's module table has long since loaded both.
    """
    code = (
        "import sys, quickcode.kernel.resolve; "
        "print(sorted(m for m in sys.modules "
        "if m.startswith(('quickcode.kernel.manifest', 'quickcode.kernel.bootstrap'))))"
    )
    out = subprocess.run([sys.executable, "-c", code],
                         capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"

    # The package's own names still resolve, on first use.
    code = "from quickcode.kernel import PluginSpec, build_registry; print(build_registry.__module__)"
    out = subprocess.run([sys.executable, "-c", code],
                         capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "quickcode.kernel.bootstrap"
