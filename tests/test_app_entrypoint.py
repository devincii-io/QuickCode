"""The windowed GUI entry point and its no-console guards.

``main_app`` backs both the ``quickcode-app`` console script a wheel install
provides and ``QuickCodeApp.exe`` in the frozen build.
"""

from __future__ import annotations

import sys
from pathlib import Path

from quickcode import cli


def test_main_app_exists():
    assert callable(cli.main_app)


def test_main_app_opens_the_home_directory(monkeypatch):
    seen: list[list[str]] = []
    monkeypatch.setattr(cli, "main", lambda argv: seen.append(argv))
    monkeypatch.setattr(sys, "argv", ["quickcode-app"])

    cli.main_app()

    assert seen == [["--cwd", str(Path.home())]]


def test_main_app_forwards_extra_arguments(monkeypatch):
    seen: list[list[str]] = []
    monkeypatch.setattr(cli, "main", lambda argv: seen.append(argv))
    monkeypatch.setattr(sys, "argv", ["quickcode-app", "--no-browser"])

    cli.main_app()

    assert seen == [["--cwd", str(Path.home()), "--no-browser"]]


def test_a_folder_argument_wins_over_the_home_default(monkeypatch, tmp_path):
    """The Explorer context menu runs ``QuickCodeApp.exe "%V"`` on the clicked
    folder. Prepending ``--cwd <home>`` regardless would discard it -- argparse
    takes the last ``--cwd``, and the folder arrives as a positional."""
    seen: list[list[str]] = []
    monkeypatch.setattr(cli, "main", lambda argv: seen.append(argv))
    monkeypatch.setattr(sys, "argv", ["QuickCodeApp", str(tmp_path)])

    cli.main_app()

    assert seen == [[str(tmp_path)]]


def test_an_explicit_cwd_wins_over_the_home_default(monkeypatch, tmp_path):
    seen: list[list[str]] = []
    monkeypatch.setattr(cli, "main", lambda argv: seen.append(argv))
    monkeypatch.setattr(sys, "argv", ["QuickCodeApp", "--cwd", str(tmp_path)])

    cli.main_app()

    assert seen == [["--cwd", str(tmp_path)]]


def test_a_prompt_that_is_not_a_directory_still_gets_the_home_default(monkeypatch):
    """``qc "fix the build"`` shape: a non-path positional is a prompt, not a
    project, so the home directory is still what the window opens on."""
    seen: list[list[str]] = []
    monkeypatch.setattr(cli, "main", lambda argv: seen.append(argv))
    monkeypatch.setattr(sys, "argv", ["QuickCodeApp", "fix the build"])

    cli.main_app()

    assert seen == [["--cwd", str(Path.home()), "fix the build"]]


def test_say_prints_when_there_is_a_console(capsys):
    cli._say("hello")
    assert capsys.readouterr().out == "hello\n"


def test_say_is_a_noop_without_stdout(monkeypatch):
    """Under pythonw sys.stdout is None; _say must not blow up."""
    monkeypatch.setattr(sys, "stdout", None)
    cli._say("hello")  # must not raise


def test_bind_null_streams_replaces_missing_streams(monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    cli._bind_null_streams()

    assert sys.stdout is not None
    assert sys.stderr is not None
    sys.stdout.write("swallowed")  # writable, goes nowhere
    sys.stderr.write("swallowed")


def test_bind_null_streams_leaves_real_streams_alone(monkeypatch):
    sentinel = sys.stdout
    cli._bind_null_streams()
    assert sys.stdout is sentinel
