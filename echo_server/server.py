"""
The echo server, run on the Mac beside the surrounding speakers. The Pi tells it
what the entity says; it answers with echoes, in the seeker's cloned voice, on
the four speakers around the room. The bells stay on the Pi.

  POST /start  {"voice_id": ..., "language": "ja"}   a session begins
  POST /utter  {"text": ...}                         the entity has spoken
  POST /fade   {"secs": 20}                          the farewell bells: fade out
  POST /end    {}                                    the session is over
  GET  /health

  python server.py                 serve on 0.0.0.0:8765 (ECHO_PORT)
  python server.py --devices       list the audio outputs, for ECHO_OUTPUTS
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dotenv import load_dotenv

load_dotenv()

import anthropic
import sounddevice as sd
from elevenlabs.client import ElevenLabs

import echoes

PORT = int(os.getenv("ECHO_PORT", "8765"))
END_FADE_SECS = 2.0


class Session:
    def __init__(self, voice_id: str, language: str) -> None:
        self.voice_id = voice_id
        self.language = language
        self.player = echoes.EchoPlayer()
        self.closed = threading.Event()
        self.player.start()

    def close(self) -> None:
        # Fade whatever is still sounding, then close the streams — in the background.
        if self.closed.is_set():
            return
        self.closed.set()
        self.player.fade_out(END_FADE_SECS)

        def stop() -> None:
            time.sleep(END_FADE_SECS + 0.2)
            self.player.stop()

        threading.Thread(target=stop, daemon=True).start()


class Echo:
    def __init__(self) -> None:
        self.claude = anthropic.Anthropic()
        self.eleven = ElevenLabs(api_key=os.environ["ELEVENLABS_API_KEY"])
        self.lock = threading.Lock()
        self.session: Session | None = None

    def start(self, voice_id: str, language: str) -> None:
        with self.lock:
            if self.session is not None:
                # The Pi never ended the last one (a crash, a power cut)
                self.session.close()
            self.session = Session(voice_id, language)
        print(f"  [session: {language} · {echoes.ECHO_OUTPUTS}]")

    def utter(self, text: str) -> None:
        session = self.session
        if session is None or session.closed.is_set():
            return

        def make() -> None:
            # Made in its own thread: an echo can be still forming when the next arrives.
            try:
                fragments = echoes.echo_text(self.claude, text, session.language)
                print(f"  Echo: {fragments}")
                if session.closed.is_set():
                    return
                audio = echoes.process(echoes.speak(self.eleven, session.voice_id, fragments))
                # A session that ended while this was made does not hear it
                if not session.closed.is_set():
                    session.player.feed(audio)
            except Exception as e:
                print(f"  [echo failed: {type(e).__name__}: {e}]")

        threading.Thread(target=make, daemon=True).start()

    def fade(self, secs: float) -> None:
        session = self.session
        if session is not None:
            session.player.fade_out(secs)

    def end(self) -> None:
        with self.lock:
            session, self.session = self.session, None
        if session is not None:
            session.close()
            print("  [session ended]")


def handler(echo: Echo) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def reply(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            if self.path == "/health":
                self.reply(200, {"ok": True, "session": echo.session is not None})
            else:
                self.reply(404, {"error": "not found"})

        def do_POST(self) -> None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                if self.path == "/start":
                    echo.start(body["voice_id"], body.get("language", "en"))
                elif self.path == "/utter":
                    echo.utter(body["text"])
                elif self.path == "/fade":
                    echo.fade(float(body.get("secs", 20.0)))
                elif self.path == "/end":
                    echo.end()
                else:
                    self.reply(404, {"error": "not found"})
                    return
                self.reply(200, {"ok": True})
            except Exception as e:
                self.reply(400, {"error": f"{type(e).__name__}: {e}"})

        def log_message(self, format: str, *args) -> None:
            print(f"  {self.command} {self.path}")

    return Handler


def main() -> None:
    if "--devices" in sys.argv:
        print(sd.query_devices())
        return
    echo = Echo()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), handler(echo))
    print(f"  [echo server on :{PORT} · outputs {echoes.ECHO_OUTPUTS}]")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        echo.end()
        print("\n  [Exiting.]")


if __name__ == "__main__":
    main()
