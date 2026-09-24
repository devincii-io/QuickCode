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
