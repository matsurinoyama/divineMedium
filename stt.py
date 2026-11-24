from __future__ import annotations

import json
import os
import re

import numpy as np
import soundfile as sf

from google import genai
from google.genai import types

import gemini

# Inline audio is capped by the request size (20 MB). A prayer at 16 kHz PCM_16 is
# ~32 KB/s, so anything under ten minutes goes inline; longer recordings are
# uploaded through the Files API instead.
INLINE_LIMIT = 15 * 1024 * 1024

LANGUAGE_NAMES: dict[str, str] = {"en": "English", "ja": "Japanese"}

TRANSCRIBE_PROMPT = (
    "This is a recording made at an art installation: one person speaking aloud, "
    "alone, in English or Japanese. Transcribe the speech verbatim. Keep hesitation "
    "and repetition as spoken. Do not translate, summarise, explain, label the "
    "speaker, or add anything that was not said. If the recording contains no "
    "intelligible speech, return an empty string."
)

RESPONSE_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "text": types.Schema(
            type=types.Type.STRING,
            description="The verbatim transcript, or an empty string if nothing was said.",
        ),
        "language": types.Schema(
            type=types.Type.STRING,
            description="BCP-47 primary subtag of the spoken language, e.g. en or ja.",
        ),
    },
    required=["text", "language"],
)

JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def audio_part(client: genai.Client, wav_path: str) -> types.Part | types.File:
    # Inline the WAV when it is small enough, otherwise upload it first.
    if os.path.getsize(wav_path) <= INLINE_LIMIT:
        with open(wav_path, "rb") as f:
            return types.Part.from_bytes(data=f.read(), mime_type="audio/wav")
    print("  [recording is large — uploading]")
    return client.files.upload(file=wav_path)


def transcribe(
    client: genai.Client, wav_path: str, language: str | None = None
) -> tuple[str, str]:
    """
    Transcribe a WAV with Gemini. Returns (text, language_code).

    Replaces tuhanKami's local Whisper model, which needs minutes per question on
    Pi-class hardware. language, if given, is passed as a hint rather than relying
    on detection — the same role LANGUAGE_OVERRIDE played before.
    """
    prompt = TRANSCRIBE_PROMPT
    if language:
        prompt += f" The speech is in {LANGUAGE_NAMES.get(language, language)}."

    raw = gemini.generate(
        client,
        [audio_part(client, wav_path), prompt],
        label="stt",
        # Transcription is the one place where invention is a defect, not a feature.
        temperature=0.0,
        top_p=1.0,
        max_tokens=4096,
        response_mime_type="application/json",
        response_schema=RESPONSE_SCHEMA,
    )

    text, detected = parse(raw)
    if not text:
        diagnose(wav_path, raw)
        if language:
            # A wrong hint (a seeker who consented in Japanese but prays in
            # English) can make Gemini return nothing; listen once more without it.
            print("  [stt: nothing heard — trying again without the language hint]")
            return transcribe(client, wav_path, language=None)
    return text, (language or detected)


def diagnose(wav_path: str, raw: str) -> None:
    # Why a transcript came back empty: too little captured, or Gemini's answer.
    data, rate = sf.read(wav_path)
    level = float(np.sqrt(np.mean(np.square(data)))) if len(data) else 0.0
    print(f"  [stt: empty transcript — recording {len(data) / rate:.1f}s, "
          f"level {level:.4f} rms, Gemini returned {raw[:160]!r}]")


def parse(raw: str) -> tuple[str, str]:
    # Pull text and language out of the JSON response, tolerating a stray code
    # fence or a sentence wrapped around it.
    match = JSON_RE.search(raw or "")
    if not match:
        return (raw or "").strip(), "en"
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return (raw or "").strip(), "en"
    text = str(data.get("text") or "").strip()
    # Trim a region subtag: ja-JP and en-US are the same voice as far as the
    # entity's language instruction is concerned.
    lang = str(data.get("language") or "en").strip().lower().split("-")[0]
    return text, lang or "en"
