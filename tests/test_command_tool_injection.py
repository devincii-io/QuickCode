"""Argument injection through an authored command tool.

Argv execution closes the shell, but not the program's own option parser: a
model-supplied ``--basetemp=/`` handed to ``pytest`` as a positional is one
inert argv element that pytest reads as an option -- and that one deletes a
directory. These tests pin the rule that a value may not *begin* an argv
element with ``-`` unless the author said it may.
"""

from __future__ import annotations

import json

import pytest

from quickcode.kernel.authoring import discovery
from tests.test_authoring import ECHO, echo_tool, run_tool, write


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """A trusted project with the user scope moved into tmp_path."""
    import quickcode.config as config_module
    from quickcode.security import trust

    home = tmp_path / "home" / ".quickcode"
    (home / "plugins").mkdir(parents=True)
    project = tmp_path / "proj"
    (project / ".quickcode" / "plugins").mkdir(parents=True)
    monkeypatch.setattr(config_module, "CONFIG_DIR", home)
    monkeypatch.setattr(trust, "is_trusted", lambda cwd: True)
    return project


def _tool(project, argv, params):
    write(project, "echo-args", echo_tool(argv, params))
    found = discovery.discover(project)
    assert found.plugins, [p.message for p in found.problems]
    return found.plugins[0].to_tool()


@pytest.mark.parametrize("value", ["--basetemp=/", "-pevil_plugin", "-", "--"])
def test_a_string_that_would_read_as_an_option_is_refused(sandbox, value):
    tool = _tool(sandbox, [*ECHO, "{target}"],
                 [{"name": "target", "type": "string", "required": True}])
    result = run_tool(tool, sandbox, target=value)
    assert result.is_error
    assert "starts with '-'" in result.content
    assert "argv" not in (result.ui_meta or {})  # refused before anything ran


def test_a_path_that_would_read_as_an_option_is_refused_with_the_safe_spelling(sandbox):
    tool = _tool(sandbox, [*ECHO, "{path}"], [{"name": "path", "type": "path"}])
    # Resolves inside the project, so the containment check alone passed it.
    result = run_tool(tool, sandbox, path="--basetemp=/")
    assert result.is_error
    assert "./--basetemp=/" in result.content
    ok = run_tool(tool, sandbox, path="./-odd-name.txt")
    assert not ok.is_error
    assert json.loads(ok.content.strip()) == ["./-odd-name.txt"]


def test_a_list_item_that_would_read_as_an_option_is_refused(sandbox):
    tool = _tool(sandbox, [*ECHO, "{files}"],
                 [{"name": "files", "type": "list", "item_type": "string"}])
    assert run_tool(tool, sandbox, files=["a.py", "--rm"]).is_error
    assert not run_tool(tool, sandbox, files=["a.py", "b-c.py"]).is_error


def test_a_value_that_starts_a_mixed_element_is_checked_too(sandbox):
    tool = _tool(sandbox, [*ECHO, "{name}.txt"],
                 [{"name": "name", "type": "string", "required": True}])
    assert run_tool(tool, sandbox, name="-rf").is_error


def test_a_value_glued_behind_literal_text_cannot_become_an_option(sandbox):
    tool = _tool(sandbox, [*ECHO, "--name={name}"],
                 [{"name": "name", "type": "string", "required": True}])
    result = run_tool(tool, sandbox, name="--evil")
    assert not result.is_error
    assert json.loads(result.content.strip()) == ["--name=--evil"]


def test_after_a_literal_double_dash_a_dash_value_is_a_positional(sandbox):
    tool = _tool(sandbox, [*ECHO, "--", "{pattern}"],
                 [{"name": "pattern", "type": "string", "required": True}])
    result = run_tool(tool, sandbox, pattern="-foo")
    assert not result.is_error
    assert json.loads(result.content.strip()) == ["--", "-foo"]


def test_the_author_can_allow_a_leading_dash_for_one_parameter(sandbox):
    tool = _tool(sandbox, [*ECHO, "-e", "{pattern}"],
                 [{"name": "pattern", "type": "string", "required": True,
                   "allow_leading_dash": True}])
    result = run_tool(tool, sandbox, pattern="-foo")
    assert not result.is_error
    assert json.loads(result.content.strip()) == ["-e", "-foo"]


def test_numbers_and_author_chosen_values_are_not_refused(sandbox):
    tool = _tool(sandbox, [*ECHO, "{n}", "{level}"],
                 [{"name": "n", "type": "int", "default": 0},
                  {"name": "level", "type": "enum", "choices": ["-v", "-q"]}])
    result = run_tool(tool, sandbox, n=-3, level="-q")
    assert not result.is_error
    assert json.loads(result.content.strip()) == ["-3", "-q"]


def test_a_float_cannot_smuggle_letters_in_as_an_option(sandbox):
    tool = _tool(sandbox, [*ECHO, "{x}"], [{"name": "x", "type": "float", "default": 0}])
    assert run_tool(tool, sandbox, x=float("-inf")).is_error
    assert not run_tool(tool, sandbox, x=-1.5).is_error
