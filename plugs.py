"""
The Entity's presence in the room, through two Tapo plugs.

  The fans are its travel: they blow while it draws near and while it departs.
  The lights are the world without it: on between rituals, out once it has
  come, on again after the last bell.

Each plug is switched in order on a thread of its own, so the ritual never waits
on a handshake. A plug unnamed or out of reach is passed over, and said so.

  TPLINK_FAN, TPLINK_LIGHT          the plugs' addresses
  TPLINK_USERNAME, TPLINK_PASSWORD  the Tapo account
"""

from __future__ import annotations

import asyncio
import os
import queue
import threading
import time

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
            print(f"  [the {name} is out of reach: {type(e).__name__}: {e}]")
        finally:
            _outbox.task_done()


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


def at_rest() -> None:
    # The room as it waits between rituals: still air, lights on.
    fans(False)
    lights(True)


def flush(timeout: float = 10.0) -> None:
    # Every switch sent before the program ends: once Python begins to close, the
    # plugs are out of reach, and the room would stay as the ritual left it.
    deadline = time.monotonic() + timeout
    with _outbox.all_tasks_done:
        while _outbox.unfinished_tasks:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                print("  [the last switch was not heard]")
                return
            _outbox.all_tasks_done.wait(remaining)
