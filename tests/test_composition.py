"""Composition, resolution and the narrowing invariant.

These are the cases that would actually catch a regression, not a sweep of the
resolver's surface: intersection cannot depend on layer order, narrowing has to
compound with depth, the depth-0 carve-out has to work in both directions,
provenance has to name the layer that really set a value, resolution has to
survive garbage, legacy presets have to keep resolving, and the ``enabled``
toggle has to actually remove a tool.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from quickcode.config import Config, Environment
from quickcode.core.permissions import Mode
from quickcode.kernel import preset as preset_module
from quickcode.kernel.composition import (
    DELEGATION_TOOLS,
    ORCHESTRATOR_ID,
    Composition,
    Resolved,
)
from quickcode.kernel.resolve import resolve_composition, session_pool
from quickcode.providers.base import ModelInfo
from quickcode.server.manager import ConversationManager
from quickcode.subagents.definitions import AgentDef, builtin_defs
from quickcode.tools.registry import default_registry


@pytest.fixture(autouse=True)
def isolate_user_settings(tmp_path, monkeypatch):
    """The user's real ``~/.quickcode/settings.json`` is not a test fixture.

    Both modules bind the path by name, so both have to be redirected or the
    layer under test is whatever happens to be on the developer's machine.
    """
    fake = tmp_path / "user-settings.json"
    monkeypatch.setattr("quickcode.kernel.state.user_settings_path", lambda: fake)
    monkeypatch.setattr("quickcode.kernel.preset.user_settings_path", lambda: fake)
    return fake


def pool():
    return list(default_registry().tools.values())


def names(resolved: Resolved) -> set[str]:
    return set(resolved.tools)


def write_settings(cwd: Path, body: dict) -> None:
    path = cwd / ".quickcode" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------
# intersection
# --------------------------------------------------------------------------

def test_intersection_is_independent_of_layer_order():
    """Two narrowing layers must give the same answer whichever holds which.

    This is the property that deletes "which layer wins" as a question for
    capability fields; if it ever fails, precedence bugs come back.
    """
    defs = dict(builtin_defs())
    wide = ("read", "glob", "grep", "write")
    narrow = ("read", "write", "edit")

    def resolve_with(preset_tools, agent_tools):
        defs["probe"] = AgentDef("probe", "p", tools=list(agent_tools))
        preset = preset_module.Preset(
            id="p", title="p",
            agents={"probe": Composition().with_fields(tools=tuple(preset_tools))},
        )
        return names(resolve_composition("probe", pool=pool(), preset=preset,
                                         defs=defs, cwd=None))

    one, other = resolve_with(wide, narrow), resolve_with(narrow, wide)
    assert one == other
    # The delegation set rides along by depth, never by allowlist.
    assert one - set(DELEGATION_TOOLS) == {"read", "write"}


# --------------------------------------------------------------------------
# narrowing
# --------------------------------------------------------------------------

def test_narrowing_compounds_at_depth_two():
    """explore (read/glob/grep) spawning general (tools: null) must not widen.

    The grandchild inherits what its parent was granted, never the session's
    pool -- otherwise delegation is an escalation path.
    """
    defs = builtin_defs()
    preset = preset_module.builtin_presets()["standard"]
    orch = resolve_composition(ORCHESTRATOR_ID, pool=pool(), preset=preset,
                               defs=defs, cwd=None)
    explore = resolve_composition("explore", pool=pool(), preset=preset, defs=defs,
                                  cwd=None, parent=orch, depth=0)
    child = resolve_composition("general", pool=pool(), preset=preset, defs=defs,
                                cwd=None, parent=explore, depth=1)

    assert names(child) == {"read", "glob", "grep"}
    assert "write" not in child.tools and "bash" not in child.tools
    # And the ceiling narrows with it: general asks for auto-edit under an
    # explore parent capped at ask.
    assert child.ceiling == Mode.ask


def test_a_literal_tool_the_parent_withholds_is_an_error_not_a_silent_drop():
    defs = dict(builtin_defs())
    defs["writer"] = AgentDef("writer", "w", tools=["read", "write"])
    preset = preset_module.builtin_presets()["standard"]
    orch = resolve_composition(ORCHESTRATOR_ID, pool=pool(), preset=preset,
                               defs=defs, cwd=None)
    explore = resolve_composition("explore", pool=pool(), preset=preset, defs=defs,
                                  cwd=None, parent=orch, depth=0)
    child = resolve_composition("writer", pool=pool(), preset=preset, defs=defs,
                                cwd=None, parent=explore, depth=1)

    codes = [p.code for p in child.errors()]
    assert "tool_withheld_by_parent" in codes
    assert names(child) == {"read"}


# --------------------------------------------------------------------------
# the depth-0 carve-out, both halves
# --------------------------------------------------------------------------

def _delegator() -> preset_module.Preset:
    return preset_module.Preset(
        id="delegator", title="Delegator",
        orchestrator=Composition().with_fields(
            tools=("read", "glob", "grep"), spawns=("explore", "general"),
        ),
        agents={"general": Composition().with_fields(
            tools=("read", "write", "edit", "bash"))},
    )


def test_restricting_the_orchestrator_does_not_restrict_the_session():
    """The carve-out. An orchestrator holding no write still admits a child
    that holds one, because the parent set at depth 0 is the session pool and
    not the orchestrator's grant."""
    defs = builtin_defs()
    preset = _delegator()
    orch = resolve_composition(ORCHESTRATOR_ID, pool=pool(), preset=preset,
                               defs=defs, cwd=None)
    assert "write" not in orch.tools

    child = resolve_composition("general", pool=pool(), preset=preset, defs=defs,
                                cwd=None, parent=orch, depth=0)
    assert {"read", "write", "edit", "bash"} <= names(child)
    assert child.errors() == ()


