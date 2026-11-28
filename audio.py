"""
What the Entity hears and how it sounds: the prayer and the questions received
from the microphone, the borrowed voice swelling out of its own echo, the bells.
"""

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

# A question is heard at 16 kHz, as speech recognition wishes. The prayer at
# 44.1 kHz, for the voice is borrowed from it and must be heard in full.
SAMPLE_RATE = 16000
PRAYER_SAMPLE_RATE = 44100
# All that sounds — the borrowed voice and the bells — sounds at this rate.
PLAYBACK_RATE = 24000

# The silence and the speech: the room's own murmur is learned first, then speech
# is awaited, then heard until the seeker has paused long enough.
VAD_CAL_CHUNKS = 20     # ~1 s of the room, to learn its murmur
VAD_MULTIPLIER = 2      # speech is this much louder than the murmur
VAD_ONSET_CHUNKS = 2    # loud chunks in a row before speech is believed
VAD_SILENCE_SECS = 1.5  # the pause that ends a question
VAD_PREROLL_CHUNKS = 6  # ~0.3 s kept from before, so a soft first syllable is not lost

# A press right after a recording begins is the press that began it, and ends nothing.
START_GRACE_SECS = 0.6

BELLS = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "bells", f"Bonsho04-{n}.mp3")
    for n in range(1, 5)
]


def _device(name: str) -> int | str | None:
    # A sounddevice index or name from the environment; unset, the system's own.
    value = os.getenv(name) or None
    return int(value) if value and value.isdigit() else value


SPEECH_INPUT = _device("SPEECH_INPUT")
MAIN_OUTPUT = _device("MAIN_OUTPUT")


def to_pcm16(chunk: np.ndarray) -> bytes:
    return (np.clip(chunk, -1.0, 1.0) * 32767).astype("<i2").tobytes()


