"""One pseudo-terminal per command invocation.

A ``PtySession`` spawns a single command (an argv list, e.g. a shell running
``-lc "<command>"``) inside a real pseudo-terminal and streams it to
completion. Programs see a tty, so they emit colors and take their
tty-interactive code paths; process-tree kill works on timeout.

Design (per ARCHITECTURE.md §PTY), scoped to a *run-to-completion* model
rather than a long-lived interactive shell:

* **reader** thread: reads bytes as they become available and accumulates
  them into a bounded scrollback ring (each ``read`` coalesces up to
  ``READ_SIZE`` bytes; the ring caps total retained bytes).
* **watcher** thread: waits on the *real* process (not the pty EOF, which on
  Windows/ConPTY can lag several seconds behind actual exit) and captures the
  exit code the moment the child dies.
* writes never block the caller (this run-to-completion model sends no input,
  so the writer degenerates to nothing, but the reader/watcher split is what
  keeps ``run`` responsive to real exit).

Bytes stay on the hot path; the caller decodes once at the boundary
(UTF-8 + ``surrogateescape``).

On Windows the backend is ConPTY via ``pywinpty`` (``winpty.PtyProcess``); on
POSIX it is the stdlib ``pty``/``os.openpty`` plus a process group. If the
backend cannot be imported or the spawn fails, ``PtyError`` is raised so
callers can fall back to a plain subprocess.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections import deque

from quickcode import subproc

IS_WINDOWS = sys.platform.startswith("win")

READ_SIZE = 65536  # ~64 KB per read; chunks stay <= 128 KB
MAX_SCROLLBACK = 16 * 1024 * 1024  # ring cap; oldest bytes dropped beyond this
POLL_INTERVAL = 0.02  # watcher poll granularity (s)
DRAIN_MAX_S = 5.0  # max time to let the reader drain buffered output after exit
KILL_GRACE_S = 3.0  # time to wait for the tree to die after a kill


class PtyError(RuntimeError):
    """Raised when a PTY backend is unavailable or a spawn fails.

    Callers should catch this and fall back to a plain subprocess.
    """


class PtySession:
    """A single command run inside its own pseudo-terminal.

    Construct with an ``argv`` list, an optional ``cwd`` and ``env``, then call
    :meth:`run`. The instance is single-use.
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
        self.dimensions = dimensions
        self.pid: int | None = None

    # ------------------------------------------------------------------ run
    def kill(self) -> None:
        """Kill the process tree from outside the thread running ``run``.

        ``run`` blocks in a worker thread, and cancelling the asyncio task that
        awaits it does not reach the child -- the command keeps going and the
        thread stays parked on it. Killing the tree is what actually ends both:
        the reader sees EOF, ``run`` returns, and the thread exits. Safe to
        call before the spawn, after the exit, or twice.
        """
        _kill_tree(getattr(self, "pid", None))

    def run(self, timeout_s: float) -> tuple[bytes, int | None, bool]:
        """Spawn, stream to completion (or timeout), and return.

        Returns ``(output_bytes, exit_code, timed_out)``. ``output_bytes`` is
        the raw terminal output (still contains ANSI/CR); decode once at the
        boundary. ``exit_code`` is ``None`` only if it could not be determined.
        On timeout the process tree is killed and ``timed_out`` is ``True``.
        """
        if IS_WINDOWS:
            return self._run_windows(timeout_s)
        return self._run_posix(timeout_s)

    # -------------------------------------------------------------- windows
    def _run_windows(self, timeout_s: float) -> tuple[bytes, int | None, bool]:
        try:
            import winpty
        except Exception as exc:  # noqa: BLE001 - any import failure -> fallback
            raise PtyError(f"pywinpty unavailable: {exc}") from exc

        try:
            proc = winpty.PtyProcess.spawn(
                self.argv,
                cwd=self.cwd,
                env=self.env,
                dimensions=self.dimensions,
            )
        except Exception as exc:  # noqa: BLE001 - FileNotFound, winpty errors, ...
            raise PtyError(f"ConPTY spawn failed: {exc}") from exc

        self.pid = proc.pid
        chunks: deque[bytes] = deque()
        total = [0]
        lock = threading.Lock()
        reader_done = threading.Event()

        def append(data: bytes) -> None:
            with lock:
                chunks.append(data)
                total[0] += len(data)
                while total[0] > MAX_SCROLLBACK and len(chunks) > 1:
                    total[0] -= len(chunks.popleft())

        def reader() -> None:
            try:
                while True:
                    # winpty returns str (UTF-8 decoded); re-encode so the ring
                    # holds bytes uniformly and the caller decodes once.
                    text = proc.read(READ_SIZE)
                    if text:
                        append(text.encode("utf-8", "surrogateescape"))
            except EOFError:
                pass
            except Exception:  # noqa: BLE001 - socket closed under us on teardown
                pass
            finally:
                reader_done.set()

        exited = threading.Event()
        exit_code: list[int | None] = [None]

        def watcher() -> None:
            try:
                while proc.isalive():
                    time.sleep(POLL_INTERVAL)
                exit_code[0] = proc.exitstatus
            finally:
                exited.set()

        threading.Thread(target=reader, daemon=True).start()
        threading.Thread(target=watcher, daemon=True).start()

        timed_out = self._wait_for_exit(exited, timeout_s)
        if timed_out:
            _kill_tree(self.pid)
            exited.wait(KILL_GRACE_S)
            if exit_code[0] is None:
                try:
                    exit_code[0] = proc.exitstatus
                except Exception:  # noqa: BLE001
                    exit_code[0] = None

        # Let the reader pull whatever is still buffered, then force EOF by
        # closing the pty (winpty's own EOF can lag ~8s behind real exit).
        self._drain(reader_done, total, lock)
        try:
            proc.close(force=True)
        except Exception:  # noqa: BLE001
            pass
        reader_done.wait(1.0)

        with lock:
            out = b"".join(chunks)
        return out, exit_code[0], timed_out

    # ---------------------------------------------------------------- posix
    def _run_posix(self, timeout_s: float) -> tuple[bytes, int | None, bool]:
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
                start_new_session=True,  # own process group for tree-kill
                close_fds=True,
            )
        except Exception as exc:  # noqa: BLE001
            os.close(master_fd)
            os.close(slave_fd)
            raise PtyError(f"pty spawn failed: {exc}") from exc

        os.close(slave_fd)
        self.pid = proc.pid

        chunks: deque[bytes] = deque()
        total = [0]
        lock = threading.Lock()
        reader_done = threading.Event()

        def reader() -> None:
            try:
                while True:
                    try:
                        data = os.read(master_fd, READ_SIZE)
                    except OSError:
                        break  # master closed / slave hung up
                    if not data:
                        break
                    with lock:
                        chunks.append(data)
                        total[0] += len(data)
                        while total[0] > MAX_SCROLLBACK and len(chunks) > 1:
                            total[0] -= len(chunks.popleft())
            finally:
                reader_done.set()

        exited = threading.Event()
        exit_code: list[int | None] = [None]

        def watcher() -> None:
            try:
                exit_code[0] = proc.wait()
            finally:
                exited.set()

        threading.Thread(target=reader, daemon=True).start()
        threading.Thread(target=watcher, daemon=True).start()

        timed_out = self._wait_for_exit(exited, timeout_s)
        if timed_out:
            _kill_tree(self.pid)
            exited.wait(KILL_GRACE_S)

        self._drain(reader_done, total, lock)
        try:
            os.close(master_fd)
        except OSError:
            pass
        reader_done.wait(1.0)

        with lock:
            out = b"".join(chunks)
        return out, exit_code[0], timed_out

    # -------------------------------------------------------------- helpers
    @staticmethod
    def _wait_for_exit(exited: threading.Event, timeout_s: float) -> bool:
        """Block until the child exits or the deadline passes. True = timed out."""
        deadline = time.monotonic() + max(timeout_s, 0.0)
        while True:
            if exited.wait(0.05):
                return False
            if time.monotonic() >= deadline:
                return True

    @staticmethod
    def _drain(reader_done: threading.Event, total: list[int], lock: threading.Lock) -> None:
        """Wait until buffered output stops growing (or DRAIN_MAX_S elapses)."""
        end = time.monotonic() + DRAIN_MAX_S
        last = -1
        stable = 0
        while time.monotonic() < end:
            if reader_done.is_set():
                return
            with lock:
                cur = total[0]
            if cur == last:
                stable += 1
                if stable >= 3:
                    return
            else:
                stable = 0
                last = cur
            time.sleep(0.05)


def _kill_tree(pid: int | None) -> None:
    """Kill the process and its whole subtree."""
    if pid is None:
        return
    if IS_WINDOWS:

        try:
            subproc.run(  # noqa: S607
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                capture_output=True,
                timeout=10,
            )
        except Exception:  # noqa: BLE001
            pass
    else:
        import signal

        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except Exception:  # noqa: BLE001
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:  # noqa: BLE001
                pass


def run_pty(
    argv: list[str],
    cwd: str | os.PathLike[str] | None,
    env: dict[str, str] | None,
    timeout_s: float,
) -> tuple[bytes, int | None, bool]:
    """Convenience wrapper: build a :class:`PtySession` and run it once."""
    return PtySession(argv, cwd=cwd, env=env).run(timeout_s)
