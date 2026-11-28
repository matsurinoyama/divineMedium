"""
The Entity's words carried to the Mac (echo_server/), so it may answer them with
echoes on the speakers around the room.

Each message goes in order, on a thread of its own, and never holds the ritual.
Without ECHO_HOST, or with the Mac out of reach, the Entity simply speaks alone.
"""

from __future__ import annotations

import os
import queue
import threading
import time

import httpx

HOST = os.getenv("ECHO_HOST")   # e.g. "mymac.local:8765"
TIMEOUT = 3.0
TRIES = 3          # ~1 s and 2 s apart: long enough for the Wi-Fi to blink and return

_outbox: queue.Queue[tuple[str, dict, threading.Event | None]] = queue.Queue()
_worker: threading.Thread | None = None
_unreachable = False   # said once, not with every message
# This ritual's /start, kept until the Mac has it: without it the Mac ignores all
# else, so it is sent again before the next message, and the echo joins late
# rather than never.
_pending_start: dict | None = None


def _post(client: httpx.Client, path: str, body: dict) -> bool:
    global _unreachable
    for attempt in range(1, TRIES + 1):
        try:
            client.post(path, json=body).raise_for_status()
            if _unreachable:
                print("  [the echo is reached again]")
            _unreachable = False
            return True
        except Exception as e:
            if attempt < TRIES:
                time.sleep(attempt)
                continue
            if not _unreachable:
                print(f"  [the echo is out of reach — the Entity speaks alone ({type(e).__name__})]")
            _unreachable = True
    return False


def _run() -> None:
    global _pending_start
    with httpx.Client(base_url=f"http://{HOST}", timeout=TIMEOUT) as client:
        while True:
            path, body, done = _outbox.get()
            try:
                if path == "/start":
                    _pending_start = body
                    if _post(client, path, body):
                        _pending_start = None
                    continue
                if _pending_start is not None:
                    if path == "/end":
                        _pending_start = None   # the Mac never knew this ritual
                        continue
                    if not _post(client, "/start", _pending_start):
                        continue                # still out of reach: this one is lost
                    _pending_start = None
                    print("  [the echo joins late]")
                _post(client, path, body)
            finally:
                if done is not None:
                    done.set()


def _send(path: str, body: dict, wait: float = 0.0) -> None:
    global _worker
    if not HOST:
        return
    if _worker is None:
        _worker = threading.Thread(target=_run, daemon=True)
        _worker.start()
    done = threading.Event() if wait else None
    _outbox.put((path, body, done))
    if done is not None:
        done.wait(wait)


def start(voice_id: str, language: str) -> None:
    # A ritual begins: the echo will speak in this borrowed voice.
    _send("/start", {"voice_id": voice_id, "language": language})


def utter(text: str) -> None:
    _send("/utter", {"text": text})


def fade(secs: float) -> None:
    # The farewell bells: the echoes fall silent over secs.
    _send("/fade", {"secs": secs})


def end() -> None:
    # The ritual is over. Waits, briefly, until the Mac has heard, so no echo is
    # made once the voice is returned.
    _send("/end", {}, wait=TRIES * (TIMEOUT + 2.0))
    report()


def report() -> None:
    # What became of this ritual's echoes, as the Mac tells it — its failures too,
    # which would otherwise be seen only on the Mac.
    if not HOST:
        return
    try:
        log = httpx.get(f"http://{HOST}/status", timeout=TIMEOUT).json().get("log", [])
    except Exception as e:
        print(f"  [the echo gives no account ({type(e).__name__})]")
        return
    print("  [the echo's account:]")
    for line in log or ["(nothing: it never knew this ritual)"]:
        print(f"    {line}")
