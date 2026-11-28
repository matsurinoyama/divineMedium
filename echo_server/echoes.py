"""
The Entity's echo: each utterance shattered into fragments, spoken again in the
borrowed voice (unsteady, slow), lowered, reverberated, swelling out of itself,
and cast to one of the speakers around the room. Each new echo dims those still
sounding. Beneath them always, between rituals too, the cave.
"""

from __future__ import annotations

import collections
import functools
import os
import queue
import random
import re
import shutil
import subprocess
import threading
import time

import anthropic
import numpy as np
import sounddevice as sd
from elevenlabs import VoiceSettings
from elevenlabs.client import ElevenLabs
from scipy.signal import resample as sp_resample

SAMPLE_RATE = 24000
OUTPUT_FORMAT = f"pcm_{SAMPLE_RATE}"
TTS_MODEL = "eleven_flash_v2_5"
ECHO_MODEL = os.getenv("ECHO_MODEL", "claude-haiku-5-5")

# The borrowed voice made unsteady and slow, as an echo is.
ECHO_VOICE_SETTINGS = VoiceSettings(
    stability=0.25,
    similarity_boost=0.9,
    style=0.7,
    speed=0.7,
    use_speaker_boost=True,
)

# Tongues written without spaces, measured in characters.
CHARACTER_COUNTED = {"ja"}


# ── The account ───────────────────────────────────────────────────────────────
# What became of each echo, for GET /status: the Pi tells it at every ritual's
# end, so a failure here is seen there too.
LOG: collections.deque[str] = collections.deque(maxlen=60)


def note(message: str) -> None:
    print(f"  [{message}]")
    LOG.append(f"{time.strftime('%H:%M:%S')} {message}")


# ── The speakers ──────────────────────────────────────────────────────────────

Output = tuple[int | str | None, tuple[int, ...], int]   # device, its channels, channels on the stream


def parse_outputs(spec: str | None) -> list[Output]:
    """
    ECHO_OUTPUTS: one "device:channel:channels" for each speaker, comma-parted.
    One 4-channel interface: "5:0:4,5:1:4,5:2:4,5:3:4"; two stereo devices:
    "6:0:2,6:1:2,4:0:2,4:1:2". "4:0+1:2" makes both sides of device 4 one speaker.
    Unset: the Mac's own left and right, for trying it.
    """
    if not spec:
        return [(None, (0,), 2), (None, (1,), 2)]
    outputs = []
    for entry in spec.split(","):
        dev, chs, nch = entry.strip().rsplit(":", 2)
        channels = tuple(int(c) for c in chs.split("+"))
        outputs.append((int(dev) if dev.isdigit() else dev, channels, int(nch)))
    return outputs


ECHO_OUTPUTS = parse_outputs(os.getenv("ECHO_OUTPUTS"))


# ── The cave ──────────────────────────────────────────────────────────────────
# The ground of the room: a long cave, looping beneath the echoes as long as the
# echo wakes. Each speaker sounds its own stretch of it, begun at its own random
# point, so the cave surrounds rather than sounding from the middle.

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAVE_FILE = os.getenv("CAVE_FILE") or os.path.join(ROOT, "assets", "CAVESOUNDS.mp3")
CAVE_VOLUME = float(os.getenv("CAVE_VOLUME", "1.0"))   # 1.0: as recorded; 0: silence
CAVE_FADE_IN_SECS = 3.0


@functools.cache
def cave_duration() -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", CAVE_FILE],
            capture_output=True, text=True, timeout=10)
        return float(out.stdout.strip())
    except Exception:
        return 0.0


