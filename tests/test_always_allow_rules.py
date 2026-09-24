""""Always allow" saves exactly what was approved, and nothing next to it.

The rule it used to write was ``bash(<first word> *)`` for the whole line, and a
single ``*`` spans spaces: approving ``FOO=1 make`` saved ``bash(FOO=1 *)``,
which then allowed ``FOO=1 rm -rf build``; approving ``git status && rm -rf x``
saved ``bash(git *)``, which allowed every git command there is (COMPLIANCE W7).
The rules are now read off the engine's own decomposition of the call: one
exact rule per part that asked, and none for a part that would ask again
whatever is saved.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from quickcode.core.permissions import Decision, Mode, PermissionEngine, Rules
from quickcode.tools.registry import default_registry


def engine(root: Path, mode: Mode = Mode.ask, **rules) -> PermissionEngine:
    return PermissionEngine(mode=mode, rules=Rules(**rules), root=root)


def tool(name: str):
    return default_registry().get(name)


def offer(root: Path, command: str, **rules):
    return engine(root, **rules).suggest_rules(tool("bash"), {"command": command})


def after(root: Path, rules, command: str, **existing) -> Decision:
    """The decision for ``command`` once ``rules`` are saved."""
    allow = [*existing.pop("allow", []), *rules]
    return engine(root, allow=allow, **existing).evaluate("bash", command)


def test_an_environment_prefix_is_saved_as_written_and_covers_nothing_else(tmp_path):
    got = offer(tmp_path, "FOO=1 make")
    assert got.rules == ("bash(FOO=1 make)",)
    assert after(tmp_path, got.rules, "FOO=1 make") == Decision.allow
    # The rule the old heuristic wrote, bash(FOO=1 *), allowed this.
    assert after(tmp_path, got.rules, "FOO=1 rm -rf build") == Decision.ask
    assert after(tmp_path, got.rules, "FOO=1 make install") == Decision.ask


def test_a_compound_line_saves_one_exact_rule_per_subcommand(tmp_path):
    got = offer(tmp_path, "git status && rm -rf x")
    assert got.rules == ("bash(git status)", "bash(rm -rf x)")
    assert got.kept == ()
    assert after(tmp_path, got.rules, "git status && rm -rf x") == Decision.allow
    # bash(git *) used to be saved here, and it covered every git command.
    assert after(tmp_path, got.rules, "git push origin main") == Decision.ask
    assert after(tmp_path, got.rules, "rm -rf src") == Decision.ask


def test_the_documented_example_saves_both_halves(tmp_path):
    got = offer(tmp_path, "npm test && git push")
    assert got.rules == ("bash(npm test)", "bash(git push)")
    assert after(tmp_path, got.rules, "npm test && git push") == Decision.allow
    assert after(tmp_path, got.rules, "npm publish") == Decision.ask


def test_a_part_the_engine_already_allows_gets_no_rule(tmp_path):
    got = offer(tmp_path, "ls && make", allow=["bash(git status)"])
    assert got.rules == ("bash(make)",)
    got = offer(tmp_path, "git status; make", allow=["bash(git status)"])
    assert got.rules == ("bash(make)",)


@pytest.mark.parametrize(("command", "rules"), [
    ("sudo rm x", ("bash(sudo rm x)", "bash(rm x)")),
    ("nice make", ("bash(nice make)", "bash(make)")),
    ("timeout 5 make test", ("bash(timeout 5 make test)", "bash(make test)")),
    ("env FOO=1 rm x", ("bash(env FOO=1 rm x)", "bash(rm x)")),
])
def test_a_wrapper_saves_the_line_and_the_command_it_runs_both_exact(tmp_path, command, rules):
    """The engine judges the wrapper line and the command it runs separately,
    so both need a rule -- and each is exactly what was approved."""
    got = offer(tmp_path, command)
    assert got.rules == rules
    assert after(tmp_path, got.rules, command) == Decision.allow
    assert after(tmp_path, got.rules, "sudo rm -rf y") == Decision.ask
    assert after(tmp_path, got.rules, "rm -rf y") == Decision.ask


def test_a_command_run_by_another_gets_its_own_rule(tmp_path):
    got = offer(tmp_path, "find . -name x -exec rm {} +")
    assert "bash(find . -name x -exec rm {} +)" in got.rules
    assert after(tmp_path, got.rules, "find . -name x -exec rm {} +") == Decision.allow
    assert after(tmp_path, got.rules, "find . -name y -exec rm {} +") == Decision.ask


def test_a_line_that_trips_a_circuit_breaker_saves_nothing(tmp_path):
    got = offer(tmp_path, "npm test && git push -f origin main")
    assert got.rules == ()
    assert ("npm test && git push -f origin main", "circuit_breaker") in got.kept


def test_a_breaker_inside_another_command_saves_nothing_either(tmp_path):
    got = offer(tmp_path, "bash -c 'git push --force origin main'")
    assert got.rules == ()
    assert any(reason == "circuit_breaker" for _, reason in got.kept)


def test_a_protected_path_gets_no_rule_and_is_named(tmp_path):
    got = offer(tmp_path, "npm test && cat .env")
    assert got.rules == ("bash(npm test)",)
    assert got.kept == (("cat .env", "protected_path"),)


def test_an_ask_rule_is_not_answered_by_a_rule_that_could_never_fire(tmp_path):
    got = offer(tmp_path, "git push && make", ask=["bash(git push**)"])
    assert got.rules == ("bash(make)",)
    assert got.kept == (("git push", "ask_rule"),)


def test_a_line_allow_rules_never_see_is_named_once_and_gets_no_rule(tmp_path):
    for command in ("echo hi > out.txt", "npm test 2>&1"):
        got = offer(tmp_path, command)
        assert got.rules == (), command
        assert got.kept == ((command, "substitution"),)


def test_what_a_substitution_runs_is_judged_and_saved_on_its_own(tmp_path):
    """`$(...)` keeps the line itself from any allow rule; the command it runs
    was judged separately, and that exact command is what was approved."""
    got = offer(tmp_path, "echo $(npm test)")
    assert "bash(echo $(npm test))" not in got.rules
    assert "bash(npm test)" in got.rules
    assert got.kept


def test_a_program_pointed_at_unseen_code_gets_no_rule(tmp_path):
    got = offer(tmp_path, "git -c core.pager=less log")
    assert "bash(git -c core.pager=less log)" not in got.rules
    assert ("git -c core.pager=less log", "opaque") in got.kept


def test_a_literal_star_cannot_be_spelled_so_no_rule_is_offered(tmp_path):
    """A rule's `*` is a wildcard with no escape: `bash(rm *.pyc)` would also
    allow `rm -rf src x.pyc`."""
    got = offer(tmp_path, "rm *.pyc && make")
    assert got.rules == ("bash(make)",)
    assert got.kept == (("rm *.pyc", "wildcard"),)


@pytest.mark.parametrize("command", [
    "npm install left-pad",
    "FOO=1 BAR=2 make -j4",
    "git status && git diff --stat | tail -1",
    "python -m pytest tests/test_a.py -q",
    "xargs -n1 echo < list.txt",
    "cd sub && make && cd ..",
    "nohup uv run server &",
    "echo 'a|b'",
])
def test_whatever_is_saved_stops_the_next_prompt_for_the_same_call(tmp_path, command):
    got = offer(tmp_path, command)
    if got.kept:
        assert after(tmp_path, got.rules, command) == Decision.ask
    else:
        assert got.rules
        assert after(tmp_path, got.rules, command) == Decision.allow


def test_a_path_tool_saves_the_path_it_was_called_with(tmp_path):
    e = engine(tmp_path)
    got = e.suggest_rules(tool("edit"), {"file_path": "src/a.py"})
    assert got.rules == ("edit(src/a.py)",)
    assert engine(tmp_path, allow=list(got.rules)).evaluate("edit", "src/a.py") == Decision.allow
    assert engine(tmp_path, allow=list(got.rules)).evaluate("edit", "src/b.py") == Decision.ask


def test_a_protected_path_tool_call_saves_nothing(tmp_path):
    for target in (".env", ".git/config", "../outside.txt"):
        got = engine(tmp_path).suggest_rules(tool("write"), {"file_path": target})
        assert got.rules == (), target
        assert got.kept == ((target, "protected_path"),)


def test_a_path_with_a_star_in_it_saves_nothing(tmp_path):
    got = engine(tmp_path).suggest_rules(tool("write"), {"file_path": "src/*.py"})
    assert got.rules == ()
    assert got.kept == (("src/*.py", "wildcard"),)


def test_a_call_the_engine_does_not_ask_about_offers_nothing(tmp_path):
    """A hook can force a prompt the engine would not have raised; there is
    nothing to save for that, since the rules already allow the call."""
    e = engine(tmp_path, Mode.auto_edit)
    assert e.suggest_rules(tool("write"), {"file_path": "out.txt"}).rules == ()
    assert engine(tmp_path).suggest_rules(tool("bash"), {"command": "ls"}).rules == ()


def test_the_shell_stands_where_the_session_left_it(tmp_path):
    """Asked from outside the project, every part is a protected path."""
    got = engine(tmp_path).suggest_rules(tool("bash"), {"command": "make"},
                                         cwd=tmp_path.parent)
    assert got.rules == ()
    assert got.kept == (("make", "protected_path"),)
