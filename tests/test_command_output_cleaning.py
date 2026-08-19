"""What a command wrote, as a terminal would have shown it.

Two paths run commands (pty on POSIX, plain pipes on Windows) and they used to
clean their output differently: the pty path stripped ANSI, the subprocess path
did not. So on Windows, where pipes are the default, `pytest --color=yes` sent
escape bytes to the model and drew them raw in the transcript.

Both now share `_clean_output`, which also stops turning a progress bar into
several hundred lines.
"""

from __future__ import annotations

import pytest

from quickcode.tools.bash import _clean_output, _collapse_redraws

RED = "\x1b[31m"
RESET = "\x1b[0m"


# ---- colour ----


def test_colour_codes_do_not_reach_the_caller() -> None:
    assert _clean_output(f"{RED}FAILED{RESET} test_x".encode()) == "FAILED test_x"


@pytest.mark.parametrize("raw", [
    b"\x1b]0;a window title\x07done",           # OSC + BEL
    b"\x1b[1t\x1b[c\x1b[?1004h\x1b[?9001hdone",  # the ConPTY opening handshake
    b"\x1b[2J\x1b[Hdone",                        # clear screen, home cursor
    b"\x1b[38;2;255;0;0mdone\x1b[0m",            # truecolor
])
def test_no_escape_byte_survives(raw: bytes) -> None:
    assert _clean_output(raw) == "done"
    assert "\x1b" not in _clean_output(raw)


# ---- carriage returns ----


def test_a_progress_bar_is_the_line_it_finished_on() -> None:
    """It used to be one line per redraw. 300 bars, all but one obsolete."""
    bar = "".join(f"\r[{'#' * n:<10}] {n * 10}%" for n in range(11))
    got = _clean_output(bar.encode())

    assert got.count("\n") == 0
    assert got == "[##########] 100%"


def test_overwriting_leaves_what_the_terminal_leaves() -> None:
    """Not "take the last segment": a real terminal keeps the longer tail."""
    assert _collapse_redraws("100%\r5%") == "5%0%"


def test_crlf_is_a_line_break_not_a_redraw() -> None:
    assert _clean_output(b"one\r\ntwo\r\nthree") == "one\ntwo\nthree"


def test_redraws_are_collapsed_per_line() -> None:
    got = _clean_output(b"downloading\rdone       \nunpacking\rdone     ")
    assert got == "done       \ndone     "


def test_a_short_redraw_leaves_the_tail_of_the_longer_line() -> None:
    """`downloading` is 11 characters and `done` pads to 10, so the `g` stays
    on screen. Tools that redraw pad wide enough to cover; one that doesn't
    leaves this litter on a real terminal too, and we show what it left."""
    assert _collapse_redraws("downloading\rdone      ") == "done      g"


@pytest.mark.parametrize("text", ["", "plain", "a\nb\nc", "trailing\n"])
def test_output_without_redraws_is_untouched(text: str) -> None:
    assert _clean_output(text.encode()) == text
    assert _collapse_redraws(text) == text


def test_a_bare_cr_never_survives_as_a_control_character() -> None:
    assert "\r" not in _clean_output(b"a\rb\r\nc\r")


# ---- the two paths agree ----


def test_both_paths_clean_identically() -> None:
    """The bug was that they didn't. `_run_subprocess` decoded and stopped."""
    import inspect

    from quickcode.tools import bash

    source = inspect.getsource(bash)
    assert "decode_output(raw_out)" not in source, (
        "a command-output path is decoding without cleaning; "
        "both paths must go through _clean_output"
    )