def test_disabling_a_plugin_does_restrict_the_session(tmp_path):
    """The other half. To restrict the session you disable the plugin, and
    then no agent at any depth can be granted it."""
    write_settings(tmp_path, {"plugins": {"tool.write": {"enabled": False}}})
    restricted = session_pool(tmp_path, pool())
    assert "write" not in {t.name for t in restricted}

    defs = builtin_defs()
    preset = _delegator()
    orch = resolve_composition(ORCHESTRATOR_ID, pool=restricted, preset=preset,
                               defs=defs, cwd=tmp_path)
    child = resolve_composition("general", pool=restricted, preset=preset, defs=defs,
                                cwd=tmp_path, parent=orch, depth=0)
    assert "write" not in child.tools
    assert {"read", "edit", "bash"} <= names(child)


# --------------------------------------------------------------------------
# provenance
# --------------------------------------------------------------------------

def test_provenance_names_the_layer_that_set_the_value(tmp_path):
    write_settings(tmp_path, {
        "plugins": {"prompt.tone": {"settings": {"body": "Be terse."}}},
    })
    defs = builtin_defs()
    preset = _delegator()
    orch = resolve_composition(ORCHESTRATOR_ID, pool=pool(), preset=preset,
                               defs=defs, cwd=tmp_path)

    # Every granted tool is explained, and one the preset chose says so.
    assert all(orch.chain.get(f"tools.{name}") for name in orch.tools)
    assert orch.chain["tools.read"][-1].layer == "preset"
    assert orch.chain["tools.read"][-1].rule == "read"
    # A section body written into the project settings file says "project".
    assert orch.section_bodies["prompt.tone"] == "Be terse."
    assert orch.chain["section_bodies.prompt.tone"][-1].layer == "project"


# --------------------------------------------------------------------------
# totality and legacy
# --------------------------------------------------------------------------

def test_resolution_is_total_over_garbage():
    """A session must always open, so nothing here may raise."""
    broken = preset_module.Preset.from_dict("broken", {
        "orchestrator": "not a dict",
        "tools": "read",                # a string where a list belongs
        "agents": 17,
        "bindings": ["nope", {"plugin": "tool.bash"}],
        "settings": "no",
    })
    resolved = resolve_composition("general", pool=pool(), preset=broken,
                                   defs=builtin_defs(), cwd=None)
    assert isinstance(resolved, Resolved)

    missing = resolve_composition("does-not-exist", pool=pool(), preset=broken,
                                  defs=builtin_defs(), cwd=None)
    assert [p.code for p in missing.errors()] == ["unknown_agent"]


def test_a_legacy_preset_body_still_resolves(tmp_path):
    """The flat shape on disk keeps meaning what it meant: tools lift onto the
    orchestrator, the agents *list* lifts to its spawns."""
    write_settings(tmp_path, {
        "active_preset": "old",
        "presets": {"old": {
            "title": "Old",
            "tools": ["read", "glob", "grep"],
            "agents": ["explore"],
            "prompt_overrides": {"prompt.tone": "Terse."},
            "default_mode": "plan",
        }},
    })
    preset = preset_module.resolve(tmp_path)
    assert preset.id == "old"
    assert preset.default_mode == "plan"

    orch = resolve_composition(ORCHESTRATOR_ID, pool=pool(), preset=preset,
                               defs=builtin_defs(), cwd=tmp_path)
    # The same tools ``select_tools`` picks, plus the delegation set, which is
    # granted by depth rather than by allowlist.
    selected = {t.name for t in preset_module.select_tools(preset, pool())}
    assert selected == {"read", "glob", "grep"}
    assert names(orch) == selected | set(DELEGATION_TOOLS)
    assert orch.spawns == ("explore",)
    assert orch.section_bodies["prompt.tone"] == "Terse."


