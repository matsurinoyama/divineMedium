"""
How the seeker's hand reaches the ritual: the PiSugar button, through a pipe,
and the keyboard, for whoever keeps the installation. Both speak the same letters.
"""

from __future__ import annotations

import os
import select
import sys
import termios
import threading
import tty
from collections.abc import Iterator
from contextlib import contextmanager

# The stages of the ritual. A press means what the stage says it means.
IDLE = "idle"
PRAYER = "prayer"
ARRIVAL = "arrival"     # the prayer given, the Entity drawing near: a press hastens it
DIALOGUE = "dialogue"
FAREWELL = "farewell"

# The pipe the PiSugar button speaks through (pisugar_button.sh). It lives beside
# the code, not in /tmp, where root may not write to a pipe the user owns.
BUTTON_FIFO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "button.fifo")
_button_fd: int | None = None


class Session:
    """
    What the hand and the ritual share. The ritual only reads these signs; the
    keeper of the keys only gives them.
    """

    def __init__(self) -> None:
        self.stage = IDLE
        self.tap = threading.Event()     # pressed twice in the prayer: it is ended
        self.end = threading.Event()     # pressed twice in the dialogue: the farewell
        self.abort = threading.Event()   # a long press: all is undone, back to the covenant
        self.hasten = threading.Event()  # pressed twice while the Entity draws near


class RitualBroken(Exception):
    pass


def abide(session: Session) -> None:
    # The ritual goes on, unless it has been broken.
    if session.abort.is_set():
        raise RitualBroken


def read_key(timeout: float = 0.1) -> str | None:
    # One letter from the keyboard or the button, or None. Needs raw_keyboard();
    # the button is heard only within button_pipe().
    sources = [sys.stdin] if _button_fd is None else [sys.stdin, _button_fd]
    ready = select.select(sources, [], [], timeout)[0]
    if _button_fd is not None and _button_fd in ready:
        data = os.read(_button_fd, 1)
        if data:
            return data.decode(errors="ignore")
    if sys.stdin in ready:
        return sys.stdin.read(1)
    return None


@contextmanager
def raw_keyboard() -> Iterator[None]:
    # Each key heard as it is struck, without waiting for Enter.
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


@contextmanager
def button_pipe() -> Iterator[None]:
    # Opened to read and write, so it always has a writer: no end-of-file between
    # presses, and the button's script never waits on it.
    global _button_fd
    if os.path.lexists(BUTTON_FIFO):
        os.remove(BUTTON_FIFO)
    os.mkfifo(BUTTON_FIFO, 0o600)
    _button_fd = os.open(BUTTON_FIFO, os.O_RDWR | os.O_NONBLOCK)
    try:
        yield
    finally:
        os.close(_button_fd)
        _button_fd = None
        os.remove(BUTTON_FIFO)
