"""
The Pi's side of the echo: tells the echo server on the Mac (echo_server/) what
the entity says, so it can answer with echoes on the surrounding speakers.

Messages go out in order on one background thread and never hold up the ritual.
If ECHO_HOST is unset, or the Mac cannot be reached, every call does nothing and
the session carries on exactly as it would without the echo.
"""

from __future__ import annotations

import os
import queue
import threading
import time

import httpx

HOST = os.getenv("ECHO_HOST")   # e.g. "mymac.local:8765"
TIMEOUT = 3.0
TRIES = 3          # per message, ~1 s and 2 s apart: rides out a Wi-Fi blink

_outbox: queue.Queue[tuple[str, dict, threading.Event | None]] = queue.Queue()
_worker: threading.Thread | None = None
_unreachable = False   # said once, not on every message
# The current session's /start, kept until the Mac has it. Without it the Mac
# ignores every line, so a /start lost to a network blink is sent again before
# the next message — the echo joins late rather than not at all.
_pending_start: dict | None = None


def _post(client: httpx.Client, path: str, body: dict) -> bool:
    # One message, retried; True once the Mac has it.
    global _unreachable
    for attempt in range(1, TRIES + 1):
        try:
            client.post(path, json=body).raise_for_status()
            if _unreachable:
                print("  [echo: reached again]")
            _unreachable = False
            return True
        except Exception as e:
            if attempt < TRIES:
                time.sleep(attempt)
                continue
            if not _unreachable:
                print(f"  [echo: server unreachable — carrying on without it ({type(e).__name__})]")
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
                        _pending_start = None   # the Mac never had this session
                        continue
                    if not _post(client, "/start", _pending_start):
                        continue                # still unreachable: drop this line
                    _pending_start = None
                    print("  [echo: session started late]")
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
    # A session begins: the echo will speak in this voice.
    _send("/start", {"voice_id": voice_id, "language": language})


def utter(text: str) -> None:
    # The entity has spoken: the echo answers it.
    _send("/utter", {"text": text})


def fade(secs: float) -> None:
    # The farewell bells: the echo fades to silence over secs.
    _send("/fade", {"secs": secs})


def end() -> None:
    # The session is over. Waits (briefly) until the Mac has heard, so no echo is
    # made after the voice clone is deleted.
    _send("/end", {}, wait=TRIES * (TIMEOUT + 2.0))
    report()


def report() -> None:
    # What the Mac says happened to this session's echoes — its errors included,
    # which otherwise appear only in the Mac's own terminal.
    if not HOST:
        return
    try:
        log = httpx.get(f"http://{HOST}/status", timeout=TIMEOUT).json().get("log", [])
    except Exception as e:
        print(f"  [echo: no report from the Mac ({type(e).__name__})]")
        return
    print("  [echo — the Mac reports:]")
    for line in log or ["(nothing: it never heard of this session)"]:
        print(f"    {line}")
