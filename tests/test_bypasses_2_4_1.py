"""Six ways past a boundary, found by an adversarial sweep of 2.4.0.

Each one was reproduced before it was fixed, and each assertion here is the
reproduction. They share a shape worth naming: every one of them came from
comparing a *string* against a rule instead of the thing the string will become
— an unexpanded `$HOME`, a quoted `.en''v`, a `pattern` nobody gated, a flag
spelled `-fr` instead of `-rf`, a conversation id that was really `..`, and a
filename assumed unique because a counter starts at 1.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from quickcode.core.permissions import Decision, Mode, PermissionEngine, Rules
from quickcode.session.store import SessionStore, purge_sessions, safe_conv_id
from quickcode.subagents.artifacts import write_artifact
from quickcode.tools.glob import GlobTool

PROMPTING = [Mode.plan, Mode.ask, Mode.auto_edit]


def engine(mode: Mode = Mode.ask, root: Path | None = None, **rules) -> PermissionEngine:
    return PermissionEngine(mode=mode, rules=Rules(**rules), root=root or Path.cwd())


@pytest.mark.parametrize("mode", PROMPTING)
@pytest.mark.parametrize("command", [
    "cat $HOME/.aws/credentials",
    "cat ${HOME}/.ssh/id_rsa",
    "cat $(echo /etc/passwd)",
    "cat `pwd`/../secrets.txt",
    "cat %USERPROFILE%/.ssh/id_rsa",
])
def test_a_path_built_from_an_expansion_is_not_assumed_to_be_inside_the_project(mode, command):
    """It resolved as a literal relative path, so `$HOME/.aws/credentials`
    looked like a file inside the project and auto-allowed — in every mode,
    while the same file named plainly prompted. The shell expands it long after
    this decision is made, so the engine cannot know where it points."""
    assert engine(mode).evaluate("bash", command) == Decision.ask


def test_an_expansion_is_denied_rather_than_allowed_where_there_is_nobody_to_ask():
    assert engine(Mode.dontask).evaluate("bash", "cat $HOME/.aws/credentials") == Decision.deny


@pytest.mark.parametrize("command", ["cat .en''v", 'cat ".env"', "cat .e''nv", "cat '.env'"])
def test_quoting_a_protected_name_does_not_hide_it(command):
    """The shell concatenates `.en''v` into `.env`; the scan stripped quotes
    only from the ends, so it compared a string the shell never sees."""
    assert engine().evaluate("bash", command) == Decision.ask
    assert engine().evaluate("bash", "cat .env") == Decision.ask   # the plain form, unchanged


@pytest.mark.parametrize("pattern", ["../*/*.txt", "../../**/*.py", "/etc/*", "$HOME/*"])
def test_a_glob_pattern_that_leaves_the_project_is_gated(pattern):
    """`glob` declared `path` as its permission target — an *optional* field —
    while the place it reads is `path` joined with the required `pattern`. So
    `glob(pattern="../*/*.txt")` offered an empty target, matched nothing, and
    enumerated files outside the project unprompted in every mode."""
    decision, target = engine().evaluate_tool(GlobTool(), {"pattern": pattern})
    assert decision == Decision.ask
    assert target, "the engine was handed an empty target again"


@pytest.mark.parametrize("args", [
    {"pattern": "src/**/*.py"},
    {"pattern": "**/*.md"},
    {"pattern": "*.py", "path": "quickcode"},
])
def test_an_ordinary_glob_inside_the_project_still_runs_unprompted(args):
    assert engine().evaluate_tool(GlobTool(), args)[0] == Decision.allow


@pytest.mark.parametrize("command", [
    "rm -rf /", "rm -rf /*", "rm -fr /", "rm --recursive --force /",
    "rm -rf ~", "rm -rf ~/*", "git push --force", "git push -f",
    "git push origin main -f", "git push --force-with-lease",
])
def test_the_catastrophic_commands_stop_however_they_are_spelled(command):
    """The breakers matched one spelling each, so `rm -fr /` and `git push -f`
    — the same commands — ran with no prompt in the one mode where the breakers
    are the only thing left."""
    assert engine(Mode.yolo).evaluate("bash", command) == Decision.ask


@pytest.mark.parametrize("command", [
    "rm -rf build", "rm -rf ./dist", "git push origin main", "ls -la", "grep -rf pat.txt src",
])
def test_the_breakers_do_not_fire_on_ordinary_commands(command):
    """A breaker that cries wolf is a breaker people learn to click through."""
    assert engine(Mode.yolo).evaluate("bash", command) == Decision.allow


@pytest.mark.parametrize("bad", ["..", "../..", "a/b", "a\\b", "", "x" * 65, "./."])
def test_a_conversation_id_that_is_really_a_path_is_refused(bad):
    assert not safe_conv_id(bad)


def test_the_empty_session_sweep_cannot_delete_the_whole_project_directory(tmp_path: Path):
    """A file named `...jsonl` in the sessions directory made `empty_sessions`
    offer `..` as a sweepable session; the board path is
    `.quickcode/tasks/<id>`, and `.quickcode/tasks/..` is `.quickcode` itself —
    so one click on "clean up empty sessions" removed every session, every task
    board, every artifact and the project's settings."""
    root = tmp_path
    (root / ".quickcode" / "sessions").mkdir(parents=True)
    (root / ".quickcode" / "tasks").mkdir()
    (root / ".quickcode" / "settings.json").write_text("keep me", encoding="utf-8")
    (root / ".quickcode" / "sessions" / "...jsonl").write_text("", encoding="utf-8")
    (root / ".quickcode" / "sessions" / "real1.jsonl").write_text("", encoding="utf-8")

    assert ".." not in SessionStore.empty_sessions(root)
    result = purge_sessions(root, [".."])
    assert result.sessions == [] and result.missing == [".."]
    assert (root / ".quickcode").is_dir()
    assert (root / ".quickcode" / "settings.json").read_text(encoding="utf-8") == "keep me"


def test_two_sessions_offloading_the_same_agent_id_keep_both_reports(tmp_path: Path):
    """Agent ids come from a counter that restarts at 1 every conversation, so
    `explore-1.md` is the first offloaded report of every session — and the
    write was a plain overwrite. Opening a new conversation and fanning out
    silently destroyed the previous session's report while its transcript went
    on pointing at the file, now describing someone else's work."""
    first = write_artifact(tmp_path, "explore-1", "SESSION ONE")
    second = write_artifact(tmp_path, "explore-1", "SESSION TWO")
    assert first != second
    assert first.read_text(encoding="utf-8") == "SESSION ONE"
    assert second.read_text(encoding="utf-8") == "SESSION TWO"
