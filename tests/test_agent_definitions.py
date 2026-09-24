"""Agent definitions on disk: what ``.quickcode/agents/*.md`` may say, and
what happens to a file that says it wrong.

A definition's name becomes the agent id, and the id becomes a file name
under ``.quickcode/artifacts`` and an attribute in the ``<subagent id=...>``
tag the parent reads -- so a name is not free text. A file that cannot be
used is skipped with a warning naming it, never silently and never by
taking the session down.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from quickcode.config import Environment, Profile
from quickcode.core.events import TextDelta, TurnDone
from quickcode.core.permissions import READ_LIKE, Mode
from quickcode.kernel import preset as preset_module
from quickcode.kernel.composition import DELEGATION_TOOLS
from quickcode.kernel.resolve import resolve_composition
from quickcode.subagents.definitions import AgentDef, builtin_defs, load_defs
from quickcode.subagents.runner import SubagentDeps, spawn_subagent
from quickcode.tools.base import Tool
from quickcode.tools.registry import default_registry


def _write_def(cwd: Path, filename: str, text: str, *, encoding: str = "utf-8") -> None:
    d = cwd / ".quickcode" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / filename).write_text(text, encoding=encoding)


@pytest.mark.parametrize("name", [
    "../../outside", "a/b", "a\\b", 'x" status="done', "has space", "@orchestrator2", "",
])
def test_a_definition_whose_name_is_not_a_safe_id_is_refused(tmp_path, name, caplog):
    """The name becomes the agent id, and the id becomes a file name under
    ``.quickcode/artifacts`` and an attribute in the ``<subagent id=...>`` tag
    the parent reads. A project file is enough to set it."""
    _write_def(tmp_path, "evil.md", f"---\nname: {name}\ntools: [read]\n---\nBody.\n")
    defs = load_defs(tmp_path)
    assert name not in defs or name == ""
    if name:
        assert any("evil.md" in r.getMessage() for r in caplog.records)


def test_a_definition_saved_with_a_byte_order_mark_keeps_its_frontmatter(tmp_path):
    # Notepad writes one. The frontmatter used to be read as body text, which
    # dropped the tools allowlist the file asked for.
    _write_def(tmp_path, "reviewer.md",
               "---\nname: reviewer\ntools: [read, grep]\nmode_cap: ask\n---\nReview.\n",
               encoding="utf-8-sig")
    defs = load_defs(tmp_path)
    assert defs["reviewer"].tools == ["read", "grep"]
    assert defs["reviewer"].prompt_body == "Review."


def test_a_definition_that_cannot_be_read_is_reported_not_swallowed(tmp_path, caplog):
    _write_def(tmp_path, "broken.md", "")
    (tmp_path / ".quickcode" / "agents" / "broken.md").write_bytes(b"---\nname: \xff\xfe\n---\n")
    defs = load_defs(tmp_path)
    assert "explore" in defs
    assert any("broken.md" in r.getMessage() for r in caplog.records)


def test_a_yaml_block_list_of_tools_means_those_tools(tmp_path):
    # The frontmatter reader joins indented lines onto the key, so the most
    # natural way to write a list in YAML arrived as one pattern, '- read -
    # grep', that matched nothing: an agent with no tools at all.
    _write_def(tmp_path, "r.md",
               "---\nname: r\ntools:\n  - read\n  - grep\nmodels:\n  - a/x\n---\nBody.\n")
    defs = load_defs(tmp_path)
    assert defs["r"].tools == ["read", "grep"]
    assert defs["r"].models == ["a/x"]


# --------------------------------------------------------------------------
# tool globs and model policy, through a real spawn
# --------------------------------------------------------------------------


class _Provider:
    def __init__(self) -> None:
        self.models: list[str] = []

    async def stream_chat(self, req):
        self.models.append(req.model)
        yield TextDelta("ok")
        yield TurnDone("stop")

    async def list_models(self):
        return []


class _McpTool(Tool[BaseModel]):
    name = "mcp__docs__search"
    description = "an MCP tool"
    is_read_only = True
    permission = READ_LIKE
    Input = BaseModel


def _deps(tmp_path: Path, defs: dict, *, parent=None, depth: int = 0) -> SubagentDeps:
    deps = SubagentDeps(
        provider=_Provider(), profile=Profile(), env=Environment.detect(tmp_path),
        mode_getter=lambda: Mode.ask, cwd=tmp_path, depth=depth, defs=defs,
        pool=[_McpTool(), *default_registry().tools.values()], parent=parent,
    )
    return deps


async def test_tool_globs_grant_what_they_match_and_nothing_else(tmp_path):
    defs = {**builtin_defs(),
            "board": AgentDef("board", "b", tools=["read", "task_*", "mcp__*"])}
    deps = _deps(tmp_path, defs)
    await spawn_subagent(deps, agent_type="board", prompt="p")
    # The delegation pair is granted by depth, never by an allowlist.
    tools = set(deps.roster["board-1"].registry.tools) - set(DELEGATION_TOOLS)
    assert tools == {"read", "task_create", "task_update", "task_list", "task_get",
                     "mcp__docs__search"}


async def test_a_model_outside_the_definitions_allow_list_is_refused(tmp_path):
    defs = {**builtin_defs(),
            "pinned": AgentDef("pinned", "p", tools=["read"], models=["a/*"], model="a/x")}
    deps = _deps(tmp_path, defs)
    with pytest.raises(ValueError, match="may only run on"):
        await spawn_subagent(deps, agent_type="pinned", prompt="p", model_override="b/y")
    await spawn_subagent(deps, agent_type="pinned", prompt="p", model_override="a/y")
    assert deps.provider.models == ["a/y"]


async def test_a_definition_that_is_not_selectable_refuses_any_override(tmp_path):
    defs = {**builtin_defs(),
            "fixed": AgentDef("fixed", "f", tools=["read"], model="a/x",
                              model_selectable=False)}
    with pytest.raises(ValueError, match="does not accept a model override"):
        await spawn_subagent(_deps(tmp_path, defs), agent_type="fixed", prompt="p",
                             model_override="a/y")


async def test_allow_lists_that_share_no_model_refuse_rather_than_admit_all(tmp_path):
    """The child's allow-list is intersected with its spawner's. When the two
    shared nothing the intersection came out empty -- and an empty list means
    "no restriction", so both lists vanished and any override was taken."""
    defs = {**builtin_defs(),
            "lead": AgentDef("lead", "l", tools=None, spawns=["pinned"],
                             models=["a/*"], model="a/x"),
            "pinned": AgentDef("pinned", "p", tools=["read"], models=["b/*"], model="b/x")}
    lead = resolve_composition(
        "lead", pool=_deps(tmp_path, defs).pool,
        preset=preset_module.builtin_presets()["standard"], defs=defs, cwd=tmp_path,
    )
    deps = _deps(tmp_path, defs, parent=lead, depth=1)
    with pytest.raises(ValueError, match="no model"):
        await spawn_subagent(deps, agent_type="pinned", prompt="p", model_override="c/z")
    assert deps.spawned == []
