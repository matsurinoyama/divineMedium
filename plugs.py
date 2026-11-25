"""
The god's presence in the room, carried over from tuhanKami: two TP-Link Tapo
smart plugs, one driving the fans and one the lights.

  The fans are its travel: they blow while it draws near (the voice being
  cloned from the prayer) and while it departs (the farewell).
  The lights are the world without it: on between sessions, off once it has
  arrived, on again after the last bell.

Plugs are switched in order on one background thread, so the ritual never waits
for a Tapo handshake (a second or two). An unset address or a plug that cannot be
reached is skipped with a message — it never interrupts a session.

  TPLINK_FAN, TPLINK_LIGHT        the plugs' IP addresses
  TPLINK_USERNAME, TPLINK_PASSWORD  the TP-Link (Tapo) account
"""

from __future__ import annotations

import asyncio
import os
import queue
import threading

FAN = "fan"
LIGHT = "light"
HOSTS = {FAN: os.getenv("TPLINK_FAN"), LIGHT: os.getenv("TPLINK_LIGHT")}

_outbox: queue.Queue[tuple[str, bool]] = queue.Queue()
_worker: threading.Thread | None = None


async def _switch(name: str, on: bool) -> None:
    from kasa import Discover

    dev = await Discover.discover_single(
        HOSTS[name],
        username=os.environ["TPLINK_USERNAME"],
        password=os.environ["TPLINK_PASSWORD"],
    )
    try:
        await (dev.turn_on() if on else dev.turn_off())
        await dev.update()
    finally:
        await dev.disconnect()


def _run() -> None:
    while True:
        name, on = _outbox.get()
        try:
            asyncio.run(_switch(name, on))
            print(f"  [{name} {'on' if on else 'off'}]")
        except Exception as e:
            print(f"  [{name} plug unreachable: {type(e).__name__}: {e}]")


def _set(name: str, on: bool) -> None:
    global _worker
    if not HOSTS[name]:
        return
    if _worker is None:
        _worker = threading.Thread(target=_run, daemon=True)
        _worker.start()
    _outbox.put((name, on))


def fans(on: bool) -> None:
    _set(FAN, on)


def lights(on: bool) -> None:
    _set(LIGHT, on)


def idle() -> None:
    # Between sessions: still air, lights on.
    fans(False)
    lights(True)
