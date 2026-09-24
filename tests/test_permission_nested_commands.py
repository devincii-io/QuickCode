"""A command another command runs is decided as if it had been typed.

Before: the engine looked at the first word of each subcommand. `find`,
`xargs`, `env`, `sudo`, `bash -c`, `eval`, `$(...)` and a git alias all run a
second program the engine never saw, so a deny rule on that program did not
fire (yolo ran it) and an allow rule on the first program covered it
(`bash(find **)` approved `find . -exec rm -rf {} +`). The read-only builtins
had the same hole from the other side: `rg --pre` runs a program and `tree -o`
writes a file, both unprompted in plan mode.
"""

from __future__ import annotations

import base64
import shlex
from pathlib import Path

import pytest

from quickcode.core.permissions import Decision, Mode, PermissionEngine, Rules


def engine(mode: Mode = Mode.ask, root: Path | None = None, **rules) -> PermissionEngine:
    return PermissionEngine(mode=mode, rules=Rules(**rules), root=root or Path.cwd())


def _encoded(command: str) -> str:
    return base64.b64encode(command.encode("utf-16-le")).decode()


WRAPPED_RM = [
    "env rm -rf build", "env -i PATH=/bin rm -rf build", "sudo rm -rf build",
    "sudo -u root rm -rf build", "command rm -rf build", "exec rm -rf build",
    "nohup rm -rf build", "nice -n 5 rm -rf build", "timeout 5 rm -rf build",
    "xargs rm -rf < list", "xargs -0 -I {} rm -rf {}", "find . -exec rm -rf {} +",
    "find . -name x -execdir rm -rf {} ;", "eval rm -rf build", "eval 'rm -rf build'",
    "bash -c 'rm -rf build'", "sh -lc \"rm -rf build\"", "bash -o pipefail -c 'rm -rf build'",
    "echo $(rm -rf build)", 'echo "$(rm -rf build)"', "echo `rm -rf build`",
    "cat <(rm -rf build)", "if true; then rm -rf build; fi", "{ rm -rf build; }",
    "watch -n 1 rm -rf build", "busybox rm -rf build", "stdbuf -oL rm -rf build",
    "cmd /c rm -rf build", "powershell -NoProfile -Command rm -rf build",
    f"powershell -EncodedCommand {_encoded('rm -rf build')}",
    "git -c alias.x='!rm -rf build' x", "git rebase -x 'rm -rf build' main",
    "git bisect run rm -rf build", "git submodule foreach rm -rf build",
    "rg --pre rm x src",
]


@pytest.mark.parametrize("command", WRAPPED_RM)
def test_a_deny_rule_sees_the_command_a_wrapper_runs(command, tmp_path):
    """Yolo is where this matters: a deny rule is the only thing left."""
    e = engine(Mode.yolo, root=tmp_path, deny=["bash(rm **)"])
    assert e.evaluate("bash", command) == Decision.deny


@pytest.mark.parametrize(("allowed", "command"), [
    ("bash(find **)", "find . -exec rm -rf {} +"),
    ("bash(xargs **)", "xargs rm -rf"),
    ("bash(env **)", "env FOO=1 rm -rf build"),
    ("bash(sudo **)", "sudo rm -rf build"),
    ("bash(bash **)", "bash -c 'rm -rf build'"),
    ("bash(git **)", "git -c alias.x='!rm -rf build' x"),
    ("bash(git **)", "git rebase --exec 'rm -rf build' main"),
    ("bash(rg **)", "rg --pre=rm x src"),
])
def test_an_allow_rule_does_not_cover_the_command_it_runs(allowed, command, tmp_path):
    e = engine(root=tmp_path, allow=[allowed])
    assert e.evaluate("bash", command) == Decision.ask


def test_allowing_both_commands_allows_the_pair(tmp_path):
    e = engine(root=tmp_path, allow=["bash(find **)", "bash(rm **)"])
    assert e.evaluate("bash", "find . -name '*.pyc' -exec rm {} +") == Decision.allow


def test_a_wrapped_read_only_builtin_still_auto_allows(tmp_path):
    assert engine(root=tmp_path).evaluate("bash", "nice cat README.md") == Decision.allow
    assert engine(root=tmp_path).evaluate("bash", "time ls") == Decision.allow


