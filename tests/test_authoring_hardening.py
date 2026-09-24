"""Authored files the validator passed but the runtime could not use.

The validator's contract (schema.py) is that a file it accepts loads, and a
file it rejects says why. These are the cases where that broke: the tool was
dropped with a log line nobody reads, or loaded and then crashed on its first
call.
"""

from __future__ import annotations

import json

import pytest

from quickcode.kernel.authoring import discovery, schema
from tests.test_authoring import ECHO, echo_tool, run_tool, write


@pytest.fixture
def project(tmp_path, monkeypatch):
    import quickcode.config as config_module
    from quickcode.security import trust

    home = tmp_path / "home" / ".quickcode"
    (home / "plugins").mkdir(parents=True)
    root = tmp_path / "proj"
    (root / ".quickcode" / "plugins").mkdir(parents=True)
    monkeypatch.setattr(config_module, "CONFIG_DIR", home)
    monkeypatch.setattr(trust, "is_trusted", lambda cwd: True)
    return root


@pytest.mark.parametrize("name", ["model_config", "model_dump", "schema", "json", "copy"])
def test_a_parameter_named_after_the_input_model_is_refused_by_the_validator(project, name):
    write(project, "echo-args", echo_tool(
        [*ECHO, f"{{{name}}}"], [{"name": name, "type": "string", "default": "x"}]))
    found = discovery.discover(project)
    assert found.plugins == []
    problem = next(p for p in found.problems if p.code == schema.BAD_SLUG)
    assert name in problem.message
    assert problem.fix


@pytest.mark.parametrize("pattern", ["[", r"^(?!-)\w+$"])
def test_a_pattern_the_input_model_cannot_enforce_is_reported_not_dropped(project, pattern):
    """Invalid, or valid for ``re`` but not for pydantic's linear-time engine
    (look-around): either way the model would not build, and the tool used to
    vanish with only a log line."""
    write(project, "echo-args", echo_tool(
        [*ECHO, "{x}"], [{"name": "x", "type": "string", "pattern": pattern}]))
    found = discovery.discover(project)
    assert found.plugins == []
    problem = next(p for p in found.problems if p.code == schema.BAD_PATTERN)
    assert "pattern" in problem.message


def test_a_supported_pattern_is_enforced(project):
    write(project, "echo-args", echo_tool(
        [*ECHO, "{x}"], [{"name": "x", "type": "string", "pattern": r"^[\w./]+$"}]))
    tool = discovery.command_tools(project)[0]
    assert json.loads(run_tool(tool, project, x="src/a.py").content.strip()) == ["src/a.py"]
    with pytest.raises(ValueError):
        tool.Input(x="a b")


def test_every_tool_the_validator_accepts_builds(project):
    write(project, "echo-args", echo_tool(
        [*ECHO, "{a}", "{b}", "{c}"],
        [{"name": "a", "type": "string", "pattern": r"\d+", "max_length": 5},
         {"name": "b", "type": "int", "minimum": 0, "maximum": 3},
         {"name": "c", "type": "list", "item_type": "path"}]))
    found = discovery.discover(project)
    assert [p.id for p in found.plugins] == ["tool.echo-args"]
    assert [t.name for t in discovery.command_tools(project)] == ["echo-args"]


# ---- one parser, and duplicate keys refused ------------------------------

_ARGV_BODY = '\n```json params\n[]\n```\n\n```json argv\n["sh"]\n```\n'


@pytest.mark.parametrize("key,text", [
    ("kind", "---\nkind: prompt\nkind: tool\nname: evil\ndescription: x\n---\n"),
    ("kind", "---\nkind: agent\n\n\tkind: tool\nname: evil\ndescription: x\n---\n"),
    ("name", "---\nkind: prompt\nname: a\nname: b\ndescription: x\n---\n\nbody\n"),
])
def test_a_key_set_twice_is_refused_naming_both_lines(project, key, text):
    """Two readers once took different copies of a repeated ``kind:`` -- the
    trust hash the first, the loader the last. Refusing the file ends the
    question of which copy is meant, for every reader."""
    write(project, "twice", text + _ARGV_BODY)
    found = discovery.discover(project)
    assert found.plugins == []
    problem = next(p for p in found.problems if p.code == schema.DUPLICATE_KEY)
    assert problem.severity == "error"
    assert f"'{key}'" in problem.message
    assert problem.fix


@pytest.mark.parametrize("text", [
    "---\nkind: prompt\nname: p\n---\n\nkind: tool\n",
    "---\nkind:\n  tool\nname: t\n---\n",
    "---\n kind: tool\n---\n",
    "﻿---\nkind: tool\n---\n",
    "---\nkind: TOOL\n---\n",
    "---\nkind: prompt\nkind: tool\n---\n",
])
def test_the_trust_gate_reads_kind_with_the_loaders_parser(text):
    """Single-sourced: the gate classifies a file by exactly the frontmatter the
    loader reads -- body text that mentions a kind is not a kind -- and a file
    the loader refuses for a repeated key is one the gate cannot classify."""
    from quickcode import frontmatter
    from quickcode.kernel.authoring.format import parse_document
    from quickcode.security import trust

    parsed = frontmatter.parse(text)
    assert parsed.meta == parse_document(text).meta
    expected = None if parsed.duplicates else (
        parsed.meta.get("kind", "").strip().lower() or None)
    assert trust._declared_kind(text) == expected


