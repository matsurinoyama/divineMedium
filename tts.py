from __future__ import annotations

import os

from elevenlabs import VoiceSettings
from elevenlabs.client import ElevenLabs

from audio import PLAYBACK_RATE

# ElevenLabs model and output format used for all speech synthesis
MODEL_ID = "eleven_flash_v2_5"
OUTPUT_FORMAT = f"pcm_{PLAYBACK_RATE}"   # raw 16-bit PCM, played as-is by audio.play

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


def make_client() -> ElevenLabs | None:
    # The voice is optional: without a key the installation runs text-only.
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        print("  [voice: ELEVENLABS_API_KEY is not set — running text-only]")
        return None
    return ElevenLabs(api_key=key)


def clone_voice(client: ElevenLabs, prayer_wav: str) -> str:
    # Instant voice clone from the prayer recording. Returns the voice id.
    print("  [Cloning voice...]")
    with open(prayer_wav, "rb") as f:
        # labels must be passed: left out, this SDK still sends the field, serialised
        # from its "omitted" placeholder, and the API rejects it (invalid_labels).
        voice = client.voices.ivc.create(
            name="divineMedium_entity", files=[f], labels={"source": "divineMedium"}
        )
    print("  [Voice ready]")
    return voice.voice_id


def speak(client: ElevenLabs, voice_id: str, text: str) -> bytes:
    # Synthesise text to raw PCM bytes in the cloned voice.
    chunks = client.text_to_speech.convert(
        voice_id=voice_id,
        text=text,
        model_id=MODEL_ID,
        output_format=OUTPUT_FORMAT,
        voice_settings=VOICE_SETTINGS,
    )
    return b"".join(chunks)


def delete_voice(client: ElevenLabs, voice_id: str) -> None:
    # Delete the cloned voice from ElevenLabs. Called at the end of every session.
    try:
        client.voices.delete(voice_id)
        print("  [Voice clone deleted.]")
    except Exception as e:
        print(f"  [voice clone could not be deleted ({voice_id}): {e}]")