def test_a_wrapped_command_still_meets_the_protected_path_check(tmp_path):
    e = engine(root=tmp_path, allow=["bash(bash **)"])
    assert e.evaluate("bash", "bash -c 'cat .env'") == Decision.ask


def test_nesting_past_the_limit_asks_even_in_yolo(tmp_path):
    line = "echo hi"
    for _ in range(6):
        line = f"bash -c {shlex.quote(line)}"
    assert engine(Mode.yolo, root=tmp_path).evaluate("bash", line) == Decision.ask


@pytest.mark.parametrize("command", [
    "rg --pre ./convert.sh x src", "rg --pre=cat x src", "rg --hostname-bin=./h x",
    "tree -o src/main.py", "tree -aR", "tree -L 2 -o out.txt", "file -C -m magic",
    "file --compile -m magic",
])
def test_a_read_only_builtin_that_runs_or_writes_is_not_read_only(command, tmp_path):
    assert engine(root=tmp_path).evaluate("bash", command) == Decision.ask
    assert engine(Mode.plan, root=tmp_path).evaluate("bash", command) == Decision.deny


@pytest.mark.parametrize("command", ["rg TODO src", "rg -g '*.py' x", "tree -L 2 -a",
                                     "file src/a.py", "rg --pre-glob '*.pdf' x src"])
def test_the_same_builtins_used_plainly_still_auto_allow(command, tmp_path):
    assert engine(root=tmp_path).evaluate("bash", command) == Decision.allow


# --------------------------------------------------------------------------
# git: configuration that names a program
# --------------------------------------------------------------------------

@pytest.mark.parametrize("command", [
    "git -c core.pager='sh -c id' log", "git -c core.fsmonitor=./x status",
    "git -c core.hooksPath=hooks commit -m x", "git --config-env=core.pager=P log",
    "git --exec-path=. status", "git --git-dir=vendored.git log", "git --work-tree=/ status",
    "git clone --template=tpl https://x/r.git", "git clone -c core.fsmonitor=x https://x/r",
    "git fetch --upload-pack='sh -c id' origin", "git clone -u 'sh -c id' https://x/r",
    "git push --receive-pack='sh -c id' origin", "git difftool -x 'sh -c id'",
    "git grep -Ovim foo",
])
def test_git_pointed_at_a_program_is_not_covered_by_a_git_allow_rule(command, tmp_path):
    """`git -c <key>=<value>` can set any of the dozens of keys that name a
    program; `bash(git **)` approved git, not what those keys run."""
    assert engine(root=tmp_path, allow=["bash(git **)"]).evaluate("bash", command) == (
        Decision.ask
    )


@pytest.mark.parametrize("command", [
    "git config core.pager 'sh evil.sh'", "git config --global core.editor vim",
    "git config --add alias.x '!sh'", "git config set core.fsmonitor x",
    "git config --unset user.name", "git config -f .gitmodules x.y z",
])
def test_git_config_writes_are_protected_writes(command, tmp_path):
    """`.git/config` is where every later git command takes its pager, editor
    and hooks path from; writing it is writing a protected path."""
    assert engine(root=tmp_path, allow=["bash(git **)"]).evaluate("bash", command) == (
        Decision.ask
    )
    assert engine(Mode.dontask, root=tmp_path, allow=["bash(git **)"]).evaluate(
        "bash", command
    ) == Decision.deny


@pytest.mark.parametrize("command", [
    "git status", "git log --oneline -5", "git switch -c feature", "git commit -m 'wip'",
    "git config --get user.name", "git config user.email", "git config --list",
    "git -C src status", "git diff HEAD~1", "git -P log -1", "git push -u origin main",
])
def test_ordinary_git_stays_covered(command, tmp_path):
    (tmp_path / "src").mkdir()
    assert engine(root=tmp_path, allow=["bash(git **)"]).evaluate("bash", command) == (
        Decision.allow
    )


def test_git_into_a_committed_bare_repository_is_not_covered(tmp_path):
    """A repository can commit a bare repository -- a directory with `HEAD`
    and a `config` of its own -- and `git -C` into it runs that config's
    pager."""
    bare = tmp_path / "vendored.git"
    bare.mkdir()
    (bare / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (bare / "config").write_text("[core]\n\tpager = sh evil.sh\n", encoding="utf-8")
    e = engine(root=tmp_path, allow=["bash(git **)"])
    assert e.evaluate("bash", "git -C vendored.git log") == Decision.ask
