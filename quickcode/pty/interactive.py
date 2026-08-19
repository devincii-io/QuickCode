"""One long-lived pseudo-terminal with a human on the other end.

``PtySession`` next door runs *one command to completion*: spawn, accumulate
into a scrollback ring, wait for exit, return everything at once. This is the
opposite shape — a shell that stays up for as long as the panel is open,
streams as it goes, and takes keystrokes back.

**Why a sibling class rather than a mode of ``PtySession``.** The two share
almost no body. The batch one owns a deadline, a bounded ring and a
drain-before-close dance; none of those mean anything here (there is no
deadline, the browser holds the scrollback, and "drain" is simply what the
reader does forever). Threading an ``interactive=True`` flag through it would
have produced one function with two disjoint halves. What the two *do* share is
process-tree kill, and that is imported rather than copied: ``_kill_tree``
stays the single place that knows how to end a shell and its children on each
platform.

**Why a PTY here at all**, when ``tools/bash.py`` deliberately stopped using
one on Windows: that decision was measured per *command* — ConPTY costs a flat
~3 s to tear down, so forty agent commands meant two minutes of waiting. Here
the cost is paid once, when the panel opens, and what the tty buys is the whole
feature: a prompt, line editing, colour, ``Ctrl+C``, and programs that page or
redraw in place. A one-shot command has none of those to lose; an interactive
shell is nothing without them.

Text, not bytes, crosses this boundary in both directions. The batch session
keeps bytes on the hot path because the caller decodes once at the end; here
there is no end, so the decode has to be incremental anyway (a UTF-8 sequence
split across two reads must not become two replacement characters), and the
transport above is a JSON WebSocket that carries ``str``.
"""

from __future__ import annotations

import codecs
import os
import sys
import threading
import time
from collections.abc import Callable

from quickcode import subproc
from quickcode.pty.session import IS_WINDOWS, PtyError, _kill_tree

READ_SIZE = 65536
POLL_INTERVAL = 0.05  # watcher poll granularity (s)

# The three seconds, and where they went.
#
# ``tools/bash.py`` measured ConPTY adding a flat ~3.0 s to every command and
# concluded the pseudo-console was slow to tear down. It is not: measured here
# on Windows 11 26200, the *spawn* costs 30 ms and the child's first byte
# arrives 3.09 s later, after which streaming is exact (0.5 s writes read back
# at 0.5 s intervals). The pause is a handshake nobody was completing —
# ConPTY opens by sending Primary Device Attributes (``ESC [ c``), asking the
# terminal what it is, and waits ~3 s for a reply before giving up on it.
#
# We are the terminal, so we answer. Replying "VT100, no options" the moment
# the query appears takes the same run from 3.09 s to 0.16 s. The reply is sent
# on *seeing* the query rather than blindly at startup, because an unsolicited
# escape sequence in the input buffer is something an interactive shell would
# echo at its own prompt.
DA1_QUERY = "\x1b[c"
DA1_ANSWER = "\x1b[?1;0c"

OnOutput = Callable[[str], None]
OnExit = Callable[[int | None], None]


