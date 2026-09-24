"""The terminal panel's pty, driven for real on POSIX.

``test_terminal_panel.py`` swaps the shell for a Python stand-in and asserts the
socket's lifecycle. This file asserts what only a real pseudo-terminal can
show: that ``Ctrl+C`` reaches the foreground program, that closing the panel
ends every job the shell started, that a program which stops reading its input
cannot stall the caller, and that output nobody has consumed is not read at
all. Each of these was broken while the stand-in tests stayed green.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest

from quickcode.pty import interactive as interactive_mod
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


@pytest.mark.skipif(BASH is None or shutil.which("setsid") is None,
                    reason="bash and setsid are needed")
def test_a_process_that_left_the_session_does_not_keep_the_reader_alive() -> None:
    """``setsid`` is a deliberate escape, and surviving the close is its right.

    Holding the pty open is not: the reader sat in ``read`` on a descriptor
    ``close`` had already released, which is a thread leaked per terminal and,
    once the number is reused, a read from somebody else's file.
    """
    screen = Screen()
    pty = _bash()
    before = {t.ident for t in threading.enumerate()}
    pty.start(screen.on_output, screen.on_exit)
    ours = [t for t in threading.enumerate() if t.ident not in before]
    escaped = None
    try:
        screen.wait_for("PROMPT$ ")
        pty.write("setsid sleep 1000 & echo escaped=$!\r")
        assert wait_until(lambda: re.search(r"escaped=\d+\r?\n", screen.text) is not None)
        escaped = int(re.search(r"escaped=(\d+)", screen.text).group(1))
    finally:
        pty.close()
    try:
        assert wait_until(lambda: not any(t.is_alive() for t in ours), timeout_s=5), (
            "a pty thread outlived close()"
        )
    finally:
        if escaped and _alive(escaped):
            os.kill(escaped, 9)


# ------------------------------------------------------------------- input


NOT_READING = """\
import sys, time, tty
tty.setraw(0)
print("RAW", flush=True)
time.sleep(float(sys.argv[1]) if len(sys.argv) > 1 else 60)
"""


def test_typing_at_a_program_that_is_not_reading_never_blocks_the_caller(tmp_path) -> None:
    """A raw-mode program that stops reading fills the pty's input buffer in
    about 8 KB. ``write`` was a blocking ``os.write`` called straight from the
    event loop, so one paste into a busy ``vim`` froze every project's server."""
    screen = Screen()
    pty = InteractivePty(_python(tmp_path, "raw.py", NOT_READING))
    pty.start(screen.on_output, screen.on_exit)
    finished = threading.Event()

    def type_a_lot() -> None:
        for _ in range(64):
            pty.write("x" * 4096)
        finished.set()

    try:
        screen.wait_for("RAW")
        threading.Thread(target=type_a_lot, daemon=True).start()
        assert finished.wait(5), "write() blocked on a program that is not reading"
    finally:
        pty.close()


def test_input_nobody_can_deliver_is_bounded(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(interactive_mod, "MAX_PENDING_INPUT", 64 * 1024)
    screen = Screen()
    pty = InteractivePty(_python(tmp_path, "raw.py", NOT_READING))
    pty.start(screen.on_output, screen.on_exit)
    try:
        screen.wait_for("RAW")
        accepted = [pty.write("y" * 4096) for _ in range(200)]
        assert accepted[0] is True
        assert accepted[-1] is False, "input kept queueing past its limit"
    finally:
        pty.close()


# ------------------------------------------------------------------ output


FLOOD = """\
import sys
for i in range(int(sys.argv[1])):
    sys.stdout.write("line %07d\\n" % i)
sys.stdout.flush()
print("DONE", flush=True)
"""


def test_output_is_not_read_faster_than_it_is_acknowledged(tmp_path) -> None:
    """Backpressure rather than loss.

    The socket used to keep the newest megabyte and silently drop the rest, so
    ``cat`` of a large log arrived with a hole in it while the reader spun a
    core decoding bytes nobody would see. A window of unacknowledged output
    makes the program wait instead, the way a real terminal does.
    """
    window = 64 * 1024
    lines = 200_000   # ~2.6 MB: far past the window, and past the pty's own buffer
    received: list[str] = []
    total = [0]
    lock = threading.Lock()

    def on_output(text: str) -> None:
        with lock:
            received.append(text)
            total[0] += len(text)

    argv = _python(tmp_path, "flood.py", FLOOD) + [str(lines)]
    pty = InteractivePty(argv, window=window)
    pty.start(on_output, lambda _code: None)
    try:
        assert wait_until(lambda: total[0] > 0)
        time.sleep(0.5)
        stalled_at = total[0]
        assert stalled_at <= window + interactive_mod.READ_SIZE, (
            f"{stalled_at} chars were read with only {window} allowed in flight"
        )
        acked = 0

        def ack_everything() -> bool:
            nonlocal acked
            with lock:
                seen = total[0]
            if seen > acked:
                pty.ack(seen - acked)
                acked = seen
            return "DONE" in "".join(received[-3:])

        assert wait_until(ack_everything, timeout_s=60), "the flood never finished"
    finally:
        pty.close()
    text = "".join(received).replace("\r\n", "\n")
    numbers = re.findall(r"line (\d{7})\n", text)
    assert len(numbers) == lines, "output was lost on the way"
    assert numbers[-1] == f"{lines - 1:07d}"


def test_without_a_window_output_streams_unthrottled(tmp_path) -> None:
    """The window is opt-in; a caller that never acks is not stalled."""
    screen = Screen()
    argv = _python(tmp_path, "flood.py", FLOOD) + ["20000"]
    pty = InteractivePty(argv)
    pty.start(screen.on_output, screen.on_exit)
    try:
        screen.wait_for("DONE", timeout_s=30)
    finally:
        pty.close()
