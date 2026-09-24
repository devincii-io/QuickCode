"""The terminal panel's pty, driven for real on POSIX.

``test_terminal_panel.py`` swaps the shell for a Python stand-in and asserts the
socket's lifecycle. This file asserts what only a real pseudo-terminal can
show: that ``Ctrl+C`` reaches the foreground program and that closing the
panel ends every job the shell started. Both were broken while the stand-in
tests stayed green.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import time
from pathlib import Path

import pytest

from quickcode.pty.interactive import InteractivePty
from tests.conftest import wait_until

pytestmark = pytest.mark.skipif(
    sys.platform.startswith("win"), reason="POSIX pty semantics (ConPTY has its own)"
)

BASH = shutil.which("bash")


class Screen:
    """Collects a pty's output; thread-safe enough for `in` checks."""

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.exit: list[int | None] = []

    def on_output(self, text: str) -> None:
        self.parts.append(text)

    def on_exit(self, code: int | None) -> None:
        self.exit.append(code)

    @property
    def text(self) -> str:
        return "".join(self.parts)

    def wait_for(self, needle: str, timeout_s: float = 10.0) -> str:
        assert wait_until(lambda: needle in self.text, timeout_s=timeout_s), (
            f"never saw {needle!r}; got {self.text[-600:]!r}"
        )
        return self.text


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        # A zombie still answers kill(0); it is dead for every purpose here.
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return True


def _python(tmp_path: Path, name: str, source: str) -> list[str]:
    script = tmp_path / name
    script.write_text(source, encoding="utf-8")
    return [sys.executable, "-u", str(script)]


def _bash(**env: str) -> InteractivePty:
    return InteractivePty(
        [BASH, "--norc", "--noprofile", "-i"],
        env={**os.environ, "PS1": "PROMPT$ ", **env},
    )


# ----------------------------------------------------------- the tty itself


INTERRUPTIBLE = """\
import sys, time
print("WAITING", flush=True)
try:
    time.sleep(60)
except KeyboardInterrupt:
    print("GOT-SIGINT", flush=True)
"""


def test_ctrl_c_interrupts_the_program_in_the_terminal(tmp_path) -> None:
    """``\\x03`` is only a signal if the pty is the program's controlling
    terminal. The shell was started in a new session and never given one, so
    the line discipline had nobody to send SIGINT to and printed ``^C`` into
    the void; bash happened to paper over it by acquiring the tty itself."""
    screen = Screen()
    pty = InteractivePty(_python(tmp_path, "sleeper.py", INTERRUPTIBLE))
    pty.start(screen.on_output, screen.on_exit)
    try:
        screen.wait_for("WAITING")
        pty.write("\x03")
        screen.wait_for("GOT-SIGINT")
    finally:
        pty.close()


@pytest.mark.skipif(shutil.which("dash") is None, reason="dash is not installed")
def test_a_shell_that_does_not_grab_the_tty_itself_still_gets_job_control() -> None:
    screen = Screen()
    pty = InteractivePty([shutil.which("dash"), "-i"], env={**os.environ, "PS1": "P$ "})
    pty.start(screen.on_output, screen.on_exit)
    try:
        screen.wait_for("P$ ")
        pty.write("sleep 30\n")
        time.sleep(0.3)
        pty.write("\x03")
        pty.write("echo back-$?\n")
        screen.wait_for("back-130")
        assert "job control turned off" not in screen.text
    finally:
        pty.close()


# ------------------------------------------------------------ ending it all


@pytest.mark.skipif(BASH is None, reason="bash is not installed")
def test_closing_the_terminal_ends_the_jobs_the_shell_started() -> None:
    """Closing the panel promises to kill the process tree. With job control a
    background job lives in a process group of its own, so killing the shell's
    group left ``sleep 1000 &`` running long after the window was gone."""
    screen = Screen()
    pty = _bash()
    pty.start(screen.on_output, screen.on_exit)
    bg = 0
    try:
        screen.wait_for("PROMPT$ ")
        pty.write("sleep 1000 & echo bgpid=$!\r")
        assert wait_until(lambda: re.search(r"bgpid=\d+\r?\n", screen.text) is not None)
        bg = int(re.search(r"bgpid=(\d+)", screen.text).group(1))
        assert _alive(bg)
    finally:
        pty.close()
    try:
        assert wait_until(lambda: not _alive(bg), timeout_s=5), "the background job survived"
    finally:
        if _alive(bg):
            os.kill(bg, 9)


