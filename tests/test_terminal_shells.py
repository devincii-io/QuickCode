"""Which shell the terminal panel opens on Linux and macOS.

The panel promised "your login shell elsewhere" and started ``/bin/bash``
unconditionally, so a macOS user — zsh by default since 10.15 — got the
system's bash 3.2 without their profile, PATH or prompt.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from quickcode.pty import shells

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX shells")


def _fake_shell(tmp_path: Path, name: str) -> str:
    path = tmp_path / name
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


@pytest.fixture
def no_passwd_shell(monkeypatch):
    monkeypatch.setattr(shells, "_passwd_shell", lambda: None)


def test_the_panel_opens_the_shell_named_in_SHELL(tmp_path, monkeypatch) -> None:
    zsh = _fake_shell(tmp_path, "zsh")
    monkeypatch.setenv("SHELL", zsh)
    assert shells.interactive_shell_argv() == [zsh, "-i", "-l"]


def test_without_SHELL_the_password_database_decides(tmp_path, monkeypatch) -> None:
    fish = _fake_shell(tmp_path, "fish")
    monkeypatch.delenv("SHELL", raising=False)
    monkeypatch.setattr(shells, "_passwd_shell", lambda: fish)
    assert shells.interactive_shell_argv() == [fish, "-i", "-l"]


def test_a_SHELL_that_does_not_exist_falls_back(tmp_path, monkeypatch, no_passwd_shell) -> None:
    monkeypatch.setenv("SHELL", str(tmp_path / "gone" / "zsh"))
    argv = shells.interactive_shell_argv()
    assert argv[0] in shells.FALLBACKS and os.path.exists(argv[0])


def test_an_account_that_refuses_logins_still_gets_a_prompt(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SHELL", _fake_shell(tmp_path, "nologin"))
    monkeypatch.setattr(shells, "_passwd_shell", lambda: "/usr/sbin/nologin")
    assert shells.interactive_shell_argv()[0] in shells.FALLBACKS


def test_a_relative_SHELL_is_not_resolved_against_the_project(
    tmp_path, monkeypatch, no_passwd_shell
) -> None:
    """The shell starts in the project directory; a relative ``$SHELL`` would
    run whatever file of that name the repository happens to contain."""
    monkeypatch.chdir(tmp_path)
    _fake_shell(tmp_path, "zsh")
    monkeypatch.setenv("SHELL", "zsh")
    assert shells.interactive_shell_argv()[0] in shells.FALLBACKS


def test_csh_is_asked_for_a_login_the_only_way_it_accepts(tmp_path, monkeypatch) -> None:
    tcsh = _fake_shell(tmp_path, "tcsh")
    monkeypatch.setenv("SHELL", tcsh)
    assert shells.interactive_shell_argv() == [tcsh, "-l"]


def test_an_unknown_shell_gets_no_flags_it_might_reject(tmp_path, monkeypatch) -> None:
    nu = _fake_shell(tmp_path, "nu")
    monkeypatch.setenv("SHELL", nu)
    assert shells.interactive_shell_argv() == [nu]
