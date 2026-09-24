"""The edges of a headless run: where its prompt comes from, what it prints,
and what its exit status tells the script that ran it.

`test_headless.py` covers what a `-p` run leaves in the session log. This file
covers the process boundary, which is the only thing a shell script sees: a
failed turn used to exit 0 with an empty line, a terminal on stdin hung
forever, and bytes the console could not encode crashed the run after the
model had already answered.
"""

from __future__ import annotations

import io
import json
import sys

import pytest

from quickcode import cli
from quickcode.core.events import TextDelta, TurnDone
from quickcode.core.permissions import Mode
from quickcode.session.store import SessionStore
from tests.test_headless import _headless, _install
from tests.test_server import FakeProvider


class _Terminal(io.StringIO):
    """A stdin that is a terminal: reading it would wait for a human."""

    def isatty(self) -> bool:
        return True

    def read(self, *a):  # pragma: no cover - the point is that it is not called
        raise AssertionError("read a terminal that nobody is typing into")


def _stdin_bytes(monkeypatch, data: bytes) -> None:
    # A strict UTF-8 reader, as Python sets one up for a pipe on a UTF-8 box.
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(data), encoding="utf-8"))


def _exit_code(argv) -> int:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(argv)
    return excinfo.value.code


# ---------------------------------------------------------------- exit codes


def test_a_turn_the_provider_failed_exits_1_and_says_why(tmp_path, monkeypatch, capsys):
    """The loop reports a provider failure on the bus and returns "", so the
    CLI printed a blank line and exited 0 — a bad key looked like success."""
    _install(monkeypatch, FakeProvider([[TurnDone("error", "401: invalid API key")]]))
    assert _exit_code(_headless(tmp_path, "hello")) == 1
    out, err = capsys.readouterr()
    assert out.strip() == ""
    assert "401: invalid API key" in err


def test_a_turn_that_succeeds_exits_0(tmp_path, monkeypatch, capsys):
    _install(monkeypatch, FakeProvider([[TextDelta("fine"), TurnDone("stop")]]))
    cli.main(_headless(tmp_path, "hello"))
    assert capsys.readouterr().out.strip() == "fine"


class _Interrupted(FakeProvider):
    async def stream_chat(self, req):
        yield TextDelta("half")
        raise KeyboardInterrupt


def test_ctrl_c_exits_130_and_leaves_a_readable_log(tmp_path, monkeypatch, capsys):
    _install(monkeypatch, _Interrupted([]))
    assert _exit_code(_headless(tmp_path, "hello")) == 130
    _out, err = capsys.readouterr()
    assert "Traceback" not in err and "interrupted" in err
    path = SessionStore(tmp_path, SessionStore.most_recent(tmp_path)).path
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            json.loads(line)


# --------------------------------------------------------------------- stdin


def test_no_prompt_with_a_terminal_on_stdin_is_a_usage_error_not_a_hang(
    tmp_path, monkeypatch, capsys
):
    _install(monkeypatch, FakeProvider([]))
    monkeypatch.setattr(sys, "stdin", _Terminal())
    assert _exit_code(_headless(tmp_path)) == 2
    assert "no prompt" in capsys.readouterr().err


def test_no_stdin_at_all_is_a_usage_error(tmp_path, monkeypatch, capsys):
    _install(monkeypatch, FakeProvider([]))
    monkeypatch.setattr(sys, "stdin", None)
    assert _exit_code(_headless(tmp_path)) == 2


def test_piped_bytes_in_another_encoding_become_the_prompt(tmp_path, monkeypatch, capsys):
    """`type notes.txt | qc -p` on a German Windows pipes cp1252. Read as
    strict UTF-8 it raised UnicodeDecodeError before the turn began; read with
    surrogateescape it would have died later, in the log or the request."""
    provider = FakeProvider([[TextDelta("ok"), TurnDone("stop")]])
    _install(monkeypatch, provider)
    _stdin_bytes(monkeypatch, b"\xef\xbb\xbfGr\xf6\xdfe von caf\xe9: summarise")
    cli.main(_headless(tmp_path))
    assert capsys.readouterr().out.strip() == "ok"
    sent = provider.requests[0].messages[-1].content
    assert "summarise" in sent
    assert not sent.startswith("﻿")
    sent.encode("utf-8")  # no lone surrogates anywhere in it


