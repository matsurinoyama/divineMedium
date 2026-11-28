"""
A sound looped on the echo's speakers, to set each one's loudness. It sounds
through ECHO_OUTPUTS at the echo's own level, as a true echo, so what is set
here is what a ritual will sound like. Run it on the Mac.

  python tests/speaker_test.py

Then type a command and press Enter:
  1 2 3 …   solo that speaker, looping
  c         cycle: one speaker at a time, in turn (default)
  a         all speakers at once
  v         sound: the echo voice (default)
  p         sound: pink noise, at the same loudness (easier to match by ear or meter)
  q         quit
"""

from __future__ import annotations

import os
import threading
import time

import sys

from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "echo_server"))
load_dotenv(os.path.join(ROOT, ".env"))

import numpy as np
import sounddevice as sd
from elevenlabs.client import ElevenLabs

import echoes

VOICE_ID = os.getenv("TEST_VOICE_ID", "JOcmGzB8OFjY8MhjHHEf")   # JUN (Japanese), a stock voice
LINE = "汝の祈りは届いた。恐れるな。朝の光を待て。"
CYCLE_SECS = 4.0
CHUNK = int(echoes.SAMPLE_RATE * 0.05)

state = {"mode": "c", "sound": "v", "since": time.time()}
stop = threading.Event()


def make_sounds() -> dict[str, np.ndarray]:
    print("  [the voice is being shaped...]")
    eleven = ElevenLabs(api_key=os.environ["ELEVENLABS_API_KEY"])
    voice = echoes.distort(echoes.speak(eleven, VOICE_ID, LINE))
    voice = echoes.trim_trailing_silence(voice)
    voice = np.concatenate([voice, np.zeros(int(0.5 * echoes.SAMPLE_RATE), dtype="float32")])
    # Pink noise, as loud as the voice
    n = echoes.SAMPLE_RATE * 4
    spectrum = np.fft.rfft(np.random.randn(n))
    f = np.arange(len(spectrum))
    f[0] = 1
    pink = np.fft.irfft(spectrum / np.sqrt(f), n).astype("float32")
    rms = lambda x: float(np.sqrt(np.mean(x[np.abs(x) > 1e-4] ** 2)))
    pink *= rms(voice) / rms(pink)
    return {"v": voice, "p": np.clip(pink, -1, 1)}


def side(channels: tuple[int, ...]) -> str:
    return "+".join({0: "L", 1: "R"}.get(c, str(c)) for c in channels)


def active(slot: int) -> bool:
    mode = state["mode"]
    if mode == "a":
        return True
    if mode == "c":
        n = len(echoes.ECHO_OUTPUTS)
        return int((time.time() - state["since"]) / CYCLE_SECS) % n == slot
    return mode == str(slot + 1)


def run_device(device, nch: int, slots: list[tuple[int, tuple[int, ...]]], sounds: dict) -> None:
    # slots: (slot index, its channels) on this device
    pos = 0
    while not stop.is_set():
        try:
            with sd.OutputStream(samplerate=echoes.SAMPLE_RATE, device=device,
                                 channels=nch, dtype="float32") as stream:
                while not stop.is_set():
                    sound = sounds[state["sound"]]
                    idx = (np.arange(CHUNK) + pos) % len(sound)
                    pos = (pos + CHUNK) % len(sound)
                    frame = np.zeros((CHUNK, nch), dtype="float32")
                    for slot, channels in slots:
                        if active(slot):
                            for ch in channels:
                                frame[:, ch] = sound[idx]
                    stream.write(frame)
        except Exception as e:
            # A speaker waking, or two opened at once: again
            print(f"  [speaker {device} failed — again: {e}]")
            stop.wait(1.0)


def status() -> str:
    mode = {"c": f"cycling every {CYCLE_SECS:.0f}s", "a": "all at once"}.get(
        state["mode"], f"speaker {state['mode']} solo")
    return f"  [{mode} · {'echo voice' if state['sound'] == 'v' else 'pink noise'}]"


def announce() -> None:
    # In turn, each speaker named as it sounds
    last = None
    while not stop.is_set():
        if state["mode"] == "c":
            now = [i for i in range(len(echoes.ECHO_OUTPUTS)) if active(i)]
            if now != last and now:
                dev, channels, _ = echoes.ECHO_OUTPUTS[now[0]]
                print(f"    ▶ speaker {now[0] + 1}  ({dev}, {side(channels)})")
            last = now
        else:
            last = None
        time.sleep(0.1)


def main() -> None:
    sounds = make_sounds()
    devices: dict[tuple, list[tuple[int, tuple[int, ...]]]] = {}
    for i, (dev, channels, nch) in enumerate(echoes.ECHO_OUTPUTS):
        devices.setdefault((dev, nch), []).append((i, channels))
        print(f"  speaker {i + 1}: {dev} {side(channels)}")
    for (dev, nch), slots in devices.items():
        threading.Thread(target=run_device, args=(dev, nch, slots, sounds), daemon=True).start()
    threading.Thread(target=announce, daemon=True).start()
    print(__doc__.split("Then type", 1)[1].split("\n", 1)[1])
    print(status())
    try:
        while True:
            cmd = input().strip().lower()
            if cmd == "q":
                break
            if cmd in ("c", "a") or (cmd.isdigit() and 1 <= int(cmd) <= len(echoes.ECHO_OUTPUTS)):
                state["mode"], state["since"] = cmd, time.time()
            elif cmd in ("v", "p"):
                state["sound"] = cmd
            else:
                continue
            print(status())
    except (KeyboardInterrupt, EOFError):
        pass
    stop.set()
    time.sleep(0.3)


if __name__ == "__main__":
    main()
