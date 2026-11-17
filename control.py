from __future__ import annotations

import select
import sys
import termios
import threading
import tty
from contextlib import contextmanager
from typing import Iterator

# Ritual stages. run_ritual() keeps Session.stage current so the key loop knows
# what a given key should mean right now.
IDLE = "idle"
PRAYER = "prayer"
DIALOGUE = "dialogue"
FAREWELL = "farewell"


class Session:
    """
    The one object shared between the key loop and the ritual thread.

    Replaces tuhanKami's ClapRitual: there is no microphone-based clap detector
    any more, so every discrete signal arrives as a keypress (and later, as a GPIO
    button — see below). The ritual thread only ever reads these events; the key
    loop only ever sets them.
    """

    def __init__(self) -> None:
        self.stage = IDLE
        self.stop = threading.Event()    # 'o' — finish the prayer recording
        self.end = threading.Event()     # 'e' — end the session, ask for a farewell
        self.abort = threading.Event()   # 'i'/'p' — drop everything, back to idle
        self.paused = threading.Event()  # 'o' held — discard the take in progress

    def reset(self) -> None:
        for event in (self.stop, self.end, self.abort, self.paused):
            event.clear()
        self.stage = IDLE


class RitualAborted(Exception):
    # Raised to cleanly exit the ritual when the operator presses i/p.
    pass


def check(session: Session) -> None:
    # Raise RitualAborted immediately if the operator has signalled an interrupt.
    if session.abort.is_set():
        raise RitualAborted


def read_key(timeout: float = 0.1) -> str | None:
    # Non-blocking single-keypress read. Returns the character or None if nothing
    # was pressed. Requires raw_keyboard() to be active.
    if select.select([sys.stdin], [], [], timeout)[0]:
        return sys.stdin.read(1)
    return None


@contextmanager
def raw_keyboard() -> Iterator[None]:
    # cbreak mode so keys register immediately instead of waiting for Enter.
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


# ── GPIO buttons ──────────────────────────────────────────────────────────────
# The installation is meant to run on physical buttons; the keyboard is only there
# so the piece can be driven over SSH before anything is wired. To switch over:
# wire a button between GPIO 17 and GND (and optionally a second between GPIO 27
# and GND), uncomment this block, and call attach_buttons(session) in main() right
# after the Session is created. The key loop can stay as it is — both paths set
# the same events, so they work side by side.
#
# BUTTON_ACT_PIN = 17   # short press: begin prayer, then end prayer / pause a take
# BUTTON_END_PIN = 27   # short press: end the session (omit for a single button)
# LONG_PRESS_SECS = 1.5
#
# def attach_buttons(session: Session) -> None:
#     from gpiozero import Button
#
#     act = Button(BUTTON_ACT_PIN, pull_up=True, bounce_time=0.05, hold_time=LONG_PRESS_SECS)
#
#     def on_release() -> None:
#         # A held button is an abort; a tap advances the stage.
#         if act.held:
#             return
#         if session.stage == PRAYER:
#             session.stop.set()
#         elif session.stage == IDLE:
#             session.start_requested.set()   # add this Event to Session if you use it
#
#     def on_hold() -> None:
#         session.abort.set()
#         session.stop.set()
#
#     act.when_released = on_release
#     act.when_held = on_hold
#
#     end = Button(BUTTON_END_PIN, pull_up=True, bounce_time=0.05)
#     end.when_pressed = lambda: session.end.set()
#
#     # Keep a reference or gpiozero will garbage-collect the handlers.
#     session._buttons = (act, end)
