"""A recursive read is gated by what it reaches, not by the directory it names.

`grep -r API_KEY .` names `.`, which is inside the project and not protected,
and printed every line of `.env` and `.git/config` on the way through -- with
the read-only auto-allow, in every mode. The `grep` tool was fixed to skip
those files on its walk; the shell's grep was the same sweep through the other
door. `rg` skips dotfiles on its own, until `--hidden`, `-uu` or a whitelist
glob (`-g '*'`) tells it not to.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from quickcode.core.permissions import Decision, Mode, PermissionEngine, Rules
from quickcode.security import sweep


def engine(mode: Mode = Mode.ask, root: Path | None = None, **rules) -> PermissionEngine:
    return PermissionEngine(mode=mode, rules=Rules(**rules), root=root or Path.cwd())


@pytest.fixture
def project(tmp_path) -> Path:
    (tmp_path / ".env").write_text("API_KEY=hunter2", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[remote]\nurl=https://t0ken@x", encoding="utf-8")
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "a.py").write_text("API_KEY = env()", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    return tmp_path


@pytest.mark.parametrize("command", [
    "grep -r API_KEY .", "grep -rn API_KEY", "grep -R API_KEY .", "grep --recursive x .",
    "grep -d recurse x .", "rg --hidden API_KEY", "rg -uu API_KEY", "rg -. API_KEY",
    "rg -g '*' API_KEY", "rg --glob=.env API_KEY", "rg --iglob '.ENV' API_KEY",
    "diff -r . docs", "diff -rN docs .",
])
def test_a_recursive_read_that_reaches_a_secret_is_protected(command, project):
    assert engine(root=project).evaluate("bash", command) == Decision.ask
    assert engine(Mode.dontask, root=project).evaluate("bash", command) == Decision.deny


def test_an_allow_rule_does_not_cover_the_sweep(project):
    """It is the protected-path check, so it comes before allow rules."""
    e = engine(root=project, allow=["bash(grep **)"])
    assert e.evaluate("bash", "grep -r API_KEY .") == Decision.ask


@pytest.mark.parametrize("command", [
    "grep -rn API_KEY src", "grep -r x src/*", "rg API_KEY", "rg -g '*.py' API_KEY",
    "rg -u API_KEY", "diff -r src docs", "grep -n API_KEY src/pkg/a.py",
])
def test_a_recursive_read_of_a_clean_tree_stays_unprompted(command, project):
    """`rg` skips hidden files by itself (`-u` alone is --no-ignore, not
    --hidden), and a tree with nothing protected in it is just a tree."""
    assert engine(root=project).evaluate("bash", command) == Decision.allow


def test_a_secret_deeper_in_the_tree_is_found(project):
    (project / "src" / "pkg" / ".env.local").write_text("X=1", encoding="utf-8")
    assert engine(root=project).evaluate("bash", "grep -r X src") == Decision.ask


def test_a_followed_symlink_out_of_the_project_is_found(project, tmp_path_factory):
    outside = tmp_path_factory.mktemp("home")
    try:
        (project / "src" / "keys").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted on this machine")
    e = engine(root=project)
    assert e.evaluate("bash", "grep -R x src") == Decision.ask
    assert e.evaluate("bash", "rg -L x src") == Decision.ask
    # grep -r does not follow a link it meets on the way down.
    assert e.evaluate("bash", "grep -r x src") == Decision.allow


def test_a_tree_too_large_to_walk_is_treated_as_reaching_one(project, monkeypatch):
    for i in range(30):
        (project / "docs" / f"page{i}.md").write_text("x", encoding="utf-8")
    monkeypatch.setattr(sweep, "LIMIT", 10)
    assert engine(root=project).evaluate("bash", "grep -r x docs") == Decision.ask


def test_yolo_does_not_walk(project):
    assert engine(Mode.yolo, root=project).evaluate("bash", "grep -r API_KEY .") == (
        Decision.allow
    )
