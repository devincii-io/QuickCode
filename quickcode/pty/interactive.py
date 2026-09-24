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
have produced one function with two disjoint halves.

They also end differently. A one-shot ``bash -lc`` keeps everything in one
process group, so ``_kill_tree`` is enough there; an interactive shell has job
control, and ending it means ending its whole session (``pty.teardown``).

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

**Nothing here blocks the caller.** ``write`` queues and ``resize`` is one
ioctl; the pty's own thread moves the bytes (``pty.flow`` says why each
direction is bounded). On POSIX that thread is a single non-blocking select
loop that also owns the master descriptor: it is the only thing that reads,
writes or closes it, so a descriptor number is never used after it was freed
and handed to somebody else.
"""

from __future__ import annotations

import codecs
import os
import selectors
import threading
import time
from collections.abc import Callable

from quickcode import subproc
from quickcode.pty.flow import INTERRUPT, InputQueue, OutputWindow
from quickcode.pty.session import IS_WINDOWS, PtyError
from quickcode.pty.teardown import end_session

if not IS_WINDOWS:
    import fcntl
    import struct
    import termios

READ_SIZE = 65536
POLL_INTERVAL = 0.05  # watcher poll granularity (s)
# Keystrokes a program has not accepted yet. Past this a write is refused: a
# megabyte of typing is not something a person produces while a program sits
# there not reading it.
MAX_PENDING_INPUT = 1 << 20

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
    the pty's thread for every chunk that arrives, and ``on_exit`` exactly once
    when the child dies. Both callbacks run off the main thread — the caller is
    responsible for getting them back onto whatever loop it lives on.

    ``window`` turns on output flow control: at most that many characters are
    read ahead of the caller's ``ack``. Without it output is read as fast as
    the program writes it.
    """

    def __init__(
        self,
        argv: list[str],
        cwd: str | os.PathLike[str] | None = None,
        env: dict[str, str] | None = None,
        *,
        dimensions: tuple[int, int] = (24, 80),
        window: int | None = None,
    ) -> None:
        if not argv:
            raise ValueError("argv must be a non-empty list")
        self.argv = [str(a) for a in argv]
        self.cwd = str(cwd) if cwd is not None else None
        self.env = env
        self.rows, self.cols = _clamp_size(*dimensions)
        self.pid: int | None = None
        self.output_done = threading.Event()   # the pty has nothing more to say
        self._closed = threading.Event()
        self._proc = None  # winpty.PtyProcess | subprocess.Popen
        self._input: InputQueue = InputQueue(MAX_PENDING_INPUT)
        self._window = OutputWindow(window)
        # Guards the descriptors below against the I/O thread closing them.
        self._fd_lock = threading.Lock()
        self._master_fd: int | None = None
        self._wake_w: int | None = None

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
        self._refuse_if_closed(proc)
        answered = [False]

        def reader() -> None:
            # winpty hands back ``str`` already, so there is nothing to decode
            # incrementally on this platform.
            try:
                while not self._closed.is_set():
                    self._window.wait_for_room(self._closed, POLL_INTERVAL)
                    if self._closed.is_set():
                        break
                    text = proc.read(READ_SIZE)
                    if text:
                        if not answered[0] and DA1_QUERY in text:
                            answered[0] = True
                            self.write(DA1_ANSWER)
                        self._window.sent(len(text))
                        on_output(text)
            except EOFError:
                pass
            except Exception:  # noqa: BLE001 - pty closed under us on teardown
                pass
            finally:
                self.output_done.set()

        def writer() -> None:
            while not self._closed.is_set():
                chunk = self._input.get(POLL_INTERVAL)
                if chunk is None:
                    continue
                try:
                    proc.write(chunk)
                except Exception:  # noqa: BLE001 - the shell went away mid-keystroke
                    return

        threading.Thread(target=writer, name="qc-pty-write", daemon=True).start()
        self._spawn_threads(reader, lambda: proc.isalive(), lambda: proc.exitstatus, on_exit)

    def _start_posix(self, on_output: OnOutput, on_exit: OnExit) -> None:
        try:
            master_fd, slave_fd = os.openpty()
        except Exception as exc:  # noqa: BLE001
            raise PtyError(f"openpty failed: {exc}") from exc
        fds = [master_fd, slave_fd]
        try:
            wake_r, wake_w = os.pipe()
            fds += [wake_r, wake_w]
            # Sized before the child exists, so its first look at the
            # terminal (bash's checkwinsize, a banner that centres itself)
            # sees the real geometry rather than 0x0.
            _set_winsize(master_fd, self.rows, self.cols)
            proc = subproc.popen(
                self.argv,
                cwd=self.cwd,
                env=self.env,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                start_new_session=True,  # its own session: see pty.teardown
                close_fds=True,
                preexec_fn=_take_controlling_tty,
            )
        except Exception as exc:  # noqa: BLE001
            for fd in fds:
                _close_quietly(fd)
            raise PtyError(f"pty spawn failed: {exc}") from exc
        os.close(slave_fd)
        for fd in (master_fd, wake_r, wake_w):
            os.set_blocking(fd, False)

        self._proc = proc
        self.pid = proc.pid
        self._refuse_if_closed(proc, (master_fd, wake_r, wake_w))
        with self._fd_lock:
            self._master_fd = master_fd
            self._wake_w = wake_w

        self._spawn_threads(
            lambda: self._pump_posix(master_fd, wake_r, on_output),
            lambda: proc.poll() is None,
            lambda: proc.returncode,
            on_exit,
        )

    def _refuse_if_closed(self, proc, fds: tuple[int, ...] = ()) -> None:
        """End a shell whose terminal was closed while it was being spawned.

        The spawn runs in a worker thread, and the socket (or the server) can
        go in the meantime. ``close`` then found no pid to kill, so the shell
        that appeared a moment later would have belonged to nobody. Whichever
        of the two sees the other second does the killing.
        """
        if not self._closed.is_set():
            return
        end_session(self.pid, leader_alive=True)
        try:
            if IS_WINDOWS:
                proc.close(force=True)
            else:
                proc.wait(timeout=2)
        except Exception:  # noqa: BLE001
            pass
        for fd in fds:
            _close_quietly(fd)
        raise PtyError("the terminal was closed while its shell was starting")

    def _pump_posix(self, master_fd: int, wake_r: int, on_output: OnOutput) -> None:
        """The one thread that touches the master: read, write, then close it."""
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        sel = selectors.DefaultSelector()
        try:
            sel.register(wake_r, selectors.EVENT_READ)
            registered = 0
            while not self._closed.is_set():
                want = 0
                if self._window.has_room():
                    want |= selectors.EVENT_READ
                if len(self._input):
                    want |= selectors.EVENT_WRITE
                if want != registered:
                    if not registered:
                        sel.register(master_fd, want)
                    elif not want:
                        sel.unregister(master_fd)
                    else:
                        sel.modify(master_fd, want)
                    registered = want
                for key, mask in sel.select():
                    if key.fd == wake_r:
                        _drain_pipe(wake_r)
                        continue
                    if mask & selectors.EVENT_WRITE and not self._flush_input(master_fd):
                        return
                    if mask & selectors.EVENT_READ:
                        try:
                            data = os.read(master_fd, READ_SIZE)
                        except BlockingIOError:
                            continue
                        except OSError:
                            return  # EIO: every holder of the slave side is gone
                        if not data:
                            return
                        text = decoder.decode(data)
                        if text:
                            self._window.sent(len(text))
                            on_output(text)
        finally:
            tail = decoder.decode(b"", final=True)
            if tail and not self._closed.is_set():
                on_output(tail)
            sel.close()
            with self._fd_lock:
                for fd in (master_fd, wake_r, self._wake_w):
                    if fd is not None:
                        _close_quietly(fd)
                self._master_fd = None
                self._wake_w = None
            self.output_done.set()

    def _flush_input(self, master_fd: int) -> bool:
        """Write what the pty will take right now. False once it takes nothing ever."""
        while (chunk := self._input.peek()) is not None:
            try:
                written = os.write(master_fd, chunk)
            except BlockingIOError:
                return True
            except OSError:
                return False
            self._input.consume(written)
        return True

    def _spawn_threads(
        self,
        pump: Callable[[], None],
        alive: Callable[[], bool],
        code: Callable[[], int | None],
        on_exit: OnExit,
    ) -> None:
        """I/O and watcher, split for the same reason ``PtySession`` splits them.

        The pty's own EOF is not a reliable exit signal — on Windows/ConPTY it
        can lag several seconds behind the process actually dying, and a
        background job holding the pty open delays it forever — so the exit is
        watched on the process and the bytes are moved on the pty.
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

        threading.Thread(target=pump, name="qc-pty-io", daemon=True).start()
        threading.Thread(target=watcher, name="qc-pty-watch", daemon=True).start()

    # ------------------------------------------------------------------- I/O
    def write(self, text: str) -> bool:
        """Queue keystrokes for the shell. Never blocks; False if refused.

        Refused means closed, or more input already waiting than a program
        that is reading would ever leave behind (``MAX_PENDING_INPUT``).
        """
        if not text or self._closed.is_set() or self._proc is None:
            return False
        chunk: str | bytes = text if IS_WINDOWS else _encode(text)
        if not self._input.put(chunk, interrupt=INTERRUPT in text):
            return False
        self._wake()
        return True

    def ack(self, count: int) -> None:
        """The consumer has dealt with ``count`` characters of output."""
        self._window.ack(count)
        self._wake()

    def resize(self, rows: int, cols: int) -> None:
        """Tell the pty its new size, so full-screen programs redraw correctly."""
        rows, cols = _clamp_size(rows, cols)
        self.rows, self.cols = rows, cols
        if self._closed.is_set() or self._proc is None:
            return
        try:
            if IS_WINDOWS:
                self._proc.setwinsize(rows, cols)
                return
            with self._fd_lock:
                if self._master_fd is not None:
                    _set_winsize(self._master_fd, rows, cols)
        except Exception:  # noqa: BLE001 - a resize is never worth an exception
            pass

    def _wake(self) -> None:
        with self._fd_lock:
            if self._wake_w is None:
                return
            try:
                os.write(self._wake_w, b"\0")
            except OSError:
                pass  # full: a wake-up is already pending, which is all this is

    @property
    def alive(self) -> bool:
        if self._closed.is_set() or self._proc is None:
            return False
        try:
            return self._proc.isalive() if IS_WINDOWS else self._proc.poll() is None
        except Exception:  # noqa: BLE001
            return False

    def close(self) -> None:
        """End the shell's whole session, then let go of the pty.

        Idempotent, and safe to call from any thread — which matters, because
        the socket closing and the project closing are two different threads of
        control that both end up here. It may block for a fraction of a second
        while the session is given a chance to hang up; call it off the loop.
        """
        if self._closed.is_set():
            return
        self._closed.set()
        proc = self._proc
        try:
            running = proc is not None and (
                proc.isalive() if IS_WINDOWS else proc.poll() is None
            )
        except Exception:  # noqa: BLE001
            running = False
        end_session(self.pid, leader_alive=running)
        self._input.close()
        self._window.release()
        self._wake()
        self._proc = None
        if proc is not None:
            try:
                if IS_WINDOWS:
                    proc.close(force=True)
                else:
                    proc.wait(timeout=2)
            except Exception:  # noqa: BLE001
                pass


def _clamp_size(rows: int, cols: int) -> tuple[int, int]:
    return max(1, min(int(rows), 500)), max(1, min(int(cols), 1000))


def _encode(text: str) -> bytes:
    try:
        return text.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:
        return text.encode("utf-8", "replace")


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _take_controlling_tty() -> None:
    """Make the pty the new session's controlling terminal (runs in the child).

    ``start_new_session`` leaves the child a session leader with no terminal
    at all, and without one the line discipline has nobody to deliver
    ``Ctrl+C`` to: ``\\x03`` became a printed ``^C`` and nothing stopped. bash
    happens to claim the tty itself; dash, a program run as the shell, and
    anything else that does not, got no signals and no job control.
    """
    try:
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    except OSError:
        pass


def _drain_pipe(fd: int) -> None:
    try:
        while os.read(fd, 4096):
            pass
    except OSError:
        pass


def _close_quietly(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass
