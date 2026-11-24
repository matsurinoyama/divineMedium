from __future__ import annotations

import asyncio
import base64
import threading

from elevenlabs import RealtimeEvents
from elevenlabs.client import ElevenLabs
from elevenlabs.realtime.scribe import AudioFormat, CommitStrategy

# ElevenLabs Scribe, realtime: the question is transcribed while it is spoken, so
# the entity has the words the moment the seeker stops (about a second, where
# Gemini on the finished recording took four to eight) — and has them before the
# seeker stops, which is what lets it answer midway.
MODEL_ID = "scribe_v2_realtime"
SAMPLE_RATE = 16000   # matches audio.SAMPLE_RATE, the question recording rate

_COMMIT = object()
_CLOSE = object()


class Listener:
    """
    Live transcription of one question. Feed it the microphone as it records
    (send); read what has been said so far (heard); when the seeker has finished,
    finish() returns the final transcript. All network work runs on a private
    event loop thread, so the recording loop never waits on it.

    Any failure (no network, a refused key) only marks it failed: the caller then
    transcribes the recording itself, and the question is not lost.

    context: text the question is likely to echo — the seeker's prayer — sent with
    the first audio so names and words from it are heard right ("妻", not "キバ").
    """

    def __init__(self, client: ElevenLabs, language: str, context: str = "") -> None:
        self.partial = ""
        self.final: str | None = None
        self.failed = False
        self.committed = threading.Event()
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.queue: asyncio.Queue = asyncio.Queue()
        self.context = context[-2000:]
        asyncio.run_coroutine_threadsafe(self.run(client, language), self.loop)

    @property
    def heard(self) -> str:
        # The words so far: the final transcript once there is one, else the latest partial.
        return self.final if self.final is not None else self.partial

    def send(self, pcm: bytes) -> None:
        # 16-bit PCM at SAMPLE_RATE, in the order it was recorded.
        if not self.failed:
            self.loop.call_soon_threadsafe(self.queue.put_nowait, pcm)

    def finish(self, timeout: float = 4.0) -> str:
        # The seeker has stopped: ask for the final transcript and wait for it.
        if not self.failed:
            self.loop.call_soon_threadsafe(self.queue.put_nowait, _COMMIT)
            self.committed.wait(timeout)
        self.close()
        return self.heard

    def close(self) -> None:
        self.loop.call_soon_threadsafe(self.queue.put_nowait, _CLOSE)

    def fail(self, e: BaseException) -> None:
        if not self.failed:
            print(f"  [listen: live transcription unavailable — {type(e).__name__}: {e}]")
        self.failed = True
        self.committed.set()

    def on_partial(self, data: dict) -> None:
        self.partial = (data.get("text") or data.get("transcript") or "").strip() or self.partial

    def on_committed(self, data: dict) -> None:
        self.final = (data.get("text") or data.get("transcript") or "").strip()
        self.committed.set()

    async def run(self, client: ElevenLabs, language: str) -> None:
        try:
            conn = await client.speech_to_text.realtime.connect({
                "model_id": MODEL_ID,
                "audio_format": AudioFormat.PCM_16000,
                "sample_rate": SAMPLE_RATE,
                "commit_strategy": CommitStrategy.MANUAL,   # our own speech detection decides
                "language_code": language,
            })
        except Exception as e:
            self.fail(e)
            self.loop.call_soon(self.loop.stop)
            return
        conn.on(RealtimeEvents.PARTIAL_TRANSCRIPT, self.on_partial)
        conn.on(RealtimeEvents.COMMITTED_TRANSCRIPT, self.on_committed)
        for event in (RealtimeEvents.ERROR, RealtimeEvents.AUTH_ERROR, RealtimeEvents.QUOTA_EXCEEDED,
                      RealtimeEvents.TRANSCRIBER_ERROR, RealtimeEvents.INPUT_ERROR):
            conn.on(event, lambda data, event=event: self.fail(RuntimeError(f"{event.value}: {data}")))
        try:
            while True:
                item = await self.queue.get()
                if item is _CLOSE:
                    break
                if item is _COMMIT:
                    await conn.commit()
                elif not self.failed:
                    message = {"audio_base_64": base64.b64encode(item).decode()}
                    if self.context:
                        # Context may only go with the first chunk of audio
                        message["previous_text"], self.context = self.context, ""
                    await conn.send(message)
        except Exception as e:
            self.fail(e)
        finally:
            try:
                await conn.close()
            except Exception:
                pass
            self.loop.call_soon(self.loop.stop)