class InteractivePty:
    """A pseudo-terminal that stays open, streams output and accepts input.

    ``start`` spawns and returns immediately; ``on_output`` is then called from
    a reader thread for every chunk that arrives, and ``on_exit`` exactly once
    when the child dies. Both callbacks run off the main thread — the caller is
    responsible for getting them back onto whatever loop it lives on.
    """

    def __init__(
        self,
        argv: list[str],
        cwd: str | os.PathLike[str] | None = None,
        env: dict[str, str] | None = None,
        *,
        dimensions: tuple[int, int] = (24, 80),
    ) -> None:
        if not argv:
            raise ValueError("argv must be a non-empty list")
        self.argv = [str(a) for a in argv]
        self.cwd = str(cwd) if cwd is not None else None
        self.env = env
        self.rows, self.cols = dimensions
        self.pid: int | None = None
        self._closed = threading.Event()
        self._proc = None  # winpty.PtyProcess | subprocess.Popen
        self._master_fd: int | None = None
        self._write_lock = threading.Lock()

    # ----------------------------------------------------------------- start
    def start(self, on_output: OnOutput, on_exit: OnExit) -> None:
        if IS_WINDOWS:
            self._start_windows(on_output, on_exit)
        else:
            self._start_posix(on_output, on_exit)

    def _start_windows(self, on_output: OnOutput, on_exit: OnExit) -> None:
        try:
            import winpty
        except Exception as exc:  # noqa: BLE001 - any import failure -> caller falls back
            raise PtyError(f"pywinpty unavailable: {exc}") from exc
        try:
            proc = winpty.PtyProcess.spawn(
                self.argv,
                cwd=self.cwd,
                env=self.env,
                dimensions=(self.rows, self.cols),
            )
        except Exception as exc:  # noqa: BLE001 - FileNotFound, winpty errors, ...
            raise PtyError(f"ConPTY spawn failed: {exc}") from exc

        self._proc = proc
        self.pid = proc.pid
        answered = [False]

        def reader() -> None:
            # winpty hands back ``str`` already, so there is nothing to decode
            # incrementally on this platform.
            try:
                while not self._closed.is_set():
                    text = proc.read(READ_SIZE)
                    if text:
                        if not answered[0] and DA1_QUERY in text:
                            answered[0] = True
                            self.write(DA1_ANSWER)
                        on_output(text)
            except EOFError:
                pass
            except Exception:  # noqa: BLE001 - pty closed under us on teardown
                pass

        self._spawn_threads(reader, lambda: proc.isalive(), lambda: proc.exitstatus, on_exit)

    def _start_posix(self, on_output: OnOutput, on_exit: OnExit) -> None:
        try:
            master_fd, slave_fd = os.openpty()
        except Exception as exc:  # noqa: BLE001
            raise PtyError(f"openpty failed: {exc}") from exc
        try:
            proc = subproc.popen(
                self.argv,
                cwd=self.cwd,
                env=self.env,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                start_new_session=True,  # own process group, so tree-kill works
                close_fds=True,
            )
        except Exception as exc:  # noqa: BLE001
            os.close(master_fd)
            os.close(slave_fd)
            raise PtyError(f"pty spawn failed: {exc}") from exc
        os.close(slave_fd)

        self._proc = proc
        self._master_fd = master_fd
        self.pid = proc.pid
        self.resize(self.rows, self.cols)

        decoder = codecs.getincrementaldecoder("utf-8")("replace")

        def reader() -> None:
            while not self._closed.is_set():
                try:
                    data = os.read(master_fd, READ_SIZE)
                except OSError:
                    break  # master closed / slave hung up
                if not data:
                    break
                text = decoder.decode(data)
                if text:
                    on_output(text)

        self._spawn_threads(
            reader, lambda: proc.poll() is None, lambda: proc.returncode, on_exit
        )

    def _spawn_threads(
        self,
        reader: Callable[[], None],
        alive: Callable[[], bool],
        code: Callable[[], int | None],
        on_exit: OnExit,
    ) -> None:
        """Reader and watcher, split for the same reason ``PtySession`` splits them.

        The pty's own EOF is not a reliable exit signal — on Windows/ConPTY it
        can lag several seconds behind the process actually dying — so the exit
        is watched on the process and the bytes are read on the pty.
        """

        def watcher() -> None:
            try:
                while alive():
                    time.sleep(POLL_INTERVAL)
            except Exception:  # noqa: BLE001
                pass
            status: int | None
            try:
                status = code()
            except Exception:  # noqa: BLE001
                status = None
            if not self._closed.is_set():
                on_exit(status)

        threading.Thread(target=reader, name="qc-pty-read", daemon=True).start()
        threading.Thread(target=watcher, name="qc-pty-watch", daemon=True).start()

    # ------------------------------------------------------------------- I/O
    def write(self, text: str) -> None:
        """Send keystrokes to the shell. Silently no-ops after close."""
        if not text or self._closed.is_set() or self._proc is None:
            return
        with self._write_lock:
            try:
                if IS_WINDOWS:
                    self._proc.write(text)
                elif self._master_fd is not None:
                    os.write(self._master_fd, text.encode("utf-8", "surrogateescape"))
            except Exception:  # noqa: BLE001 - the shell went away mid-keystroke
                pass

    def resize(self, rows: int, cols: int) -> None:
        """Tell the pty its new size, so full-screen programs redraw correctly."""
        rows = max(1, min(int(rows), 500))
        cols = max(1, min(int(cols), 1000))
        self.rows, self.cols = rows, cols
        if self._closed.is_set() or self._proc is None:
            return
        try:
            if IS_WINDOWS:
                self._proc.setwinsize(rows, cols)
            elif self._master_fd is not None:
                import fcntl
                import struct
                import termios

                fcntl.ioctl(
                    self._master_fd,
                    termios.TIOCSWINSZ,
                    struct.pack("HHHH", rows, cols, 0, 0),
                )
        except Exception:  # noqa: BLE001 - a resize is never worth an exception
            pass

    @property
    def alive(self) -> bool:
        if self._closed.is_set() or self._proc is None:
            return False
        try:
            return self._proc.isalive() if IS_WINDOWS else self._proc.poll() is None
        except Exception:  # noqa: BLE001
            return False

    def close(self) -> None:
        """Kill the shell and everything it started, then drop the pty.

        Idempotent, and safe to call from any thread — which matters, because
        the socket closing and the project closing are two different threads of
        control that both end up here.
        """
        if self._closed.is_set():
            return
        self._closed.set()
        _kill_tree(self.pid)
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                if IS_WINDOWS:
                    proc.close(force=True)
                else:
                    proc.wait(timeout=2)
            except Exception:  # noqa: BLE001
                pass
        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None


def interactive_shell_argv() -> list[str]:
    """The argv for a shell a person can sit in front of.

    Deliberately not ``-c``: ``tools/bash.py`` builds ``bash -lc "<command>"``
    because it has exactly one command to run and wants the process to exit
    afterwards. Here the shell *is* the session, so it is invoked the way a
    terminal emulator invokes it — a login, interactive shell that reads the
    user's profile and prints a prompt.

    Shell discovery is ``tools/bash.py``'s, so the panel and the agent land in
    the same shell and the user is not debugging two different environments.
    """
    if sys.platform.startswith("win"):
        # Imported here rather than at module scope: the tools package pulls in
        # pydantic and the tool registry, which a pty has no business needing.
        from quickcode.tools.bash import _find_git_bash

        found = _find_git_bash()
        if found:
            return [found, "-i", "-l"]
        # No Git Bash: PowerShell, minus the -NonInteractive the tool passes.
        return ["powershell", "-NoLogo", "-NoExit"]
    return ["/bin/bash", "-i", "-l"]
