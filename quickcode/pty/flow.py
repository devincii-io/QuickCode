"""Flow control between an interactive pty and whoever is on the other end.

Two directions, two different shapes of the same problem: the two sides of a
terminal run at speeds nobody controls.

**Input** comes from a person, through the event loop, and goes to a program
that may not be reading. A raw-mode program that stops reading fills the pty's
input buffer in a few kilobytes, after which a write blocks — and the caller
is the server's event loop. So input is queued here and written by the pty's
own I/O thread, and the queue is bounded: past the limit a keystroke is
refused rather than held forever for a program that will never ask for it.

**Output** comes from a program that may write faster than the browser can
draw. Rather than reading everything and dropping what does not fit, the
reader stops once ``size`` characters are in flight and resumes as the
consumer acknowledges them. The program then blocks on its own write, which
is exactly what happens in a real terminal and costs nothing while it waits.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Generic, TypeVar

# ``bytes`` on POSIX (written to a file descriptor), ``str`` on Windows
# (handed to pywinpty, which encodes itself).
Chunk = TypeVar("Chunk", bytes, str)

# End of text: what Ctrl+C sends, and what the line discipline turns into
# SIGINT. See ``InputQueue.put``.
INTERRUPT = "\x03"


class InputQueue(Generic[Chunk]):
    """Keystrokes waiting for a program to accept them."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._items: deque[Chunk] = deque()
        self._size = 0
        self._cond = threading.Condition()
        self._closed = False

    def put(self, chunk: Chunk, *, interrupt: bool = False) -> bool:
        """Queue ``chunk``; False when it would pass the limit.

        An ``interrupt`` discards whatever the program has not yet taken, as
        the tty driver does with its own queue on SIGINT: someone pressing
        Ctrl+C at a program that stopped reading wants it stopped, not fed the
        paste that is still waiting behind it.
        """
        with self._cond:
            if self._closed:
                return False
            if interrupt:
                self._items.clear()
                self._size = 0
            if self._size + len(chunk) > self.limit:
                return False
            self._items.append(chunk)
            self._size += len(chunk)
            self._cond.notify_all()
            return True

    def __len__(self) -> int:
        with self._cond:
            return self._size

    def peek(self) -> Chunk | None:
        """The next chunk to write, without taking it (a write may be partial)."""
        with self._cond:
            return self._items[0] if self._items else None

    def consume(self, count: int) -> None:
        """Drop the first ``count`` units of the head chunk: that much was written."""
        with self._cond:
            if not self._items:
                return
            head = self._items[0]
            if count >= len(head):
                self._items.popleft()
            else:
                self._items[0] = head[count:]
            self._size -= min(count, len(head))

    def get(self, timeout: float) -> Chunk | None:
        """Take the next chunk, waiting up to ``timeout``; None if there is none."""
        with self._cond:
            if not self._items and not self._closed:
                self._cond.wait(timeout)
            if not self._items:
                return None
            chunk = self._items.popleft()
            self._size -= len(chunk)
            return chunk

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._items.clear()
            self._size = 0
            self._cond.notify_all()


class OutputWindow:
    """How much output may be read before the consumer has caught up.

    ``size=None`` means no limit — a caller that consumes synchronously (a
    test, a log) has nothing to acknowledge and must not be stalled.
    """

    def __init__(self, size: int | None) -> None:
        self.size = size
        self._in_flight = 0
        self._cond = threading.Condition()

    def has_room(self) -> bool:
        with self._cond:
            return self.size is None or self._in_flight < self.size

    def sent(self, count: int) -> None:
        if self.size is None:
            return
        with self._cond:
            self._in_flight += count

    def ack(self, count: int) -> None:
        if self.size is None:
            return
        with self._cond:
            self._in_flight = max(0, self._in_flight - count)
            self._cond.notify_all()

    def wait_for_room(self, stop: threading.Event, poll_s: float) -> None:
        """Block until there is room or ``stop`` is set."""
        with self._cond:
            while not stop.is_set() and not (
                self.size is None or self._in_flight < self.size
            ):
                self._cond.wait(poll_s)

    def release(self) -> None:
        """Wake every waiter; used on close so nothing sleeps on a dead pty."""
        with self._cond:
            self._cond.notify_all()
