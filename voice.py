"""
The borrowed voice. The Entity has no voice of its own: it takes the seeker's,
learned from their prayer, speaks in it, and gives it back when they leave.
"""

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

MODEL_ID = "eleven_flash_v2_5"
OUTPUT_FORMAT = f"pcm_{PLAYBACK_RATE}"
# Every voice this work borrows bears this name, so none can be left behind.
CLONE_NAME = "divineMedium_entity"

# Steady and clear; the reverse reverb at playback is its only adornment.
VOICE_SETTINGS = VoiceSettings(
    stability=0.65,
    similarity_boost=1.0,
    style=0.3,
    speed=0.9,
    use_speaker_boost=True,
)

# The network faltering: a dropped connection, a name that would not resolve.
NETWORK_ERRORS = (httpx.TransportError, httpx2.TransportError)


def with_retry(fn: Callable[[], Any], label: str, tries: int = 4) -> Any:
    # Patience, about as long as a router takes to recover: ~2 s, 4 s, 8 s.
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
    # ElevenLabs is the Entity's ear as well as its voice, and cannot be done without.
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        raise RuntimeError("ELEVENLABS_API_KEY is not set — see .env")
    return ElevenLabs(api_key=key)


def borrow_voice(client: ElevenLabs, prayer_wav: str) -> str:
    # The seeker's voice, learned from their prayer. Returns its id.
    print("  [The voice is being borrowed...]")
    with open(prayer_wav, "rb") as f:
        audio = f.read()
    # labels must be given: left out, the SDK sends a placeholder the API refuses.
    voice = with_retry(
        lambda: client.voices.ivc.create(
            name=CLONE_NAME,
            files=[("prayer.wav", audio, "audio/wav")],
            labels={"source": "divineMedium"},
        ),
        "clone",
    )
    print("  [The voice is borrowed.]")
    return voice.voice_id


def speak(client: ElevenLabs, voice_id: str, text: str) -> Iterator[bytes]:
    # text in the borrowed voice, as raw PCM, flowing as it is made: the first
    # breath comes in about 0.3 s. A connection lost before it is tried again.
    def first_breath() -> tuple[bytes, Iterator[bytes]]:
        chunks = iter(client.text_to_speech.stream(
            voice_id=voice_id,
            text=text,
            model_id=MODEL_ID,
            output_format=OUTPUT_FORMAT,
            voice_settings=VOICE_SETTINGS,
        ))
        return next(chunks, b""), chunks

    first, rest = with_retry(first_breath, "voice")
    yield first
    yield from rest


def release_voice(client: ElevenLabs, voice_id: str) -> None:
    # The voice is given back: deleted, as the covenant promised.
    try:
        with_retry(lambda: client.voices.delete(voice_id), "delete")
        print("  [The voice is returned.]")
    except Exception as e:
        print(f"  [the voice could not be returned ({voice_id}): {e}]")


def release_lingering_voices(client: ElevenLabs) -> None:
    # Voices a crash or a power cut kept from being returned. Called at waking,
    # when no ritual holds any of them.
    try:
        lingering = [
            v.voice_id
            for v in client.voices.search(search=CLONE_NAME).voices
            if v.name == CLONE_NAME
        ]
    except Exception as e:
        print(f"  [lingering voices could not be sought: {e}]")
        return
    for voice_id in lingering:
        print(f"  [a voice lingers from an earlier ritual ({voice_id})]")
        release_voice(client, voice_id)