def test_a_piped_prompt_in_utf8_arrives_intact(tmp_path, monkeypatch, capsys):
    provider = FakeProvider([[TextDelta("ok"), TurnDone("stop")]])
    _install(monkeypatch, provider)
    _stdin_bytes(monkeypatch, "Größe ändern ✓".encode())
    cli.main(_headless(tmp_path))
    assert "Größe ändern ✓" in provider.requests[0].messages[-1].content


# -------------------------------------------------------------------- stdout


def test_an_answer_the_console_cannot_encode_is_still_printed(tmp_path, monkeypatch):
    """`qc -p ... > out.txt` on Windows writes cp1252. A ✓ in the answer raised
    UnicodeEncodeError after the whole turn had run, and the answer was lost."""
    _install(monkeypatch, FakeProvider([[TextDelta("done ✓ 😀"), TurnDone("stop")]]))
    raw = io.BytesIO()
    out = io.TextIOWrapper(raw, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", out)
    cli.main(_headless(tmp_path, "hello"))
    out.flush()
    assert raw.getvalue().startswith(b"done ")


# --------------------------------------------------------------- arguments


def test_a_project_directory_that_does_not_exist_is_refused_not_created(
    tmp_path, monkeypatch, capsys
):
    """A typo in --cwd created the whole path, a .quickcode inside it and a
    session file, then ran the agent in an empty directory."""
    _install(monkeypatch, FakeProvider([]))
    missing = tmp_path / "no" / "such" / "project"
    assert _exit_code(["--print", "--cwd", str(missing), "hello"]) == 2
    assert not (tmp_path / "no").exists()
    assert "no such directory" in capsys.readouterr().err


def test_a_file_is_not_a_project_directory(tmp_path, monkeypatch, capsys):
    _install(monkeypatch, FakeProvider([]))
    target = tmp_path / "notes.txt"
    target.write_text("x", encoding="utf-8")
    assert _exit_code(["--print", "--cwd", str(target), "hello"]) == 2


def test_continue_with_nothing_to_continue_says_so(tmp_path, monkeypatch, capsys):
    _install(monkeypatch, FakeProvider([[TextDelta("fresh"), TurnDone("stop")]]))
    cli.main(_headless(tmp_path, "--continue", "hello"))
    out, err = capsys.readouterr()
    assert out.strip() == "fresh"
    assert "no earlier session" in err


@pytest.mark.parametrize("port", ["0", "-1", "65536", "http"])
def test_a_port_outside_the_range_is_an_argument_error(port, capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli._build_parser().parse_args(["--port", port])
    assert excinfo.value.code == 2


def test_a_port_in_range_is_accepted():
    assert cli._build_parser().parse_args(["--port", "8643"]).port == 8643


# ----------------------------------------------------------------------- yolo


def test_yolo_mode_needs_the_yolo_flag_in_a_headless_run_too(tmp_path, monkeypatch, capsys):
    """The app refuses yolo unless it was armed (--yolo, or Settings). The
    headless path took `--mode yolo` at its word and ran every tool unasked."""
    _install(monkeypatch, FakeProvider([]))
    argv = ["--print", "--cwd", str(tmp_path), "--mode", "yolo", "hello"]
    assert _exit_code(argv) == 2
    assert "--yolo" in capsys.readouterr().err


def test_yolo_mode_with_the_flag_is_yolo(tmp_path, monkeypatch, capsys):
    built: dict = {}
    _install(monkeypatch, FakeProvider([[TextDelta("ok"), TurnDone("stop")]]), built)
    cli.main(["--print", "--cwd", str(tmp_path), "--mode", "yolo", "--yolo", "hello"])
    assert built["agent"].permissions.mode is Mode.yolo


def test_a_profile_asking_for_yolo_is_held_to_ask_without_the_flag(
    tmp_path, monkeypatch, capsys
):
    """Same rule the server applies to a profile (manager.apply_profile): the
    profile does not arm yolo, the app does; say so rather than go silent."""
    from quickcode.core import profiles

    monkeypatch.setattr(profiles, "effective",
                        lambda cwd, rules, **kw: (Mode.yolo, rules, None))
    built: dict = {}
    _install(monkeypatch, FakeProvider([[TextDelta("ok"), TurnDone("stop")]]), built)
    cli.main(["--print", "--cwd", str(tmp_path), "hello"])
    assert built["agent"].permissions.mode is Mode.ask
    assert "yolo" in capsys.readouterr().err