# ---- reserved names come from the live registry --------------------------


def _core_names() -> list[str]:
    from quickcode.tools.registry import core_tools

    return sorted(t.name for t in core_tools())


@pytest.mark.parametrize("name", _core_names())
def test_every_builtin_tool_name_is_refused_to_an_authored_tool(name):
    from quickcode.kernel.authoring.reserved import reserved_reason

    assert reserved_reason(f"tool.{name}", "tool", name), name


def test_web_fetch_cannot_be_authored_over_the_builtin(project):
    write(project, "web_fetch", echo_tool(ECHO, []).replace("name: echo-args", "name: web_fetch"))
    found = discovery.discover(project)
    assert found.plugins == []
    problem = next(p for p in found.problems if p.code == schema.ID_RESERVED)
    assert "built-in tool" in problem.message


def test_a_builtin_added_later_is_reserved_without_editing_a_list(monkeypatch):
    from quickcode.kernel.authoring import reserved
    from quickcode.tools import registry
    from quickcode.tools.base import Tool

    class BashOutput(Tool):
        name = "bash_output"

    shipped = registry.core_tools
    monkeypatch.setattr(registry, "core_tools", lambda **kw: [*shipped(**kw), BashOutput()])
    assert "built-in tool" in reserved.reserved_reason("tool.bash_output", "tool", "bash_output")


def test_the_reserved_names_still_hold_if_the_registry_cannot_be_read(monkeypatch):
    from quickcode.kernel.authoring import reserved
    from quickcode.tools import registry

    def broken(**kw):
        raise RuntimeError("import trouble")

    monkeypatch.setattr(registry, "core_tools", broken)
    for name in ("bash", "web_fetch", "web_search", "read"):
        assert reserved.reserved_reason(f"tool.{name}", "tool", name), name


# ---- the store: writes that cannot half-happen ---------------------------

_PROMPT = "---\nkind: prompt\nname: house\ndescription: x\n---\n\n<house>text</house>\n"


def test_a_save_that_cannot_be_encoded_leaves_the_file_as_it_was(project):
    """JSON can carry a lone surrogate. write_text truncated the file and then
    failed to encode, so the plugin was replaced by an empty file."""
    from quickcode.kernel.authoring import store

    path = write(project, "house", _PROMPT)
    with pytest.raises(store.AuthoringError) as info:
        store.save_source(project, "prompt.house", "---\nkind: prompt\n\ud800\n")
    assert info.value.status == 400
    assert path.read_text(encoding="utf-8") == _PROMPT
    assert [p.name for p in path.parent.iterdir() if p.is_file()] == ["house.md"]


def test_two_deletes_of_one_name_in_one_second_keep_both(project, monkeypatch):
    from quickcode.kernel.authoring import store

    monkeypatch.setattr(store.time, "time", lambda: 1_700_000_000.0)
    write(project, "house", _PROMPT)
    store.delete(project, "prompt.house")
    write(project, "house", _PROMPT.replace("text", "second"))
    store.delete(project, "prompt.house")
    trash = project / ".quickcode" / "plugins" / ".trash"
    kept = sorted(p.read_text(encoding="utf-8") for p in trash.iterdir())
    assert len(kept) == 2, "the second delete overwrote the first"


@pytest.fixture
def real_trust(tmp_path, monkeypatch):
    """The real trust store, moved into tmp_path."""
    import quickcode.config as config_module
    from quickcode.security import trust

    store = trust.TrustStore(tmp_path / "trust.json")
    monkeypatch.setattr(config_module, "CONFIG_DIR", tmp_path / "home")
    monkeypatch.setattr(trust, "default_store", lambda: store)
    project = tmp_path / "proj"
    (project / ".quickcode" / "plugins").mkdir(parents=True)
    (project / ".quickcode" / "settings.json").write_text(json.dumps(
        {"mcpServers": {"docs": {"command": "npx", "args": ["-y", "docs"]}}}),
        encoding="utf-8")
    return project, store


def test_editing_a_projects_tool_in_the_app_keeps_the_project_trusted(real_trust):
    """The trust hash covers command-tool files, so saving one from the editor
    untrusted the project: its MCP servers went inert and its allow rules were
    ignored, for having used the app's own editor."""
    from quickcode.kernel.authoring import store

    project, trust_store = real_trust
    write(project, "echo-args", echo_tool(ECHO, []))
    trust_store.grant(project)

    store.save_source(project, "tool.echo-args", echo_tool([*ECHO, "x"], []))
    assert trust_store.is_trusted(project)
    store.create(project, kind="tool", name="second")
    assert trust_store.is_trusted(project)
    store.delete(project, "tool.second")
    assert trust_store.is_trusted(project)

    # An edit made outside the app still re-prompts, and an untrusted project
    # is never trusted by a save.
    (project / ".quickcode" / "plugins" / "echo-args.md").write_text(
        echo_tool([*ECHO, "y"], []), encoding="utf-8")
    assert not trust_store.is_trusted(project)
    store.save_source(project, "tool.echo-args", echo_tool([*ECHO, "z"], []))
    assert not trust_store.is_trusted(project)
