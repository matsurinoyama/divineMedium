from __future__ import annotations

import functools
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
    from collections.abc import Callable, Iterator
    from control import Session

# Question recording rate — 16 kHz PCM_16 is what speech recognition wants.
SAMPLE_RATE = 16000
# The prayer is recorded at 44100 Hz, as in tuhanKami, because it is also the
# source for the voice clone and ElevenLabs needs the fidelity.
PRAYER_SAMPLE_RATE = 44100
# Rate of everything played back: the cloned voice (ElevenLabs pcm_24000) and the
# bells, which are resampled to it.
PLAYBACK_RATE = 24000

# Input device for prayer and question recording. None = system default; on a Pi
# with a USB microphone this is usually right without touching it.
_speech_input = os.getenv("SPEECH_INPUT")
SPEECH_INPUT: int | str | None = int(_speech_input) if (_speech_input or "").isdigit() else _speech_input or None
# Output device for the voice and the bells, same convention. None = system default.
_main_output = os.getenv("MAIN_OUTPUT")
MAIN_OUTPUT: int | str | None = int(_main_output) if (_main_output or "").isdigit() else _main_output or None

BELLS = [f"bonsho/Bonsho04-{n}.mp3" for n in range(1, 5)]

# Ignore taps briefly after recording starts, so the same press that began a
# recording (consent, or the tap that opens a question) cannot also end it.
START_GRACE_SECS = 0.6


def is_silent(filename: str, threshold: float = 0.001) -> bool:
    # True if the file's average amplitude is below threshold (nothing useful captured).
    data, _ = sf.read(filename)
    return float(np.abs(data).mean()) < threshold


def record_until_tap(
    session: "Session", filename: str, rate: int, *cancels: threading.Event
) -> bool:
    """
    Record from now until the seeker taps. Returns True and writes filename when a
    tap ends it; returns False, writing nothing, if any of cancels is set first.
    The tap that ends it is consumed, so it cannot also open the next recording.
    """
    chunks: list[np.ndarray] = []
    q: queue.Queue = queue.Queue()
    CHUNK_SIZE = int(rate * 0.05)

    session.tap.clear()
    grace_until = time.monotonic() + START_GRACE_SECS

    def callback(indata, _frames, _t, _status):
        q.put(indata.copy())

    tapped = False
    with sd.InputStream(samplerate=rate, device=SPEECH_INPUT, channels=1,
                        dtype="float32", blocksize=CHUNK_SIZE, callback=callback):
        while True:
            if any(e.is_set() for e in cancels):
                break
            if time.monotonic() < grace_until:
                session.tap.clear()
            elif session.tap.is_set():
                tapped = True
                break
            try:
                chunks.append(q.get(timeout=0.1))
            except queue.Empty:
                pass
    session.tap.clear()

    if tapped and chunks:
        sf.write(filename, np.concatenate(chunks, axis=0), rate, subtype="PCM_16")
    return tapped


def record_prayer(session: "Session", filename: str) -> None:
    # Recording starts at consent and runs until the seeker taps once.
    record_until_tap(session, filename, PRAYER_SAMPLE_RATE, session.abort)


def record_question(
    session: "Session",
    filename: str,
    on_start: Callable[[], None] | None = None,
    on_end: Callable[[], None] | None = None,
) -> bool:
    """
    One question, controlled by the button: wait for a tap, record until the next.
    Returns True if the seeker ended the session (a double press) instead — while
    waiting, or mid-question, in which case the unfinished question is dropped.

    A tap made while the entity was still answering is kept and opens the
    question straight away: that seeker is ready to speak.

    on_start and on_end are called at the opening and the closing tap.
    """
    print("  [Waiting — tap once to ask, again when done]")
    while not session.tap.is_set():
        if session.end.is_set() or session.abort.is_set():
            return session.end.is_set()
        time.sleep(0.05)

    print("  [Recording question...]")
    if on_start is not None:
        on_start()
    if not record_until_tap(session, filename, SAMPLE_RATE, session.end, session.abort):
        return session.end.is_set()
    if on_end is not None:
        on_end()
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


def trim_trailing_silence(audio: np.ndarray, threshold: float = 0.01) -> np.ndarray:
    # Remove silent samples from the end of an array, keeping up to the last loud sample.
    active = np.where(np.abs(audio) > threshold)[0]
    if len(active) == 0:
        return audio
    return audio[:active[-1] + 1]


