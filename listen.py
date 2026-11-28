"""
The ear of the Entity. The prayer and every question are heard as they are
spoken, so the words are known a breath after the seeker falls silent, and
before, which is what lets the Entity answer a question not yet finished.
"""

from __future__ import annotations

import asyncio
import base64
import threading

import numpy as np
import soundfile as sf
from elevenlabs import RealtimeEvents
from elevenlabs.client import ElevenLabs
from elevenlabs.realtime.scribe import AudioFormat, CommitStrategy

from voice import with_retry

MODEL_ID = "scribe_v2_realtime"
# For a finished recording, when the living ear could not be had.
BATCH_MODEL_ID = "scribe_v2"
BATCH_TIMEOUT_SECS = 30
CONNECT_TRIES = 3

FORMATS = {16000: AudioFormat.PCM_16000, 44100: AudioFormat.PCM_44100}

_COMMIT = object()
_CLOSE = object()


class Listener:
    """
    Hears one utterance as it is spoken. Feed it the microphone (send), ask what
    has been heard so far (heard), and when the seeker is silent, finish() gives
    the whole. Its listening runs on a thread of its own, so the recording never
    waits on the network.

    If the ear fails (no network, a refused key), it says so and falls quiet: the
    recording is then heard whole, and nothing the seeker said is lost.

    context: words the seeker is likely to repeat (their prayer), so that names
    from it are heard rightly ("妻", not "キバ").
    rate: of the 16-bit PCM it is fed, 16000 for a question, 44100 for the prayer.
    """

    def __init__(
        self, client: ElevenLabs, language: str, context: str = "", rate: int = 16000
    ) -> None:
        self.partial = ""
        self.final: str | None = None
        self.failed = False
        self.closing = False
        self.committed = threading.Event()
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.queue: asyncio.Queue = asyncio.Queue()
        self.context = context[-2000:]
        self.rate = rate
        asyncio.run_coroutine_threadsafe(self.run(client, language), self.loop)

    @property
    def heard(self) -> str:
        return self.final if self.final is not None else self.partial

    def send(self, pcm: bytes) -> None:
        if not self.failed:
            self.loop.call_soon_threadsafe(self.queue.put_nowait, pcm)

    def finish(self, timeout: float = 4.0) -> str:
        # The seeker is silent: the whole of what they said.
        if not self.failed:
            self.loop.call_soon_threadsafe(self.queue.put_nowait, _COMMIT)
            self.committed.wait(timeout)
        self.close()
        return self.heard

    def close(self) -> None:
        self.closing = True
        self.loop.call_soon_threadsafe(self.queue.put_nowait, _CLOSE)

    def fail(self, e: BaseException) -> None:
        # Once closing, complaints about the closed socket are no failure: the words are in.
        if self.closing:
            return
        if not self.failed:
            print(f"  [the ear is closed — {type(e).__name__}: {e}]")
        self.failed = True
        self.committed.set()

    def on_partial(self, data: dict) -> None:
        self.partial = (data.get("text") or data.get("transcript") or "").strip() or self.partial

    def on_committed(self, data: dict) -> None:
        self.final = (data.get("text") or data.get("transcript") or "").strip()
        self.committed.set()

    async def connect(self, client: ElevenLabs, language: str):
        # A slow network may let the first greeting lapse; what is spoken meanwhile
        # waits in the queue, so trying again loses nothing.
        for attempt in range(1, CONNECT_TRIES + 1):
            try:
                return await client.speech_to_text.realtime.connect({
                    "model_id": MODEL_ID,
                    "audio_format": FORMATS[self.rate],
                    "sample_rate": self.rate,
                    "commit_strategy": CommitStrategy.MANUAL,   # the silence decides, not the ear
                    "language_code": language,
                })
            except Exception as e:
                if attempt == CONNECT_TRIES or self.closing:
                    raise
                print(f"  [the ear: {type(e).__name__} — again, {attempt}/{CONNECT_TRIES - 1}]")
                await asyncio.sleep(1.0)

    async def run(self, client: ElevenLabs, language: str) -> None:
        try:
            conn = await self.connect(client, language)
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
                        # The context may travel only with the first breath
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


def transcribe(client: ElevenLabs, wav_path: str, language: str | None = None) -> str:
    """
    A finished recording, heard whole: when the living ear failed, and for
    debug.wav. If the tongue given yields nothing (a seeker who consented in
    Japanese but prays in English), it is heard once more with no tongue assumed.
    """
    with open(wav_path, "rb") as f:
        audio = f.read()
    kwargs = {"language_code": language} if language else {}
    try:
        result = with_retry(
            lambda: client.speech_to_text.convert(
                file=("speech.wav", audio, "audio/wav"),
                model_id=BATCH_MODEL_ID,
                request_options={"timeout_in_seconds": BATCH_TIMEOUT_SECS},
                **kwargs,
            ),
            "listen",
        )
    except Exception as e:
        print(f"  [the words could not be heard — {type(e).__name__}: {e}]")
        return ""
    text = (result.text or "").strip()
    if not text:
        data, rate = sf.read(wav_path)
        level = float(np.sqrt(np.mean(np.square(data)))) if len(data) else 0.0
        print(f"  [only silence was heard — {len(data) / rate:.1f}s at {level:.4f} rms]")
        if language:
            return transcribe(client, wav_path, language=None)
    return text