def pcm_to_float(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0


def is_silent(filename: str, threshold: float = 0.001) -> bool:
    data, _ = sf.read(filename)
    return float(np.abs(data).mean()) < threshold


def resample(audio: np.ndarray, rate: int, target: int) -> np.ndarray:
    if rate == target:
        return audio
    return np.asarray(sp_resample(audio, int(len(audio) * target / rate)), dtype="float32")


def compress_for_ivc(src_wav: str, dst_wav: str, target_rate: int = 22050) -> None:
    # The prayer, made small enough to lend (ElevenLabs takes at most 11 MB).
    data, rate = sf.read(src_wav, dtype="float32")
    if data.ndim > 1:
        data = data.mean(axis=1)
    sf.write(dst_wav, resample(data, rate, target_rate), target_rate, subtype="PCM_16")


def trim_trailing_silence(audio: np.ndarray, threshold: float = 0.01) -> np.ndarray:
    active = np.where(np.abs(audio) > threshold)[0]
    return audio if len(active) == 0 else audio[:active[-1] + 1]


# ── Receiving ─────────────────────────────────────────────────────────────────


def receive_prayer(
    session: Session,
    filename: str,
    hear: Callable[[bytes], None] | None = None,
) -> bool:
    """
    The prayer, received from now until the seeker presses. Written to filename
    and True if they press; False, and nothing written, if the ritual is broken
    first. The press that ends it is spent, and opens nothing after it.
    hear, if given, is offered every chunk as 16-bit PCM as it is spoken.
    """
    chunks: list[np.ndarray] = []
    q: queue.Queue = queue.Queue()
    session.tap.clear()
    grace_until = time.monotonic() + START_GRACE_SECS

    tapped = False
    with sd.InputStream(samplerate=PRAYER_SAMPLE_RATE, device=SPEECH_INPUT, channels=1,
                        dtype="float32", blocksize=int(PRAYER_SAMPLE_RATE * 0.05),
                        callback=lambda indata, *_: q.put(indata.copy())):
        while not session.abort.is_set():
            if time.monotonic() < grace_until:
                session.tap.clear()
            elif session.tap.is_set():
                tapped = True
                break
            try:
                chunk = q.get(timeout=0.1)
            except queue.Empty:
                continue
            chunks.append(chunk)
            if hear is not None:
                hear(to_pcm16(chunk))
    session.tap.clear()

    if tapped and chunks:
        sf.write(filename, np.concatenate(chunks, axis=0), PRAYER_SAMPLE_RATE, subtype="PCM_16")
    return tapped


class SpeechGate:
    """
    The silence and the speech of one question: the room's murmur is learned
    first, then speech is awaited, then heard until the seeker pauses long enough.
    """

    def __init__(self) -> None:
        self.murmur: list[float] = []
        self.threshold = 0.001
        self.preroll: list[np.ndarray] = []
        self.onset = 0
        self.silence = 0
        self.speaking = False

    def learning(self, rms: float) -> bool:
        # True while the room's murmur is still being learned.
        if len(self.murmur) >= VAD_CAL_CHUNKS:
            return False
        self.murmur.append(rms)
        if len(self.murmur) == VAD_CAL_CHUNKS:
            self.threshold = max(float(np.mean(self.murmur)) * VAD_MULTIPLIER, 0.001)
        return True

    def opens(self, chunk: np.ndarray, rms: float) -> list[np.ndarray]:
        # The chunks to keep once speech begins (the soft breath before it too),
        # or nothing while the room is still silent.
        self.preroll = (self.preroll + [chunk])[-VAD_PREROLL_CHUNKS:]
        self.onset = self.onset + 1 if rms > self.threshold else 0
        self.speaking = self.onset >= VAD_ONSET_CHUNKS
        return self.preroll if self.speaking else []

    def closes(self, rms: float) -> bool:
        # True once the seeker has paused long enough.
        self.silence = 0 if rms > self.threshold else self.silence + 1
        return self.silence >= int(VAD_SILENCE_SECS / 0.05)


def _next_chunk(q: queue.Queue) -> np.ndarray | None:
    try:
        return q.get(timeout=0.1)
    except queue.Empty:
        return None


def _nothing() -> None:
    pass


def receive_question(
    session: Session,
    filename: str,
    on_start: Callable[[], None] = _nothing,
    on_end: Callable[[], None] = _nothing,
    hear: Callable[[bytes], None] | None = None,
    cut_in: Callable[[], bool] | None = None,
) -> bool:
    """
    One question, found by the silence around it (SpeechGate), and written to
    filename. True if the seeker ended the ritual instead (a double press),
    waiting or mid-question.

    It is heard until the seeker pauses, or until cut_in() says the Entity will not
    wait for the rest. on_start and on_end are called as speech begins and as it is
    taken; hear is offered every chunk, for the living ear.
    """
    q: queue.Queue = queue.Queue()
    chunks: list[np.ndarray] = []
    gate = SpeechGate()

    def take(kept: list[np.ndarray]) -> None:
        chunks.extend(kept)
        if hear is not None:
            for chunk in kept:
                hear(to_pcm16(chunk))

    print("  [The Entity listens...]")
    with sd.InputStream(samplerate=SAMPLE_RATE, device=SPEECH_INPUT, channels=1,
                        dtype="float32", blocksize=int(SAMPLE_RATE * 0.05),
                        callback=lambda indata, *_: q.put(indata.copy())):
        while not (session.end.is_set() or session.abort.is_set()):
            chunk = _next_chunk(q)
            if chunk is None:
                continue
            rms = float(np.sqrt(np.mean(chunk ** 2)))
            if gate.learning(rms):
                continue
            if not gate.speaking:
                kept = gate.opens(chunk, rms)
                if kept:
                    print("  [The seeker speaks...]")
                    on_start()
                    take(kept)
                continue
            take([chunk])
            if gate.closes(rms):
                break
            if cut_in is not None and cut_in():
                print("  [The Entity does not wait for the rest.]")
                break
        else:
            return session.end.is_set()

    on_end()
    sf.write(filename, np.concatenate(chunks, axis=0), SAMPLE_RATE, subtype="PCM_16")
    return False


# ── Sounding ──────────────────────────────────────────────────────────────────


class Dry:
    """The borrowed voice as it was given, unadorned: the farewell's."""

    TAIL_SECS = 0.5

    def __init__(self) -> None:
        self.carry = b""   # half a sample, left from the last chunk

    def take(self, pcm: bytes) -> np.ndarray:
        pcm = self.carry + pcm
        cut = len(pcm) - len(pcm) % 2
        self.carry = pcm[cut:]
        return pcm_to_float(pcm[:cut])

    def feed(self, pcm: bytes) -> np.ndarray:
        return self.take(pcm)

    def finish(self) -> np.ndarray:
        return np.zeros(int(self.TAIL_SECS * PLAYBACK_RATE), dtype="float32")


class ReverseReverb(Dry):
    """
    The voice swelling into each word out of its own echo, applied as the voice
    flows in, so it may begin before the whole utterance is made.

    Each sample is joined by the echoes of what comes after it, each quieter, so
    only ECHOES·delay (0.67 s) must be seen ahead. The last FADE_LEAD seconds and
    the post-pad fade away; holding back that much lets the fade fall without
    knowing in advance where the speech will end.
    """

    DELAY_SECS = 888 / 16000
    DECAY = 0.50
    ECHOES = 12           # beyond twelve, an echo is below 1/4000 of the voice
    PRE_PAD_SECS = 0.15   # room for the first word to swell
    POST_PAD_SECS = 1.5
    FADE_LEAD_SECS = 1.0

    def __init__(self) -> None:
        super().__init__()
        self.delay = int(self.DELAY_SECS * PLAYBACK_RATE)
        self.hold = max(self.ECHOES * self.delay, int(self.FADE_LEAD_SECS * PLAYBACK_RATE))
        self.pending = np.zeros(int(self.PRE_PAD_SECS * PLAYBACK_RATE), dtype="float32")

    def wet(self, x: np.ndarray, n: int) -> np.ndarray:
        # The first n samples of x, each joined by its echoes from later in x.
        y = x[:n].copy()
        for i in range(1, self.ECHOES + 1):
            ahead = x[i * self.delay: i * self.delay + n]
            y[:len(ahead)] += ahead * self.DECAY ** i
        return np.clip(y, -1.0, 1.0)

    def feed(self, pcm: bytes) -> np.ndarray:
        self.pending = np.concatenate([self.pending, self.take(pcm)])
        ready = len(self.pending) - self.hold
        if ready <= 0:
            return np.zeros(0, dtype="float32")
        out = self.wet(self.pending, ready)
        self.pending = self.pending[ready:]
        return out

    def finish(self) -> np.ndarray:
        # What remains once the voice is silent: its echoes, its fading, its tail.
        speech = trim_trailing_silence(self.pending)
        x = np.concatenate([speech, np.zeros(int(self.POST_PAD_SECS * PLAYBACK_RATE), dtype="float32")])
        y = self.wet(x, len(x))
        fade_start = max(0, len(speech) - int(self.FADE_LEAD_SECS * PLAYBACK_RATE))
        y[fade_start:] *= np.cos(np.linspace(0.0, np.pi / 2, len(y) - fade_start, dtype="float32"))
        return np.concatenate([y, super().finish()])


@functools.lru_cache(maxsize=None)
def load_bell(path: str) -> np.ndarray:
    # Kept once heard, for resampling is slow on the Pi. Do not alter what it returns.
    data, rate = sf.read(path, dtype="float32")
    if data.ndim > 1:
        data = data.mean(axis=1)
    return resample(data, rate, PLAYBACK_RATE)


def choose_bell() -> str:
    # One bell is chosen for each ritual, and tolls throughout it.
    return random.choice(BELLS)


def toll(bell: str, *, times: int = 1, gap_min: float = 0.0, gap_max: float = 0.0) -> np.ndarray:
    # The bell struck times over, each strike a random gap after the last.
    sound = load_bell(bell)
    step = int(np.random.uniform(gap_min, gap_max) * PLAYBACK_RATE)
    mixed = np.zeros(step * (times - 1) + len(sound), dtype="float32")
    for i in range(times):
        mixed[i * step: i * step + len(sound)] += sound
    peak = float(np.max(np.abs(mixed)))
    return mixed / peak if peak > 1.0 else mixed


def play(audio: np.ndarray, *stops: threading.Event) -> None:
    # Until done, or until any of stops. A silent speaker ends nothing: the panel
    # still bears the words.
    chunk = int(PLAYBACK_RATE * 0.05)
    try:
        with sd.OutputStream(samplerate=PLAYBACK_RATE, device=MAIN_OUTPUT,
                             channels=1, dtype="float32") as stream:
            for pos in range(0, len(audio), chunk):
                if _stopped(stops):
                    break
                stream.write(audio[pos: pos + chunk].reshape(-1, 1))
    except Exception as e:
        print(f"  [the speaker is silent: {e}]")


class VoiceFlow:
    """
    The borrowed voice as it flows in, drawn on a thread of its own through the
    reverse reverb (or dry), so a slow network never stills the bell. None: no
    voice at all.
    """

    END = object()

    def __init__(self, speech: Iterator[bytes] | None, stops: tuple[threading.Event, ...],
                 reverb: bool) -> None:
        self.q: queue.Queue = queue.Queue()
        self.audio = np.zeros(0, dtype="float32")
        self.done = speech is None
        if not self.done:
            threading.Thread(target=self.draw, args=(speech, stops, reverb), daemon=True).start()

    def draw(self, speech: Iterator[bytes], stops: tuple[threading.Event, ...], reverb: bool) -> None:
        effect = ReverseReverb() if reverb else Dry()
        try:
            for pcm in speech:
                if _stopped(stops):
                    return
                self.q.put(effect.feed(pcm))
        except Exception as e:
            print(f"  [the voice broke off: {type(e).__name__}: {e}]")
        self.q.put(effect.finish())
        self.q.put(self.END)

    def gather(self, block: bool = False) -> None:
        # All that has arrived; blocking, at least what arrives in the next 50 ms.
        try:
            while True:
                item = self.q.get(timeout=0.05) if block else self.q.get_nowait()
                block = False
                if item is self.END:
                    self.done = True
                    return
                self.audio = np.concatenate([self.audio, item])
        except queue.Empty:
            pass

    @property
    def spent(self) -> bool:
        return self.done and len(self.audio) == 0

    def starved(self, n: int) -> bool:
        # Less than n samples to hand, and more still to come.
        return not self.done and len(self.audio) < n

    def take(self, n: int) -> np.ndarray:
        taken, self.audio = self.audio[:n], self.audio[n:]
        return taken


def _stopped(stops: tuple[threading.Event, ...]) -> bool:
    return any(e.is_set() for e in stops)


def play_stream(
    speech: Iterator[bytes] | None,
    *stops: threading.Event,
    bed: np.ndarray | None = None,
    voice_offset_secs: float = 0.0,
    reverb: bool = True,
) -> None:
    """
    The borrowed voice (raw 16-bit PCM at PLAYBACK_RATE), sounded as it flows in,
    over bed (the greeting's bell), which sounds at once while the voice enters
    voice_offset_secs later, or later still if it is not yet ready. Until both
    are done, or until any of stops.
    """
    chunk = int(PLAYBACK_RATE * 0.05)
    flow = VoiceFlow(speech, stops, reverb)
    bed = bed if bed is not None else np.zeros(0, dtype="float32")
    offset = int(voice_offset_secs * PLAYBACK_RATE)
    written = 0
    try:
        with sd.OutputStream(samplerate=PLAYBACK_RATE, device=MAIN_OUTPUT,
                             channels=1, dtype="float32") as stream:
            while not _stopped(stops):
                bed_left = written < len(bed)
                voice_due = written >= offset and not flow.spent
                if not bed_left and not voice_due:
                    break
                flow.gather()
                if voice_due and not bed_left and flow.starved(chunk):
                    # Nothing else sounds: wait for the voice rather than sound silence
                    flow.gather(block=True)
                    continue
                frame = np.zeros(chunk, dtype="float32")
                part = bed[written: written + chunk]
                frame[:len(part)] += part
                if voice_due:
                    taken = flow.take(chunk)
                    frame[:len(taken)] += taken
                stream.write(np.clip(frame, -1.0, 1.0).reshape(-1, 1))
                written += chunk
    except Exception as e:
        print(f"  [the speaker is silent: {e}]")