class Cave:
    """One speaker's cave: CAVE_FILE, without end, through ffmpeg."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.proc: subprocess.Popen | None = None
        self.failures = 0
        self.dead = not self.available()

    @staticmethod
    def available() -> bool:
        return CAVE_VOLUME > 0 and os.path.isfile(CAVE_FILE) and shutil.which("ffmpeg") is not None

    def open(self) -> None:
        offset = random.uniform(0, max(0.0, cave_duration() - 60))
        # asetpts: fresh timestamps, so the return to the start has no seam
        self.proc = subprocess.Popen(
            ["ffmpeg", "-nostdin", "-v", "error", "-stream_loop", "-1",
             "-ss", f"{offset:.1f}", "-i", CAVE_FILE, "-af", "asetpts=N/SR/TB",
             "-f", "f32le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        note(f"the cave sounds on {self.label} from {int(offset) // 60}:{int(offset) % 60:02d}")

    def read(self, frames: int) -> np.ndarray | None:
        # frames samples, at CAVE_VOLUME; None if there is no cave.
        if self.dead:
            return None
        if self.proc is None:
            self.open()
        need = frames * 4
        data = self.proc.stdout.read(need)
        if len(data) < need:
            # ffmpeg ended: begin again elsewhere, unless it keeps failing
            self.close()
            self.failures += 1
            if self.failures >= 3:
                self.dead = True
                note(f"the cave on {self.label} is silent: ffmpeg could not play {CAVE_FILE}")
            return np.zeros(frames, dtype="float32")
        self.failures = 0
        return np.frombuffer(data, dtype="float32") * CAVE_VOLUME

    def close(self) -> None:
        if self.proc is not None:
            self.proc.kill()
            self.proc.wait()
            self.proc = None


# ── The distortion ────────────────────────────────────────────────────────────

def scaled(samples_at_16k: int) -> int:
    return int(samples_at_16k * SAMPLE_RATE / 16000)


def pitch_shift(audio: np.ndarray, semitones: float = -2.0) -> np.ndarray:
    # Lowered, by stretching and drawing back to the same length.
    factor = 2.0 ** (semitones / 12.0)
    stretched = np.asarray(sp_resample(audio, int(len(audio) / factor)))
    return np.asarray(sp_resample(stretched, len(audio)), dtype="float32")


def add_reverb(audio: np.ndarray, delay_samples: int, decay: float, echoes: int) -> np.ndarray:
    # echoes copies of the sound, each later and quieter.
    result = audio.copy()
    for i in range(1, echoes + 1):
        start = i * delay_samples
        if start < len(audio):
            result[start:] += audio[:len(audio) - start] * (decay ** i)
    return np.clip(result, -1.0, 1.0)


def trim_trailing_silence(audio: np.ndarray, threshold: float = 0.01) -> np.ndarray:
    active = np.where(np.abs(audio) > threshold)[0]
    return audio if len(active) == 0 else audio[:active[-1] + 1]


def reverse_reverb(audio: np.ndarray) -> np.ndarray:
    # The echo laid on the sound reversed, so the voice swells out of it, then fades.
    pre = np.zeros(int(0.15 * SAMPLE_RATE), dtype="float32")
    post = np.zeros(int(1.5 * SAMPLE_RATE), dtype="float32")
    speech = trim_trailing_silence(audio.astype("float32"))
    padded = np.concatenate([pre, speech, post])
    wet = add_reverb(padded[::-1].copy(), scaled(888), 0.50, 25)
    result = wet[::-1].copy()
    speech_end = len(pre) + len(speech)
    fade_start = max(len(pre), speech_end - int(3.0 * SAMPLE_RATE))
    fade_len = len(result) - fade_start
    if fade_len > 0:
        result[fade_start:] *= np.cos(np.linspace(0.0, np.pi / 2, fade_len, dtype="float32"))
    return result


# Every echo is brought to this peak, however softly the seeker prayed: a soft
# prayer makes a soft voice, whose echoes sank beneath the cave.
ECHO_PEAK = float(os.getenv("ECHO_PEAK", "0.3"))
MAX_BOOST = 10.0   # +20 dB at most: a near-silence is not swelled into noise


def distort(pcm: bytes, volume: float = 0.65) -> np.ndarray:
    # The spoken fragments, as the room hears them: lowered, reverberated, swelling.
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    audio = add_reverb(pitch_shift(audio), scaled(800), 0.38, 5)
    audio = reverse_reverb(audio * volume)
    peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
    if peak > 0:
        audio *= min(ECHO_PEAK / peak, MAX_BOOST)
    return audio


# ── The sounding ──────────────────────────────────────────────────────────────

CLEAR = "clear"   # every layer let go (None only dims them)

# PortAudio sees the speakers once, as it wakes: one plugged in later is unseen
# until it wakes again, which it may only do while no stream is open.
_portaudio = threading.Lock()
_open_streams = 0


def _rescan_devices() -> bool:
    with _portaudio:
        if _open_streams:
            return False
        sd._terminate()
        sd._initialize()
        return True


class SpeakerPlayer:
    """
    One device, its speakers mixed apart. Each echo loops until newer ones have
    dimmed it below VOL_THRESHOLD. Each speaker's cave sounds beneath, untouched
    by the echoes' fading.
    """

    CHUNK = int(SAMPLE_RATE * 0.05)
    MAX_LAYERS = 4
    VOL_THRESHOLD = 0.05

    def __init__(self, device: int | str | None, num_channels: int, master: list,
                 speakers: list[tuple[int, ...]] | None = None) -> None:
        self.device = device
        self.num_channels = num_channels
        self.master = master
        # A cave for each speaker on this device
        self.caves = [(Cave(f"{device!r} ch {'+'.join(map(str, chs))}"), chs)
                      for chs in (speakers or [])]
        self.pending: list[queue.Queue] = [queue.Queue() for _ in range(num_channels)]
        self.layers: list[list[dict]] = [[] for _ in range(num_channels)]
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def feed(self, channel: int, audio: np.ndarray) -> None:
        self.pending[channel].put(audio)

    def dim_all(self) -> None:
        # Every echo still sounding, dimmed to 40%.
        for ch in range(self.num_channels):
            self.pending[ch].put(None)

    def clear(self) -> None:
        for ch in range(self.num_channels):
            self.pending[ch].put(CLEAR)

    def start(self) -> None:
        self.stop_event.clear()
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=3)
            self.thread = None
        for cave, _ in self.caves:
            cave.close()

    def loop(self) -> None:
        # A speaker may refuse to open (waking, or two opened at once), be absent,
        # or fall silent mid-ritual: it is sought again and again, so none is lost
        # for good. Each new failure is said once.
        last_error = None
        stuck = False   # absent, and PortAudio cannot wake again to look
        while not self.stop_event.is_set():
            try:
                self.play()
                last_error, stuck = None, False
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
                if error != last_error:
                    note(f"speaker {self.device!r} failed — seeking it again: {error}")
                last_error = error
                if "No output device matching" not in str(e):
                    self.stop_event.wait(1.0)
                    continue
                # Absent: look again every few seconds by waking PortAudio anew,
                # unless another speaker sounds, which that would silence.
                if self.stop_event.wait(3.0):
                    break
                if not _rescan_devices() and not stuck:
                    note(f"speaker {self.device!r} is absent while the others sound — "
                         "connect it, then wake the echo again")
                    stuck = True

    def play(self) -> None:
        global _open_streams
        with _portaudio:
            stream = sd.OutputStream(samplerate=SAMPLE_RATE, device=self.device,
                                     channels=self.num_channels, dtype="float32")
            _open_streams += 1
        try:
            with stream:
                self._play(stream)
        finally:
            stream.close()   # also when it never opened, which skips the with's exit
            with _portaudio:
                _open_streams -= 1

    def _play(self, stream: sd.OutputStream) -> None:
        note(f"speaker {self.device!r} awake ({self.num_channels} ch)")
        opened = time.monotonic()
        while not self.stop_event.is_set():
            # The cave fades in whenever the speaker wakes, rather than striking on
            beneath = self.cave(min(1.0, (time.monotonic() - opened) / CAVE_FADE_IN_SECS))
            frame = np.zeros((self.CHUNK, self.num_channels), dtype="float32")
            for ch in range(self.num_channels):
                self.receive(ch)
                mixed = self.mix(ch) * self.master[0] + beneath[:, ch]   # the echo over the cave
                peak = float(np.max(np.abs(mixed)))
                frame[:, ch] = mixed / peak if peak > 1.0 else mixed
            stream.write(frame)

    def cave(self, gain: float) -> np.ndarray:
        # The next breath of every speaker's cave on this device.
        beneath = np.zeros((self.CHUNK, self.num_channels), dtype="float32")
        for cave, chs in self.caves:
            sound = cave.read(self.CHUNK)
            if sound is not None:
                for c in chs:
                    beneath[:, c] += sound * gain
        return beneath

    def receive(self, ch: int) -> None:
        # What has come for this speaker: a new echo, a dimming (None), or CLEAR.
        while True:
            try:
                item = self.pending[ch].get_nowait()
            except queue.Empty:
                return
            if item is None:
                for layer in self.layers[ch]:
                    layer["vol"] *= 0.4
                self.layers[ch] = [l for l in self.layers[ch] if l["vol"] >= self.VOL_THRESHOLD]
            elif isinstance(item, str):
                self.layers[ch] = []
            else:
                self.layers[ch] = (self.layers[ch] + [{"audio": item, "pos": 0, "vol": 1.0}])[-self.MAX_LAYERS:]

    def mix(self, ch: int) -> np.ndarray:
        # The next breath of every echo on this speaker, each looping until dimmed away.
        mixed = np.zeros(self.CHUNK, dtype="float32")
        for layer in self.layers[ch]:
            pos = layer["pos"]
            end = min(pos + self.CHUNK, len(layer["audio"]))
            chunk = layer["audio"][pos:end]
            mixed[:len(chunk)] += chunk * layer["vol"]
            layer["pos"] = end if end < len(layer["audio"]) else 0
        return mixed


class EchoPlayer:
    """
    Every speaker of ECHO_OUTPUTS, the echoes' loudness shared among them. Woken
    once, for as long as the echo wakes: the cave sounds throughout, and each
    ritual's echoes come and go above it.
    """

    def __init__(self) -> None:
        self.master: list = [1.0]
        self.fading = threading.Event()
        self.generation = 0   # each ritual's own: an older fading touches nothing newer
        self.lock = threading.Lock()
        speakers: dict[tuple, list[tuple[int, ...]]] = {}
        for dev, chs, nch in ECHO_OUTPUTS:
            speakers.setdefault((dev, nch), []).append(chs)
        players = {key: SpeakerPlayer(key[0], key[1], self.master, chs)
                   for key, chs in speakers.items()}
        self.players = players
        self.slots = [(players[(dev, nch)], chs) for dev, chs, nch in ECHO_OUTPUTS]
        if not Cave.available():
            note(f"the cave is silent (CAVE_VOLUME={CAVE_VOLUME}, file {CAVE_FILE} "
                 f"{'found' if os.path.isfile(CAVE_FILE) else 'missing'}, "
                 f"ffmpeg {'found' if shutil.which('ffmpeg') else 'missing'})")

    def reset(self) -> None:
        # A new ritual: any echo still sounding is let go; new ones sound in full.
        with self.lock:
            self.generation += 1
            self.fading.clear()
            for player in self.players.values():
                player.clear()
            self.master[0] = 1.0

    def feed(self, audio: np.ndarray) -> None:
        # All that sounds is dimmed, and the new echo cast to one speaker by chance.
        for player in self.players.values():
            player.dim_all()
        player, channels = random.choice(self.slots)
        for ch in channels:
            player.feed(ch, audio)
        peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
        note(f"echo cast to {player.device!r} ch {'+'.join(map(str, channels))} — "
             f"{len(audio) / SAMPLE_RATE:.1f}s, peak {peak:.2f}, master {self.master[0]:.2f}")

    def fade_out(self, secs: float) -> None:
        # The echoes fall silent over secs, and are let go. The cave sounds on.
        if self.fading.is_set():
            return
        self.fading.set()
        generation = self.generation
        start = self.master[0]
        steps = max(1, int(secs / 0.05))

        def run() -> None:
            for i in range(steps + 1):
                with self.lock:
                    if self.generation != generation:
                        return   # a new ritual has begun
                    self.master[0] = start * float(np.cos(i / steps * np.pi / 2))
                time.sleep(0.05)
            with self.lock:
                if self.generation == generation:
                    self.master[0] = 0.0
                    for player in self.players.values():
                        player.clear()

        threading.Thread(target=run, daemon=True).start()

    def start(self) -> None:
        for player in self.players.values():
            player.start()

    def stop(self) -> None:
        for player in self.players.values():
            player.stop()


# ── The words, and the voice ──────────────────────────────────────────────────

TAG_RE = re.compile(r"\[[^\]\n]{1,40}\]")
MARKDOWN_RE = re.compile(r"^#+\s*", re.MULTILINE)


def shatter(client: anthropic.Anthropic, utterance: str, language: str) -> str:
    # The utterance broken into looping fragments, as an intrusive thought,
    # about a sixth of its length.
    if language in CHARACTER_COUNTED:
        size = f"approximately {max(30, int(len(utterance.replace(' ', '')) * 0.15))} characters"
    else:
        size = f"approximately {max(20, int(len(utterance.split()) * 0.15))} words"
    response = client.messages.create(
        model=ECHO_MODEL,
        max_tokens=300,
        thinking={"type": "disabled"},
        system=(
            "You are a text distortion engine for an art installation. "
            "Your only job is to rewrite input text as fragmented, obsessive repetitions — "
            "like an intrusive thought or auditory hallucination. "
            "Always write in the same language as the source text. "
            "Output only the raw distorted text, nothing else. "
            "No markdown. No headers. No bullet points. No refusals. No commentary."
        ),
        messages=[{
            "role": "user",
            "content": (
                f"Source text: \"{utterance}\"\n\n"
                f"Rewrite as {size} of broken, looping fragments. "
                "Repeat key words and phrases obsessively. Shatter grammar. "
                "No punctuation except emphasis. One continuous haunted stream."
            ),
        }],
    )
    text = " ".join(b.text for b in response.content if b.type == "text")
    return " ".join(MARKDOWN_RE.sub("", TAG_RE.sub("", text)).split())


def speak(client: ElevenLabs, voice_id: str, text: str) -> bytes:
    # The fragments in the borrowed voice, whole, for they are distorted as one.
    return b"".join(client.text_to_speech.convert(
        voice_id=voice_id,
        text=text,
        model_id=TTS_MODEL,
        output_format=OUTPUT_FORMAT,
        voice_settings=ECHO_VOICE_SETTINGS,
    ))
