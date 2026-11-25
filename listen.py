from __future__ import annotations

import asyncio
import base64
import threading

import numpy as np
import soundfile as sf

from elevenlabs import RealtimeEvents
from elevenlabs.client import ElevenLabs
from elevenlabs.realtime.scribe import AudioFormat, CommitStrategy

import tts

# ElevenLabs Scribe, realtime: the prayer and every question are transcribed while
# they are spoken, so the words are there about a second after the seeker stops
# (Gemini, on the finished recording, took four to eight) — and, for a question,
# before the seeker stops, which is what lets the entity answer midway.
MODEL_ID = "scribe_v2_realtime"
# Scribe on a finished recording: the fallback when the live connection fails.
BATCH_MODEL_ID = "scribe_v2"
BATCH_TIMEOUT_SECS = 30

CONNECT_TRIES = 3

FORMATS = {16000: AudioFormat.PCM_16000, 44100: AudioFormat.PCM_44100}

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
    rate: of the 16-bit PCM it is fed — 16000 for questions, 44100 for the prayer.
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
        # The words so far: the final transcript once there is one, else the latest partial.
        return self.final if self.final is not None else self.partial

    def send(self, pcm: bytes) -> None:
        # 16-bit PCM at the listener's rate, in the order it was recorded.
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
        self.closing = True
        self.loop.call_soon_threadsafe(self.queue.put_nowait, _CLOSE)

    def fail(self, e: BaseException) -> None:
        # Once we are closing, the server's errors about the closed socket
        # ("no close frame received") are not failures: the transcript is in.
        if self.closing:
            return
        if not self.failed:
            print(f"  [listen: live transcription unavailable — {type(e).__name__}: {e}]")
        self.failed = True
        self.committed.set()

    def on_partial(self, data: dict) -> None:
        self.partial = (data.get("text") or data.get("transcript") or "").strip() or self.partial

    def on_committed(self, data: dict) -> None:
        self.final = (data.get("text") or data.get("transcript") or "").strip()
        self.committed.set()

    async def connect(self, client: ElevenLabs, language: str):
        # A slow network can time the handshake out; audio sent meanwhile waits in
        # the queue, so trying again loses nothing.
        for attempt in range(1, CONNECT_TRIES + 1):
            try:
                return await client.speech_to_text.realtime.connect({
                    "model_id": MODEL_ID,
                    "audio_format": FORMATS[self.rate],
                    "sample_rate": self.rate,
                    "commit_strategy": CommitStrategy.MANUAL,   # our own speech detection decides
                    "language_code": language,
                })
            except Exception as e:
                if attempt == CONNECT_TRIES or self.closing:
                    raise
                print(f"  [listen: {type(e).__name__} connecting — retry {attempt}/{CONNECT_TRIES - 1}]")
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


def transcribe(client: ElevenLabs, wav_path: str, language: str | None = None) -> str:
    """
    Transcribe a finished recording with Scribe: the fallback for when a live
    transcript could not be had, and for debug.wav.

    A dropped connection is retried, and no single try may hang past
    BATCH_TIMEOUT_SECS. language, if given, is a hint; if the hint yields nothing
    (a seeker who consented in Japanese but prays in English), it listens once
    more without it.
    """
    with open(wav_path, "rb") as f:
        audio = f.read()
    kwargs = {"language_code": language} if language else {}
    try:
        result = tts.with_retry(
            lambda: client.speech_to_text.convert(
                file=("speech.wav", audio, "audio/wav"),
                model_id=BATCH_MODEL_ID,
                request_options={"timeout_in_seconds": BATCH_TIMEOUT_SECS},
                **kwargs,
            ),
            "listen",
        )
    except Exception as e:
        print(f"  [listen: transcription failed — {type(e).__name__}: {e}]")
        return ""
    text = (result.text or "").strip()
    if not text:
        diagnose(wav_path)
        if language:
            print("  [listen: nothing heard — trying again without the language hint]")
            return transcribe(client, wav_path, language=None)
    return text


def diagnose(wav_path: str) -> None:
    # Why a transcript came back empty: was anything much captured at all?
    data, rate = sf.read(wav_path)
    level = float(np.sqrt(np.mean(np.square(data)))) if len(data) else 0.0
    print(f"  [listen: empty transcript — recording {len(data) / rate:.1f}s, level {level:.4f} rms]")
