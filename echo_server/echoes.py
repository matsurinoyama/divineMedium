"""
The entity's echo, ported from tuhanKami: broken fragments of each utterance,
spoken in the seeker's cloned voice (unstable, slow), pitched down, reverberated,
swelled in through the reverse reverb, and thrown to one of the surrounding
speakers at random. Every new echo dims the ones still sounding, so old echoes
fade as new ones arrive. Beneath them, always, the cave: CAVESOUNDS.mp3 looping
on every speaker, between sessions too. Runs on the Mac; the Pi only sends the words.
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

# tuhanKami's echo voice settings — here applied to the seeker's own clone, not a remix
ECHO_VOICE_SETTINGS = VoiceSettings(
    stability=0.25,
    similarity_boost=0.9,
    style=0.7,
    speed=0.7,
    use_speaker_boost=True,
)

# Languages written without spaces, where a word budget means nothing
CHARACTER_COUNTED = {"ja", "zh", "ko"}


# ── Session log ───────────────────────────────────────────────────────────────
# What happened to each echo, kept for GET /status: the Pi prints it at the end
# of every session, so a failure on the Mac shows up in the Pi's log too.
LOG: collections.deque[str] = collections.deque(maxlen=60)


def note(message: str) -> None:
    print(f"  [{message}]")
    LOG.append(f"{time.strftime('%H:%M:%S')} {message}")


# ── Speakers ──────────────────────────────────────────────────────────────────

Output = tuple[int | str | None, tuple[int, ...], int]   # device, its channels, channels on the stream


def parse_outputs(spec: str | None) -> list[Output]:
    """
    ECHO_OUTPUTS as "device:channel:channels" entries, comma-separated — one entry
    per speaker. E.g. one 4-channel interface: "5:0:4,5:1:4,5:2:4,5:3:4", or two
    stereo devices: "6:0:2,6:1:2,4:0:2,4:1:2". Channels joined with + make one
    speaker of several: "4:0+1:2" sends the same echo to both sides of device 4.
    Device is an index or a name. Unset: the default device's left and right,
    for trying it out on the laptop.
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
# The room's ground: a long cave recording, looping beneath the echoes for as long
# as the server runs. The recording is mono, so each speaker gets its own decoder,
# started at its own random point in it: the speakers play different stretches of
# the cave, and the room sounds wide rather than one sound from the middle.

