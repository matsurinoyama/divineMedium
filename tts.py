from __future__ import annotations

import os
import random
import time
from collections.abc import Iterator
from typing import Any, Callable

import httpx
import httpx2

from elevenlabs import VoiceSettings
from elevenlabs.client import ElevenLabs

from audio import PLAYBACK_RATE

# ElevenLabs model and output format used for all speech synthesis
MODEL_ID = "eleven_flash_v2_5"
OUTPUT_FORMAT = f"pcm_{PLAYBACK_RATE}"   # raw 16-bit PCM, played as it streams by audio.play_stream

# tuhanKami's main voice: stable and clear. Unlike tuhanKami there is no remix and
# no second (echo) clone — the entity speaks in the seeker's own cloned voice, and
# the only treatment is the reverse reverb applied at playback.
VOICE_SETTINGS = VoiceSettings(
    stability=0.65,
    similarity_boost=1.0,
    style=0.3,
    speed=0.9,
    use_speaker_boost=True,
)


# A dropped connection or a DNS failure (the installation's Wi-Fi blinking).
NETWORK_ERRORS = (httpx.TransportError, httpx2.TransportError)


def with_retry(fn: Callable[[], Any], label: str, tries: int = 4) -> Any:
    # Backoff with jitter, ~2s, 4s, 8s: about the length of a router hiccup.
    for attempt in range(1, tries + 1):
        try:
            return fn()
        except NETWORK_ERRORS as e:
            if attempt == tries:
                raise
            wait = round(2.0 * 2 ** (attempt - 1) * (0.75 + random.random() * 0.5), 1)
            print(f"  [{label}] {type(e).__name__} retry {attempt}/{tries - 1} in {wait}s")
            time.sleep(wait)


def make_client() -> ElevenLabs:
    # ElevenLabs hears every word as well as speaking them: it is required. If the
    # voice clone itself fails, a session still carries on text-only.
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        raise RuntimeError("ELEVENLABS_API_KEY is not set — see .env")
    return ElevenLabs(api_key=key)


def clone_voice(client: ElevenLabs, prayer_wav: str) -> str:
    # Instant voice clone from the prayer recording. Returns the voice id.
    print("  [Cloning voice...]")
    with open(prayer_wav, "rb") as f:
        audio = f.read()
    # labels must be passed: left out, this SDK still sends the field, serialised
    # from its "omitted" placeholder, and the API rejects it (invalid_labels).
    voice = with_retry(
        lambda: client.voices.ivc.create(
            name=CLONE_NAME,
            files=[("prayer.wav", audio, "audio/wav")],
            labels={"source": "divineMedium"},
        ),
        "clone",
    )
    print("  [Voice ready]")
    return voice.voice_id


def speak_stream(client: ElevenLabs, voice_id: str, text: str) -> Iterator[bytes]:
    """
    Synthesise text in the cloned voice as a stream of raw PCM chunks: the first
    arrives in about 0.3 s, where the whole answer takes as long to make as to say.
    A dropped connection before any audio is retried; one mid-stream ends it.
    """
    def first_chunk() -> tuple[bytes, Iterator[bytes]]:
        chunks = iter(client.text_to_speech.stream(
            voice_id=voice_id,
            text=text,
            model_id=MODEL_ID,
            output_format=OUTPUT_FORMAT,
            voice_settings=VOICE_SETTINGS,
        ))
        return next(chunks, b""), chunks

    first, rest = with_retry(first_chunk, "voice")
    yield first
    yield from rest


CLONE_NAME = "divineMedium_entity"


def sweep(client: ElevenLabs) -> None:
    # Delete every clone this installation made and did not get to delete: the
    # consent form promises the voice is gone when the seeker leaves. Run at
    # startup, when no session is running, so none of these is in use.
    try:
        leftovers = [
            v.voice_id
            for v in client.voices.search(search=CLONE_NAME).voices
            if v.name == CLONE_NAME
        ]
    except Exception as e:
        print(f"  [voice: could not check for leftover clones: {e}]")
        return
    for voice_id in leftovers:
        print(f"  [voice: deleting a clone left by an earlier session ({voice_id})]")
        delete_voice(client, voice_id)


def delete_voice(client: ElevenLabs, voice_id: str) -> None:
    # Delete the cloned voice from ElevenLabs. Called at the end of every session.
    try:
        with_retry(lambda: client.voices.delete(voice_id), "delete")
        print("  [Voice clone deleted.]")
    except Exception as e:
        print(f"  [voice clone could not be deleted ({voice_id}): {e}]")
