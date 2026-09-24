"""``quickcode/gitcmd.py``: git, run without running the repository's own code.

Every call is made against a real repository whose ``.git/config`` names a
program -- the configuration a downloaded archive carries with it -- and the
test is whether that program ran.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from quickcode import gitcmd

pytestmark = [
    pytest.mark.skipif(
        subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
        reason="git unavailable",
    ),
    pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX shell scripts as programs"),
]


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                          text=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "user.name", "Test")
    (root / "a.txt").write_text("one\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


def tripwire(tmp_path: Path, name: str) -> tuple[str, Path]:
    """A filter program that records that it ran, and passes content through."""
    marker = tmp_path / f"{name}-ran"
    script = tmp_path / f"{name}.sh"
    script.write_text(f'#!/bin/sh\ntouch "{marker}"\ncat\n', encoding="utf-8")
    script.chmod(0o755)
    return str(script), marker


def filtered(repo: Path, name: str, program: str) -> None:
    git(repo, "config", f"filter.{name}.clean", program)
    git(repo, "config", f"filter.{name}.smudge", program)
    (repo / ".gitattributes").write_text(f"*.txt filter={name}\n", encoding="utf-8")


def test_a_filter_the_repository_defines_does_not_run_on_status_diff_add_or_checkout(
        repo, tmp_path):
    program, ran = tripwire(tmp_path, "evil")
    filtered(repo, "evil", program)
    # Same size, new content: status cannot tell from the stat data and has to
    # hash the file, which is when it runs the clean filter.
    (repo / "a.txt").write_text("ONE\n", encoding="utf-8")

    assert gitcmd.run(repo, "status", "--porcelain").ok
    assert "+ONE" in gitcmd.run(repo, "diff", *gitcmd.DIFF_SAFE, "HEAD", "--", "a.txt").stdout
    assert gitcmd.run(repo, "add", "-A", write=True).ok
    assert gitcmd.run(repo, "worktree", "add", "--detach", str(tmp_path / "wt"), "HEAD",
                      write=True).ok

    assert (tmp_path / "wt" / "a.txt").read_text(encoding="utf-8") == "one\n"
    assert not ran.exists(), "a filter from .git/config ran"


def test_a_filter_from_the_users_own_config_still_runs(repo, tmp_path, monkeypatch):
    """Git LFS is a filter the user (or Git's installer) defines, not the
    repository; switching it off would read every LFS file as a pointer."""
    program, ran = tripwire(tmp_path, "lfs-like")
    user_config = tmp_path / "user.gitconfig"
    git(repo, "config", "--file", str(user_config), "filter.mine.clean", program)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(user_config))
    (repo / ".gitattributes").write_text("*.txt filter=mine\n", encoding="utf-8")
    (repo / "a.txt").write_text("ONE\n", encoding="utf-8")

    assert gitcmd.run(repo, "add", "-A", write=True).ok
    assert ran.exists()


def test_a_submodule_s_own_filter_does_not_run_through_the_parent(repo, tmp_path):
    """``git status`` runs ``git status`` inside every submodule, with the
    submodule's own config -- a filter list read from the parent cannot reach
    it. ``--ignore-submodules=dirty`` stops the recursion, and as a flag it
    outranks an ``ignore = none`` the repository's ``.gitmodules`` can set."""
    program, ran = tripwire(tmp_path, "sub-evil")
    sub = tmp_path / "sub"
    sub.mkdir()
    git(sub, "init", "-q", "-b", "main")
    (sub / "s.txt").write_text("one\n", encoding="utf-8")
    (sub / ".gitattributes").write_text("*.txt filter=evil\n", encoding="utf-8")
    git(sub, "add", "-A")
    git(sub, "-c", "user.email=t@e.invalid", "-c", "user.name=T", "commit", "-q", "-m", "i")
    git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "sub")
    git(repo, "config", "-f", ".gitmodules", "submodule.sub.ignore", "none")
    git(repo, "commit", "-q", "-m", "sub")
    git(repo / "sub", "config", "filter.evil.clean", program)
    (repo / "sub" / "s.txt").write_text("ONE\n", encoding="utf-8")

    assert gitcmd.run(repo, "status", "--porcelain", *gitcmd.STATUS_SAFE).ok
    gitcmd.run(repo, "diff", *gitcmd.DIFF_SAFE, "HEAD", "--", "sub")

    assert not ran.exists(), "the submodule's filter ran"


def test_a_filter_whose_name_cannot_be_switched_off_is_not_run_around(repo, tmp_path):
    """``-c key=value`` splits at the first ``=``, so a filter named ``x=y``
    cannot be named in an override. The call is refused rather than made."""
    program, ran = tripwire(tmp_path, "eq")
    filtered(repo, "x=y", program)
    (repo / "a.txt").write_text("ONE\n", encoding="utf-8")

    result = gitcmd.run(repo, "status", "--porcelain")

    assert not result.ok and "filter" in result.reason()
    assert not ran.exists()


def test_the_ext_transport_is_refused_whatever_the_repository_allows(repo, tmp_path):
    marker = tmp_path / "ext-ran"
    git(repo, "config", "protocol.allow", "always")
    git(repo, "config", "protocol.ext.allow", "always")

    gitcmd.run(repo, "ls-remote", f"ext::sh -c touch% {marker}")

    assert not marker.exists()


def test_git_gets_none_of_quickcodes_keys(monkeypatch):
    monkeypatch.setenv("QUICKCODE_OPENROUTER_API_KEY", "sk-or-not-for-git")
    monkeypatch.setenv("QUICKCODE_ANTHROPIC_API_KEY", "sk-ant-not-for-git")
    monkeypatch.setenv("GIT_DIR", "/somewhere/else")

    env = gitcmd.environment()

    assert "QUICKCODE_OPENROUTER_API_KEY" not in env
    assert "QUICKCODE_ANTHROPIC_API_KEY" not in env
    assert "GIT_DIR" not in env