# --------------------------------------------------------------------------
# spawning
# --------------------------------------------------------------------------

async def test_spawn_refuses_before_minting_an_agent_id():
    """Resolution is total; spawning is fallible -- and the refusal has to land
    before an id is taken out of the counter."""
    from quickcode.config import Profile
    from quickcode.core.events import TextDelta, TurnDone
    from quickcode.subagents.runner import SubagentDeps, spawn_subagent

    class Provider:
        async def stream_chat(self, req):
            yield TextDelta("x")
            yield TurnDone("stop")

        async def list_models(self):
            return []

    defs = dict(builtin_defs())
    defs["writer"] = AgentDef("writer", "w", tools=["read", "write"])
    preset = preset_module.builtin_presets()["standard"]
    orch = resolve_composition(ORCHESTRATOR_ID, pool=pool(), preset=preset,
                               defs=defs, cwd=None)
    explore = resolve_composition("explore", pool=pool(), preset=preset, defs=defs,
                                  cwd=None, parent=orch, depth=0)
    deps = SubagentDeps(
        provider=Provider(), profile=Profile(),
        env=Environment.detect(Path.cwd()), mode_getter=lambda: Mode.ask,
        cwd=Path.cwd(), depth=1, pool=pool(), parent=explore, defs=defs,
        preset=preset,
    )
    with pytest.raises(ValueError, match="tool 'write'"):
        await spawn_subagent(deps, agent_type="writer", prompt="p")
    assert deps.spawned == []


# --------------------------------------------------------------------------
# the session, end to end
# --------------------------------------------------------------------------

class _Provider:
    async def stream_chat(self, req):  # pragma: no cover - never driven here
        raise AssertionError("no turn is taken in these tests")

    async def list_models(self):
        return [ModelInfo(id="test/model", name="Test", context_length=100_000)]


def _manager(tmp_path: Path) -> ConversationManager:
    cfg = Config()
    cfg.last_model = "test/model"
    env = Environment(
        cwd=str(tmp_path), platform="Windows", os_version="10", shell_name="bash",
        session_date="2026-08-17", is_git_repo=False, git_branch="",
    )
    return ConversationManager(cwd=tmp_path, config=cfg, env=env, provider=_Provider())


async def test_a_delegation_only_orchestrator_still_hands_its_children_the_pool(tmp_path):
    """The defect this phase exists to fix, at the wiring level.

    ``manager.open()`` used to pass the preset-filtered registry down as the
    subagents' pool, so an orchestrator restricted to delegation handed every
    child an empty toolset.
    """
    write_settings(tmp_path, {
        "active_preset": "delegator",
        "presets": {"delegator": {
            "title": "Delegator",
            "orchestrator": {"tools": ["read", "glob", "grep"],
                             "spawns": ["explore", "general"]},
        }},
    })
    conv = _manager(tmp_path).open()

    assert "write" not in conv.agent.registry.tools
    assert "agent" in conv.agent.registry.tools

    deps = conv.agent.ctx.extra["subagent"]
    assert {"write", "edit", "bash"} <= {t.name for t in deps.pool}

    child = resolve_composition("general", pool=deps.pool, preset=deps.preset,
                                defs=deps.defs, cwd=tmp_path,
                                parent=deps.parent, depth=0)
    assert {"read", "write", "edit", "bash"} <= names(child)


async def test_the_composition_is_frozen_for_the_life_of_a_session(tmp_path):
    """Editing the preset must not change the tools under a conversation that
    has already been told what it has."""
    write_settings(tmp_path, {
        "active_preset": "p",
        "presets": {"p": {"title": "P", "tools": ["read", "glob", "grep"]}},
    })
    conv = _manager(tmp_path).open()
    conv_id = conv.conv_id
    before = set(conv.agent.registry.tools)

    write_settings(tmp_path, {
        "active_preset": "p",
        "presets": {"p": {"title": "P", "tools": ["read", "write", "edit", "bash"]}},
    })
    resumed = _manager(tmp_path).open(conv_id)
    assert set(resumed.agent.registry.tools) == before
    assert resumed.resolved.digest() == conv.resolved.digest()