HERE = os.path.dirname(os.path.abspath(__file__))
CAVE_FILE = os.getenv("CAVE_FILE") or os.path.join(os.path.dirname(HERE), "CAVESOUNDS.mp3")
CAVE_VOLUME = float(os.getenv("CAVE_VOLUME", "1.0"))   # 1.0: as recorded; 0: no cave
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
    """CAVE_FILE decoded by ffmpeg, endlessly, as mono float samples — one speaker's."""

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
        # asetpts: fresh timestamps, so the loop back to the start is seamless
        self.proc = subprocess.Popen(
            ["ffmpeg", "-nostdin", "-v", "error", "-stream_loop", "-1",
             "-ss", f"{offset:.1f}", "-i", CAVE_FILE, "-af", "asetpts=N/SR/TB",
             "-f", "f32le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        note(f"cave on {self.label} from {int(offset) // 60}:{int(offset) % 60:02d}")

    def read(self, frames: int) -> np.ndarray | None:
        # frames samples, at CAVE_VOLUME; None if there is no cave to play.
        if self.dead:
            return None
        if self.proc is None:
            self.open()
        need = frames * 4
        data = self.proc.stdout.read(need)
        if len(data) < need:
            # ffmpeg ended: start again elsewhere, unless it keeps failing
            self.close()
            self.failures += 1
            if self.failures >= 3:
                self.dead = True
                note(f"cave on {self.label} stopped: ffmpeg could not play {CAVE_FILE}")
            return np.zeros(frames, dtype="float32")
        self.failures = 0
        return np.frombuffer(data, dtype="float32") * CAVE_VOLUME

    def close(self) -> None:
        if self.proc is not None:
            self.proc.kill()
            self.proc.wait()
            self.proc = None


# ── Processing (tuhanKami's audio.py, scaled from 16 kHz to SAMPLE_RATE) ─────

def scaled(samples_at_16k: int) -> int:
    return int(samples_at_16k * SAMPLE_RATE / 16000)


def pitch_shift(audio: np.ndarray, semitones: float = -2.0) -> np.ndarray:
    # Shift pitch by resampling: stretch then compress back to original length.
    factor = 2.0 ** (semitones / 12.0)
    stretched = np.asarray(sp_resample(audio, int(len(audio) / factor)))
    return np.asarray(sp_resample(stretched, len(audio)), dtype="float32")


def add_reverb(audio: np.ndarray, delay_samples: int, decay: float, echoes: int) -> np.ndarray:
    # Simple delay-line reverb: sum N copies of the signal, each delayed and attenuated.
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
    # Swell-in / fade-out: reverb on the reversed signal, so it bleeds into the speech.
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


# Every echo is brought to this peak, whatever the clone's own level: a seeker
# recorded softly makes a soft clone, whose echoes were ~12 dB under the test voice
# (speaker_test.py) the speakers were set by — and under the cave's peaks.
ECHO_PEAK = float(os.getenv("ECHO_PEAK", "0.3"))
MAX_BOOST = 10.0   # +20 dB at most: a near-silent clip is not blown up into noise


def process(pcm: bytes, volume: float = 0.65) -> np.ndarray:
    # Raw PCM → the echo as it is heard: pitched down, reverberated, swelled in.
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    audio = add_reverb(pitch_shift(audio), scaled(800), 0.38, 5)
    audio = reverse_reverb(audio * volume)
    peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
    if peak > 0:
        audio *= min(ECHO_PEAK / peak, MAX_BOOST)
    return audio


# ── Playback (tuhanKami's SpeakerPlayer / BackgroundEchoPlayer) ──────────────

CLEAR = "clear"   # queue sentinel: drop every layer (None dims them)

# PortAudio lists the devices once, when it starts, so a speaker plugged in (or
# woken) after the server started is invisible to it until it starts again. It
# can only be restarted while no stream is open — this keeps count.
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
    One output stream per device, its channels mixed independently. Each clip
    loops until new clips have dimmed it below VOL_THRESHOLD. The cave plays
    beneath — one Cave per speaker — untouched by the echoes' master volume.
    """

    CHUNK = int(SAMPLE_RATE * 0.05)
    MAX_LAYERS = 4
    VOL_THRESHOLD = 0.05

    def __init__(self, device: int | str | None, num_channels: int, master: list,
                 speakers: list[tuple[int, ...]] | None = None) -> None:
        self.device = device
        self.num_channels = num_channels
        self.master = master
        # One cave per speaker on this device, played on all of that speaker's channels
        self.caves = [(Cave(f"{device!r} ch {'+'.join(map(str, chs))}"), chs)
                      for chs in (speakers or [])]
        self.pending: list[queue.Queue] = [queue.Queue() for _ in range(num_channels)]
        self.layers: list[list[dict]] = [[] for _ in range(num_channels)]
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def feed(self, channel: int, audio: np.ndarray) -> None:
        self.pending[channel].put(audio)

    def dim_all(self) -> None:
        # None sentinel: dim every layer on every channel to 40%.
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
        # A device can refuse to open (a USB adapter waking up, two opened at once),
        # be missing (unplugged), or drop out mid-session: keep trying, so one
        # speaker is not lost for good. Each new kind of error is said once.
        last_error = None
        stuck = False   # missing, and PortAudio cannot be restarted to look again
        while not self.stop_event.is_set():
            try:
                self.play()
                last_error, stuck = None, False
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
                if error != last_error:
                    note(f"speaker {self.device!r} error — retrying: {error}")
                last_error = error
                if "No output device matching" not in str(e):
                    self.stop_event.wait(1.0)
                    continue
                # Not there: look again every few seconds, by restarting PortAudio —
                # unless another speaker is playing, which a restart would cut off.
                if self.stop_event.wait(3.0):
                    break
                if not _rescan_devices() and not stuck:
                    note(f"speaker {self.device!r} not found while the others play — "
                         "plug it in, then restart the server")
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
            stream.close()   # also when start() failed, which skips the with's exit
            with _portaudio:
                _open_streams -= 1

    def _play(self, stream: sd.OutputStream) -> None:
        note(f"speaker {self.device!r} open ({self.num_channels} ch)")
        opened = time.monotonic()
        while not self.stop_event.is_set():
            frame = np.zeros((self.CHUNK, self.num_channels), dtype="float32")
            # The cave, fading in whenever the stream (re)opens rather than clicking on
            cave_gain = min(1.0, (time.monotonic() - opened) / CAVE_FADE_IN_SECS)
            beneath = np.zeros((self.CHUNK, self.num_channels), dtype="float32")
            for cave, chs in self.caves:
                sound = cave.read(self.CHUNK)
                if sound is not None:
                    for c in chs:
                        beneath[:, c] += sound * cave_gain
            for ch in range(self.num_channels):
                try:
                    while True:
                        item = self.pending[ch].get_nowait()
                        if item is None:
                            for layer in self.layers[ch]:
                                layer["vol"] *= 0.4
                            self.layers[ch] = [l for l in self.layers[ch]
                                               if l["vol"] >= self.VOL_THRESHOLD]
                        elif isinstance(item, str):   # CLEAR
                            self.layers[ch] = []
                        else:
                            self.layers[ch].append({"audio": item, "pos": 0, "vol": 1.0})
                            self.layers[ch] = self.layers[ch][-self.MAX_LAYERS:]
                except queue.Empty:
                    pass
                mixed = np.zeros(self.CHUNK, dtype="float32")
                for layer in self.layers[ch]:
                    pos = layer["pos"]
                    end = min(pos + self.CHUNK, len(layer["audio"]))
                    chunk = layer["audio"][pos:end]
                    if len(chunk) < self.CHUNK:
                        chunk = np.pad(chunk, (0, self.CHUNK - len(chunk)))
                    mixed += chunk * layer["vol"]
                    layer["pos"] = end if end < len(layer["audio"]) else 0
                mixed *= self.master[0]
                mixed += beneath[:, ch]   # the echo is played over the cave
                peak = float(np.max(np.abs(mixed)))
                if peak > 1.0:
                    mixed /= peak
                frame[:, ch] = mixed
            stream.write(frame)


class EchoPlayer:
    """
    All ECHO_OUTPUTS, one SpeakerPlayer per device, with a shared master volume
    for the echoes. Opened once, for as long as the server runs: the cave plays
    throughout, and each session's echoes come and go over it.
    """

    def __init__(self) -> None:
        self.master: list = [1.0]
        self.fading = threading.Event()
        self.generation = 0   # bumped by reset(): an older fade stops touching master
        self.lock = threading.Lock()
        speakers: dict[tuple, list[tuple[int, ...]]] = {}
        for dev, chs, nch in ECHO_OUTPUTS:
            speakers.setdefault((dev, nch), []).append(chs)
        players = {key: SpeakerPlayer(key[0], key[1], self.master, chs)
                   for key, chs in speakers.items()}
        self.players = players
        self.slots = [(players[(dev, nch)], chs) for dev, chs, nch in ECHO_OUTPUTS]
        if not Cave.available():
            note(f"no cave (CAVE_VOLUME={CAVE_VOLUME}, file {CAVE_FILE} "
                 f"{'found' if os.path.isfile(CAVE_FILE) else 'missing'}, "
                 f"ffmpeg {'found' if shutil.which('ffmpeg') else 'missing'})")

    def reset(self) -> None:
        # A new session: any echo still sounding is dropped, and new ones play at full volume.
        with self.lock:
            self.generation += 1
            self.fading.clear()
            for player in self.players.values():
                player.clear()
            self.master[0] = 1.0

    def feed(self, audio: np.ndarray) -> None:
        # Dim everything already sounding, then send the new echo to one speaker at random.
        for player in self.players.values():
            player.dim_all()
        player, channels = random.choice(self.slots)
        for ch in channels:
            player.feed(ch, audio)
        peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
        note(f"echo sent to {player.device!r} ch {'+'.join(map(str, channels))} — "
             f"{len(audio) / SAMPLE_RATE:.1f}s, peak {peak:.2f}, master {self.master[0]:.2f}")

    def fade_out(self, secs: float) -> None:
        # Fade the echoes to silence over secs, in the background, then drop them.
        # The cave plays on.
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
                        return   # a new session has begun
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


# ── Words and voice ───────────────────────────────────────────────────────────

TAG_RE = re.compile(r"\[[^\]\n]{1,40}\]")
MARKDOWN_RE = re.compile(r"^#+\s*", re.MULTILINE)


def echo_text(client: anthropic.Anthropic, utterance: str, language: str) -> str:
    """
    tuhanKami's ask_echo: the utterance shattered into looping fragments, like an
    intrusive thought, about 15% of its length — counted in characters for Japanese.
    """
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
    # The echo in the seeker's cloned voice, whole (it is processed as one clip).
    return b"".join(client.text_to_speech.convert(
        voice_id=voice_id,
        text=text,
        model_id=TTS_MODEL,
        output_format=OUTPUT_FORMAT,
        voice_settings=ECHO_VOICE_SETTINGS,
    ))
