from __future__ import annotations

import os
import queue
import random
import threading
import time
from typing import TYPE_CHECKING

import numpy as np
import sounddevice as sd
import soundfile as sf
from scipy.signal import resample as sp_resample

if TYPE_CHECKING:
    from control import Session

# Question recording rate — 16 kHz PCM_16 is what speech recognition wants.
SAMPLE_RATE = 16000
# The prayer is recorded at 44100 Hz, as in tuhanKami, because it is also the
# source for the voice clone and ElevenLabs needs the fidelity.
PRAYER_SAMPLE_RATE = 44100
# Rate of everything played back: the cloned voice (ElevenLabs pcm_24000) and the
# bells, which are resampled to it.
PLAYBACK_RATE = 24000

# VAD constants used by record_question
VAD_CAL_CHUNKS = 20     # ~1 second of ambient noise to calibrate the threshold
VAD_MULTIPLIER = 2      # speech threshold = ambient RMS × this
VAD_ONSET_CHUNKS = 2    # consecutive loud chunks required before recording starts
VAD_SILENCE_SECS = 1.5  # seconds of silence that ends the recording

# Input device for prayer and question recording. None = system default; on a Pi
# with a USB microphone this is usually right without touching it.
_speech_input = os.getenv("SPEECH_INPUT")
SPEECH_INPUT: int | str | None = int(_speech_input) if (_speech_input or "").isdigit() else _speech_input or None
# Output device for the voice and the bells, same convention. None = system default.
_main_output = os.getenv("MAIN_OUTPUT")
MAIN_OUTPUT: int | str | None = int(_main_output) if (_main_output or "").isdigit() else _main_output or None

