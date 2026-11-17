from __future__ import annotations

import os
import queue
import time
from typing import TYPE_CHECKING

import numpy as np
import sounddevice as sd
import soundfile as sf

if TYPE_CHECKING:
    from control import Session

# Recording rate for everything. tuhanKami recorded the prayer at 44100 Hz because
# ElevenLabs needed the fidelity for voice cloning; there is no cloning here, and
# 16 kHz PCM_16 is what speech recognition wants anyway — it also keeps a long
# prayer small enough to send to Gemini inline.
SAMPLE_RATE = 16000

# VAD constants used by record_question
VAD_CAL_CHUNKS = 20     # ~1 second of ambient noise to calibrate the threshold
VAD_MULTIPLIER = 2      # speech threshold = ambient RMS × this
VAD_ONSET_CHUNKS = 2    # consecutive loud chunks required before recording starts
VAD_SILENCE_SECS = 1.5  # seconds of silence that ends the recording

# Input device for prayer and question recording. None = system default; on a Pi
# with a USB microphone this is usually right without touching it.
_speech_input = os.getenv("SPEECH_INPUT")
SPEECH_INPUT: int | str | None = int(_speech_input) if (_speech_input or "").isdigit() else _speech_input or None

# Ignore the stop signal briefly after recording starts, so the same keypress that
# began the prayer cannot also end it.
START_GRACE_SECS = 0.6


def is_silent(filename: str, threshold: float = 0.001) -> bool:
    # True if the file's average amplitude is below threshold (nothing useful captured).
    data, _ = sf.read(filename)
    return float(np.abs(data).mean()) < threshold


def record_prayer(session: "Session", filename: str) -> None:
    # Record continuously until the operator signals stop ('o') or aborts.
    chunks: list[np.ndarray] = []
    q: queue.Queue = queue.Queue()
    CHUNK_SIZE = int(SAMPLE_RATE * 0.05)

    session.stop.clear()
    grace_until = time.monotonic() + START_GRACE_SECS

    def callback(indata, _frames, _t, _status):
        q.put(indata.copy())

    with sd.InputStream(samplerate=SAMPLE_RATE, device=SPEECH_INPUT, channels=1,
                        dtype="float32", blocksize=CHUNK_SIZE, callback=callback):
        while True:
            if session.abort.is_set():
                break
            if time.monotonic() < grace_until:
                session.stop.clear()
            elif session.stop.is_set():
                break
            try:
                chunks.append(q.get(timeout=0.1))
            except queue.Empty:
                pass

    if chunks:
        sf.write(filename, np.concatenate(chunks, axis=0), SAMPLE_RATE, subtype="PCM_16")


def record_question(session: "Session", filename: str) -> bool:
    """
    Record a question using voice activity detection. Returns True if the operator
    ended the session instead ('e').

    Pipeline:
      1. Calibrate ambient noise level over VAD_CAL_CHUNKS (≈1 second).
      2. Wait for VAD_ONSET_CHUNKS consecutive loud chunks (prevents false triggers).
      3. Record until VAD_SILENCE_SECS of consecutive quiet chunks.
      4. Save to filename at SAMPLE_RATE.
    paused discards the current take so the operator can intervene.
    """
    CHUNK_SIZE = int(SAMPLE_RATE * 0.05)
    SILENCE_CHUNKS = int(VAD_SILENCE_SECS / 0.05)

    q: queue.Queue = queue.Queue()
    chunks: list[np.ndarray] = []
    cal_rms: list[float] = []
    speech_started = False
    onset_count = 0
    silence_count = 0
    vad_threshold = 0.001

    def callback(indata, _frames, _t, _status):
        q.put(indata.copy())

    print("  [Listening...]")
    with sd.InputStream(samplerate=SAMPLE_RATE, device=SPEECH_INPUT, channels=1,
                        dtype="float32", blocksize=CHUNK_SIZE, callback=callback):
        while True:
            if session.end.is_set() or session.abort.is_set():
                return session.end.is_set()

            try:
                chunk = q.get(timeout=0.1)
            except queue.Empty:
                continue

            # Phase 1: calibrate noise floor
            if len(cal_rms) < VAD_CAL_CHUNKS:
                cal_rms.append(float(np.sqrt(np.mean(chunk ** 2))))
                if len(cal_rms) == VAD_CAL_CHUNKS:
                    vad_threshold = max(float(np.mean(cal_rms)) * VAD_MULTIPLIER, 0.001)
                continue

            # Operator pause: discard everything and restart
            if session.paused.is_set():
                speech_started = False
                onset_count = 0
                silence_count = 0
                chunks.clear()
                continue

            if not speech_started:
                # Phase 2: wait for onset (avoids triggering on transient noise)
                rms = float(np.sqrt(np.mean(chunk ** 2)))
                if rms > vad_threshold:
                    onset_count += 1
                    if onset_count >= VAD_ONSET_CHUNKS:
                        speech_started = True
                        print("  [Speaking...]")
                        chunks.append(chunk)
                else:
                    onset_count = 0
            else:
                # Phase 3: record until sustained silence
                chunks.append(chunk)
                rms = float(np.sqrt(np.mean(chunk ** 2)))
                if rms > vad_threshold:
                    silence_count = 0
                else:
                    silence_count += 1
                    if silence_count >= SILENCE_CHUNKS:
                        break

    if chunks:
        sf.write(filename, np.concatenate(chunks, axis=0), SAMPLE_RATE, subtype="PCM_16")
    return False


# ── Playback ──────────────────────────────────────────────────────────────────
# This version is text-only: nothing is ever played back, so the bells, the pitch
# shift, the reverse reverb, the interruptible main voice and the multi-speaker
# BackgroundEchoPlayer are all absent. They are intact in base/tuhanKami/audio.py
# and depend only on numpy, sounddevice, soundfile and scipy — when the audio
# layer returns, that file is the thing to port, together with tts.py.
