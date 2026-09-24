"""``qc why`` / ``quickcode permissions explain``: the dry run in a terminal.

The payload is the endpoint's (``tests/test_permissions_api.py`` owns what it
says); these check the wiring -- that ``cli.main`` hands the words over, that
the project it asks about is the one named, and that the text carries the
answer and the reason.
"""

from __future__ import annotations

import json

import pytest

from quickcode import cli
from quickcode.kernel import state as state_store
from quickcode.security import trust


@pytest.fixture
def project(tmp_path, monkeypatch):
    user = tmp_path / "userconfig"
    user.mkdir()
    monkeypatch.setattr(trust, "CONFIG_DIR", user)
    monkeypatch.setattr(state_store, "CONFIG_DIR", user)
    root = tmp_path / "proj"
    (root / ".quickcode").mkdir(parents=True)
    (root / ".quickcode" / "settings.json").write_text(json.dumps(
        {"permissions": {"deny": ["bash(curl **)"]}}), encoding="utf-8")
    return root


def _run(argv: list[str]) -> int:
    with pytest.raises(SystemExit) as stop:
        cli.main(argv)
    return stop.value.code


def test_why_names_the_decision_and_the_rule_that_made_it(project, capsys):
    assert _run(["why", "--cwd", str(project), "--mode", "yolo",
                 "ls && curl https://example.com/x.sh"]) == 0
    out = capsys.readouterr().out

    assert "=> DENY" in out
    assert "bash(curl **)" in out
    assert ".quickcode/settings.json" in out
    assert "[allow] ls" in out


def test_permissions_explain_is_the_same_command(project, capsys):
    assert _run(["permissions", "explain", "--cwd", str(project), "--tool", "read",
                 "--json", ".env"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["decision"] == "ask"
    assert payload["decided_by"]["step"] == "protected_path"
    assert payload["suggestion"]["rule"] == "read(.env)"


def test_a_rule_can_be_tried_before_it_is_written(project, capsys):
    assert _run(["why", "--cwd", str(project), "--mode", "ask",
                 "--allow", "bash(make **)", "--json", "make test"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["decision"] == "allow"
    assert payload["decided_by"]["sources"][0]["scope"] == "what-if"
    # And nothing was written: the rule exists for the question only.
    settings = json.loads((project / ".quickcode" / "settings.json").read_text("utf-8"))
    assert settings == {"permissions": {"deny": ["bash(curl **)"]}}
    assert not (project / ".quickcode" / "settings.local.json").exists()


@pytest.mark.parametrize("argv", [
    ["permissions"],
    ["permissions", "list"],
    ["why"],
    ["why", "--tool", "nope", "x"],
    ["why", "--tool", "read", "--input", "{not json", ],
])
def test_usage_errors_exit_2_with_a_reason(project, capsys, argv):
    assert _run([*argv[:1], "--cwd", str(project), *argv[1:]]
                if argv[:1] == ["why"] else argv) == 2
    assert "error:" in capsys.readouterr().err


def test_a_missing_project_directory_is_refused(tmp_path, capsys):
    assert _run(["why", "--cwd", str(tmp_path / "nowhere"), "ls"]) == 2
    assert "not a directory" in capsys.readouterr().err