BELLS = [f"bonsho/Bonsho04-{n}.mp3" for n in range(1, 5)]

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
    CHUNK_SIZE = int(PRAYER_SAMPLE_RATE * 0.05)

    session.stop.clear()
    grace_until = time.monotonic() + START_GRACE_SECS

    def callback(indata, _frames, _t, _status):
        q.put(indata.copy())

    with sd.InputStream(samplerate=PRAYER_SAMPLE_RATE, device=SPEECH_INPUT, channels=1,
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
        sf.write(filename, np.concatenate(chunks, axis=0), PRAYER_SAMPLE_RATE, subtype="PCM_16")


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
# Ported from tuhanKami's audio.py: the reverse reverb on the main voice and the
# bells. tuhanKami's pitch shift and its multi-speaker echo player are not here —
# the voice is the plain clone, and the echo voice is left for later.


def resample(audio: np.ndarray, rate: int, target: int) -> np.ndarray:
    if rate == target:
        return audio
    return np.asarray(sp_resample(audio, int(len(audio) * target / rate)), dtype="float32")


def compress_for_ivc(src_wav: str, dst_wav: str, target_rate: int = 22050) -> None:
    # Resample prayer WAV to 16-bit PCM at target_rate for ElevenLabs IVC upload (11 MB limit).
    data, rate = sf.read(src_wav, dtype="float32")
    if data.ndim > 1:
        data = data.mean(axis=1)
    sf.write(dst_wav, resample(data, rate, target_rate), target_rate, subtype="PCM_16")


def pcm_to_float(pcm: bytes) -> np.ndarray:
    # ElevenLabs raw 16-bit PCM → float32 in [-1, 1].
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0


def add_reverb(audio: np.ndarray, delay_samples: int, decay: float, echoes: int) -> np.ndarray:
    # Simple delay-line reverb: sum N copies of the signal, each delayed and attenuated.
    result = audio.copy()
    for i in range(1, echoes + 1):
        start = i * delay_samples
        if start < len(audio):
            result[start:] += audio[:len(audio) - start] * (decay ** i)
    return np.clip(result, -1.0, 1.0)


def trim_trailing_silence(audio: np.ndarray, threshold: float = 0.01) -> np.ndarray:
    # Remove silent samples from the end of an array, keeping up to the last loud sample.
    active = np.where(np.abs(audio) > threshold)[0]
    if len(active) == 0:
        return audio
    return audio[:active[-1] + 1]


def reverse_reverb(
    audio: np.ndarray,
    delay_secs: float = 888 / 16000,
    decay: float = 0.50,
    echoes: int = 25,
    pre_pad_secs: float = 0.15,
    post_pad_secs: float = 1.5,
    fade_lead_secs: float = 3.0,
) -> np.ndarray:
    """
    Swell-in / fade-out effect via time-reversed reverb.

      1. Trim trailing TTS silence so the fade anchors to real speech content.
      2. Reverse the audio, apply heavy reverb (which now bleeds backward in time).
      3. Reverse again — reverb energy now builds up *into* the speech (swell-in).
      4. Apply a cosine fade starting fade_lead_secs before speech ends, fading
         through the post-pad to silence (natural trailing decay).

    The delay is in seconds (tuhanKami's 888 samples at 16 kHz) so the effect
    sounds the same at PLAYBACK_RATE.
    """
    pre = np.zeros(int(pre_pad_secs * PLAYBACK_RATE), dtype="float32")
    post = np.zeros(int(post_pad_secs * PLAYBACK_RATE), dtype="float32")
    speech = trim_trailing_silence(audio.astype("float32"))
    padded = np.concatenate([pre, speech, post])
    wet = add_reverb(padded[::-1].copy(), int(delay_secs * PLAYBACK_RATE), decay, echoes)
    result = wet[::-1].copy()
    speech_end = len(pre) + len(speech)
    fade_start = max(len(pre), speech_end - int(fade_lead_secs * PLAYBACK_RATE))
    fade_len = len(result) - fade_start
    if fade_len > 0:
        result[fade_start:] *= np.cos(np.linspace(0.0, np.pi / 2, fade_len, dtype="float32"))
    return result


def build_voice(pcm: bytes) -> np.ndarray:
    # The entity's voice as played: reverse-reverb swell, a final 2-second fade and
    # half a second of tail.
    processed = reverse_reverb(pcm_to_float(pcm))
    fade_n = min(int(2.0 * PLAYBACK_RATE), len(processed))
    processed[-fade_n:] *= np.cos(np.linspace(0.0, np.pi / 2, fade_n, dtype="float32"))
    return np.concatenate([processed, np.zeros(int(0.5 * PLAYBACK_RATE), dtype="float32")])


def load_bell(bell_path: str) -> np.ndarray:
    # Load a bell audio file, mix to mono, and resample to PLAYBACK_RATE.
    data, rate = sf.read(bell_path, dtype="float32")
    if data.ndim > 1:
        data = data.mean(axis=1)
    return resample(data, rate, PLAYBACK_RATE)


def random_bell() -> str:
    # One bell is chosen per session, as in tuhanKami.
    return random.choice(BELLS)


def bell_mix(
    bell_path: str,
    *,
    times: int = 1,
    overlap_secs_min: float = 0.0,
    overlap_secs_max: float = 0.0,
    voice: np.ndarray | None = None,
    voice_offset_secs: float = 3.0,
) -> np.ndarray:
    """
    N bell strikes, optionally with the voice mixed in voice_offset_secs after the
    first. Strikes are spaced by a random gap within the overlap range.
    """
    bell = load_bell(bell_path)
    step = int(np.random.uniform(overlap_secs_min, overlap_secs_max) * PLAYBACK_RATE)
    total = step * (times - 1) + len(bell)
    offset = int(voice_offset_secs * PLAYBACK_RATE)
    if voice is not None:
        total = max(total, offset + len(voice))

    mixed = np.zeros(total, dtype="float32")
    for i in range(times):
        mixed[i * step: i * step + len(bell)] += bell
    if voice is not None:
        mixed[offset: offset + len(voice)] += voice

    peak = float(np.max(np.abs(mixed)))
    if peak > 1.0:
        mixed /= peak
    return mixed


def play(audio: np.ndarray, *stops: threading.Event) -> None:
    # Play on MAIN_OUTPUT, blocking until done or until any of stops is set.
    CHUNK = int(PLAYBACK_RATE * 0.05)
    try:
        with sd.OutputStream(samplerate=PLAYBACK_RATE, device=MAIN_OUTPUT,
                             channels=1, dtype="float32") as stream:
            for pos in range(0, len(audio), CHUNK):
                if any(e.is_set() for e in stops):
                    break
                stream.write(audio[pos: pos + CHUNK].reshape(-1, 1))
    except Exception as e:
        # A dead speaker must not end the session — the panel still carries the words.
        print(f"  [audio/playback error: {e}]")