class ReverseReverb:
    """
    tuhanKami's reverse reverb — the voice swelling into each word out of its own
    echo — applied to speech as it streams in, so the voice can start before the
    whole answer has been synthesised.

    Reversed reverb is a sum of echoes that come *before* the sound: each output
    sample is the input plus the input delay, 2·delay, ... later, each quieter. So
    it only needs to see ECHOES·delay (0.67 s) ahead. The end of the speech fades
    out over its last FADE_LEAD seconds and the post-pad; holding back that much
    is what lets the fade be applied without knowing in advance where speech ends.

    tuhanKami summed 25 echoes; past 12 they are below 1/4000 of the voice, so
    12 sound the same with half the lookahead. Its fade began 3 s before the end
    of speech; here 1 s, the price of starting to speak about 2 s sooner.
    """

    DELAY_SECS = 888 / 16000   # tuhanKami's 888 samples at 16 kHz
    DECAY = 0.50
    ECHOES = 12
    PRE_PAD_SECS = 0.15        # room for the first word's swell
    POST_PAD_SECS = 1.5
    FADE_LEAD_SECS = 1.0
    TAIL_SECS = 0.5

    def __init__(self) -> None:
        self.delay = int(self.DELAY_SECS * PLAYBACK_RATE)
        self.hold = max(self.ECHOES * self.delay, int(self.FADE_LEAD_SECS * PLAYBACK_RATE))
        self.pending = np.zeros(int(self.PRE_PAD_SECS * PLAYBACK_RATE), dtype="float32")
        self.carry = b""   # half a 16-bit sample left over from the last chunk

    def wet(self, x: np.ndarray, n: int) -> np.ndarray:
        # The first n samples of x, each with its echoes from later in x added.
        y = x[:n].copy()
        for i in range(1, self.ECHOES + 1):
            ahead = x[i * self.delay: i * self.delay + n]
            y[:len(ahead)] += ahead * self.DECAY ** i
        return np.clip(y, -1.0, 1.0)

    def feed(self, pcm: bytes) -> np.ndarray:
        # Take raw 16-bit PCM; return whatever is now ready to play (may be empty).
        pcm = self.carry + pcm
        cut = len(pcm) - len(pcm) % 2
        self.carry = pcm[cut:]
        self.pending = np.concatenate([self.pending, pcm_to_float(pcm[:cut])])
        ready = len(self.pending) - self.hold
        if ready <= 0:
            return np.zeros(0, dtype="float32")
        out = self.wet(self.pending, ready)
        self.pending = self.pending[ready:]
        return out

    def finish(self) -> np.ndarray:
        # The rest, once the speech has ended: its echoes, the fade and the tail.
        speech = trim_trailing_silence(self.pending)
        x = np.concatenate([speech, np.zeros(int(self.POST_PAD_SECS * PLAYBACK_RATE), dtype="float32")])
        y = self.wet(x, len(x))
        fade_start = max(0, len(speech) - int(self.FADE_LEAD_SECS * PLAYBACK_RATE))
        fade_len = len(y) - fade_start
        y[fade_start:] *= np.cos(np.linspace(0.0, np.pi / 2, fade_len, dtype="float32"))
        return np.concatenate([y, np.zeros(int(self.TAIL_SECS * PLAYBACK_RATE), dtype="float32")])


@functools.lru_cache(maxsize=None)
def load_bell(bell_path: str) -> np.ndarray:
    # Load a bell audio file, mix to mono, and resample to PLAYBACK_RATE. Cached,
    # as the resample is slow on a Pi; callers must not modify the returned array.
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
) -> np.ndarray:
    # N bell strikes, spaced by a random gap within the overlap range.
    bell = load_bell(bell_path)
    step = int(np.random.uniform(overlap_secs_min, overlap_secs_max) * PLAYBACK_RATE)
    mixed = np.zeros(step * (times - 1) + len(bell), dtype="float32")
    for i in range(times):
        mixed[i * step: i * step + len(bell)] += bell
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


def play_stream(
    speech: Iterator[bytes] | None,
    *stops: threading.Event,
    bed: np.ndarray | None = None,
    voice_offset_secs: float = 0.0,
) -> None:
    """
    Play speech as it is synthesised, through the reverse reverb, optionally over
    a bed (the greeting's bell) that starts at once while the voice comes in
    voice_offset_secs later — or later still, if the voice is not ready by then.
    Blocks until both are done or any of stops is set.

    speech is an iterator of raw 16-bit PCM chunks at PLAYBACK_RATE (None: no
    voice this session, so the bed alone). It is read on its own thread, so a
    slow network never stalls the bell.
    """
    CHUNK = int(PLAYBACK_RATE * 0.05)
    END = object()
    q: queue.Queue = queue.Queue()

    def produce() -> None:
        reverb = ReverseReverb()
        try:
            for pcm in speech:
                if any(e.is_set() for e in stops):
                    return
                q.put(reverb.feed(pcm))
        except Exception as e:
            print(f"  [voice stream broke off: {type(e).__name__}: {e}]")
        q.put(reverb.finish())
        q.put(END)

    voice_done = speech is None
    if not voice_done:
        threading.Thread(target=produce, daemon=True).start()

    bed = bed if bed is not None else np.zeros(0, dtype="float32")
    offset = int(voice_offset_secs * PLAYBACK_RATE)
    voice = np.zeros(0, dtype="float32")
    written = 0

    def drain(block: bool) -> None:
        nonlocal voice, voice_done
        try:
            while True:
                item = q.get(timeout=0.05) if block else q.get_nowait()
                block = False
                if item is END:
                    voice_done = True
                    return
                voice = np.concatenate([voice, item])
        except queue.Empty:
            pass

    try:
        with sd.OutputStream(samplerate=PLAYBACK_RATE, device=MAIN_OUTPUT,
                             channels=1, dtype="float32") as stream:
            while not any(e.is_set() for e in stops):
                bed_left = written < len(bed)
                voice_due = written >= offset and not (voice_done and len(voice) == 0)
                if not bed_left and not voice_due:
                    break
                drain(block=False)
                if voice_due and len(voice) < CHUNK and not voice_done and not bed_left:
                    # Nothing else to play: wait for the voice rather than write silence
                    drain(block=True)
                    continue
                frame = np.zeros(CHUNK, dtype="float32")
                part = bed[written: written + CHUNK]
                frame[:len(part)] += part
                if voice_due:
                    take = voice[:CHUNK]
                    frame[:len(take)] += take
                    voice = voice[len(take):]
                stream.write(np.clip(frame, -1.0, 1.0).reshape(-1, 1))
                written += CHUNK
    except Exception as e:
        # A dead speaker must not end the session — the panel still carries the words.
        print(f"  [audio/playback error: {e}]")
