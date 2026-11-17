"""
TP-Link Tapo smart plug control, carried over from tuhanKami.

Nothing in this installation calls it. main.py keeps the original call sites as
commented lines (`# plugs.entity_travel(...)`) at the points in the ritual where
tuhanKami drove its fans and lights, so the staging can be switched back on by
uncommenting them and the import at the top of main.py.

To use it:
  1. pip install python-kasa
  2. fill in TPLINK_* in .env
  3. uncomment `import plugs` and the calls in main.py

Plug 1 was the one that pulsed with the entity's attention (on while it listened
and transcribed, off while it spoke); plug 2 held the ambient state between
sessions.
"""

from __future__ import annotations

import asyncio
import os


def entity_travel(on: bool, device: int) -> None:
    # Toggle a TP-Link Tapo smart plug on or off. device is the plug number (1 or 2).
    async def run():
        from kasa import Discover

        dev = await Discover.discover_single(
            os.environ["TPLINK_HOST-" + str(device)],
            username=os.environ["TPLINK_USERNAME"],
            password=os.environ["TPLINK_PASSWORD"],
        )
        if on:
            await dev.turn_on()
        else:
            await dev.turn_off()
        await dev.update()
        await dev.disconnect()

    try:
        asyncio.run(run())
    except Exception as e:
        # A missing plug must never interrupt a session in progress.
        print(f"  [plug {device} unreachable: {e}]")
