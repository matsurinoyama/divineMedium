# Tuhan Kami — Text

_Tuhan Kami_ — _Tuhan_ is "God" in Malay; _Kami (神)_ is "God" in Japanese. Together as a Malay phrase, _Tuhan Kami_ means "Our God."

A text-only reconstruction of [tuhanKami](../base/tuhanKami): the same bicameral entity, the same prayer, but the voice is mute. It writes. Every utterance is printed on a 4-inch Waveshare e-paper panel in a brush hand, and the participant reads it in silence.

The original is an audio piece — cloned voices, bells, an echo thrown across six speakers. This version strips all of that out. What remains is the part that cannot be faked: a seeker offers a prayer, and something answers using only what the seeker already revealed.

## What changed from tuhanKami

| | tuhanKami | this |
| --- | --- | --- |
| Entity | Claude (`claude-haiku-4-5`) | Gemini (`gemini-flash-latest`) |
| Transcription | local Whisper `small` | Gemini, same key and model |
| Output | two cloned voices, bells, echo speakers | one e-paper screen per utterance, spoken in the cloned voice over bells |
| Voice cloning | ElevenLabs IVC + remix | ElevenLabs IVC only — the seeker's own voice, no remix or pitch shift, reverse reverb only; no echo voice yet |
| Discrete input | double clap, detected on the microphone | PiSugar button or keyboard (GPIO buttons ready in `control.py`) |
| Consent | — | a consent form on the panel; nothing is recorded until it is given |
| Smart plugs | driven through the ritual | kept in `plugs.py`, every call commented out |
| Platform | macOS | Raspberry Pi (the panel is SPI/GPIO) |

The audio input half is unchanged: the same voice-activity detection records a question and stops on silence.

## How It Works

0. **Consent** — Between sessions the panel shows a consent form, in Japanese. A long press switches it to English (and back); the language the participant consents in is the session's language. A double press gives consent.
1. **Prayer** — Recording starts as soon as consent is given. The participant speaks a prayer aloud and taps once to end it. The entity learns the seeker's voice and concerns from what they offer.
2. **Arrival** — Gemini transcribes the prayer and ElevenLabs clones the voice from it. A bell tolls; three seconds in, the entity speaks a greeting drawn from the prayer, in the seeker's own voice, while the panel paints the same words (about twenty seconds).
3. **Dialogue** — The participant speaks; the microphone detects speech and stops on silence. The question is transcribed, the entity answers, and the answer is spoken and replaces the greeting on the panel.
4. **Departure** — A double press. The entity speaks and prints a farewell, three bells toll, the voice clone and the recordings are deleted, and the consent form returns. No trace remains.

### Signal flow

```
[consent.py] wait_for_consent — the form, until a double press ('e')
       ↓
  [audio.py]  record_prayer — 44.1 kHz, until a single tap ('o')
       ↓
  [stt.py]    transcribe — Gemini returns prayer text
  [tts.py]    clone_voice — ElevenLabs IVC from the prayer
       ↓
  [llm.py]    ask_entity — greeting generated from the prayer
  [tts.py]    speak — greeting in the cloned voice
  [audio.py]  bell_mix + play — bell, greeting 3 s in, reverse reverb
  [display.py] show — one full-panel refresh (~20 s), alongside the voice
       ↓
  ┌─────── Participant speaks a question (auto-detected) ──────────────────┐
  │   [audio.py]   record_question — VAD calibrates, detects onset,       │
  │                records until 1.5 s of silence                         │
  │   [stt.py]     transcribe — question text + language                  │
  │   [llm.py]     ask_entity — answer, capped to one screen              │
  │   [tts.py]     speak — answer in the cloned voice                     │
  │   [display.py] show + [audio.py] play — together                      │
  └────────────────────────────────────────────────────────────────────────┘
       ↓ (double press, 'e')
  [llm.py]     ask_entity — farewell, spoken and shown
  [audio.py]   three overlapping bells
  [tts.py]     delete_voice — the clone is destroyed
```

## File Overview

| File | Purpose |
| ---- | ------- |
| `main.py` | Event loop and ritual orchestration |
| `consent.py` | The consent form (Japanese / English) and the wait for consent |
| `tts.py` | ElevenLabs: voice cloning, synthesis, deletion |
| `display.py` | E-paper rendering: fitting, wrapping, horizontal and vertical setting |
| `audio.py` | Prayer capture, VAD question capture, reverse reverb, bells, playback |
| `control.py` | `Session` events, the keyboard and button-pipe reader, and the GPIO button block |
| `pisugar_button.sh` | Forwards PiSugar button taps to `main.py` |
| `stt.py` | Gemini audio transcription |
| `llm.py` | Gemini entity responses and length budgeting |
| `gemini.py` | Shared client, safety settings, retries, thinking-off |
| `prompts.py` | Entity system prompt (the persona and its rules) |
| `plugs.py` | TP-Link smart plugs — present, never called |
| `debug_display.py` | Render text to the panel or to `screen.png` without a ritual |

## Hardware

| Component | Role |
| --- | --- |
| Raspberry Pi | Runs everything; the panel needs its SPI and GPIO |
| Waveshare 4inch e-Paper (E) | 400×600, six colours, full refresh only |
| USB microphone or headset | Captures the prayer and the questions |
| Speaker or headset | The entity's voice and the bells |
| PiSugar battery button | The participant's control: consent, language, end of prayer, farewell |
| Keyboard, or SSH | Operator control, alongside the button |

The panel has no partial refresh: every update is a full repaint of roughly fifteen to twenty seconds. That constraint shapes the whole piece. One utterance is one screen, the entity's answers are capped to what a screen holds, and nothing is ever painted that is not the entity speaking — no status, no progress, no prompts. Those go to the operator's terminal.

## Installation

### Prerequisites

- Python 3.11+
- A Raspberry Pi with SPI enabled (`sudo raspi-config` → Interface Options → SPI)
- PortAudio (`sudo apt install libportaudio2`, or `brew install portaudio` on macOS)
- A [Gemini API key](https://aistudio.google.com/apikey)
- An [ElevenLabs API key](https://elevenlabs.io/app/settings/api-keys) with voice cloning (without one, sessions run text-only with the bells)

### Setup

```bash
python3 -m venv --system-site-packages .venv   # system packages: lgpio for the panel
source .venv/bin/activate
pip install -r requirements.txt
```

### Environment variables

`.env` is already filled in with the key carried over from [YKYRMOAFIK](../base/YKYRMOAFIK). `.env.example` lists everything that can be set:

```
GEMINI_API_KEY=your_gemini_key_here
ELEVENLABS_API_KEY=your_elevenlabs_key_here
# GEMINI_MODEL=gemini-3.8-flash      # gemini-flash-latest is not served on the v1 API
# LANGUAGE_OVERRIDE=en               # the language the consent form opens in (default ja)
# EPAPER_MOCK=1                      # render to screen.png instead of the panel
# SPEECH_INPUT=1                     # sounddevice input index for the microphone
# MAIN_OUTPUT=0                      # sounddevice output index for the voice and bells
# VERTICAL=auto                      # auto / always / never
```

### Audio device

Leave `SPEECH_INPUT` and `MAIN_OUTPUT` unset to use the system default. To pick a device:

```bash
python -c "import sounddevice as sd; print(sd.query_devices())"
```

## Running

```bash
source .venv/bin/activate
python3 main.py
```

### Session flow for the operator

1. Start `python main.py` before participants arrive. The consent form appears on the panel.
2. The participant may long-press to switch the form to English, then double-presses to consent. Recording starts immediately.
3. The participant speaks their prayer and taps once to end it.
4. A bell, then the greeting is spoken and appears on the panel. From here the participant speaks freely; each question is detected, answered aloud and on its own screen.
5. A double press ends the session: a farewell, three bells, and the consent form returns.
6. Press `Ctrl-C` to stop the program.

### Keyboard controls

| Key | Effect |
| --- | --- |
| `e` | On the consent form, consents and begins the prayer; in the dialogue, ends the ritual with a farewell (and cuts short an answer being spoken) |
| `i`/`p` | On the consent form, switches Japanese / English; otherwise interrupts and returns to the consent form |
| `o` | Ends the prayer recording; held, pauses a question take |
| `Ctrl-C` | Exits |

### PiSugar button

`pisugar_button.sh` turns the PiSugar's custom button into the keyboard keys: single tap is `o`, double tap is `e`, long press is `i` — so a long press is the language switch on the consent form and an interrupt everywhere else. pisugar-server runs the command; the script forwards the tap through `button.fifo`, which `main.py` creates while it runs. In the PiSugar web UI (port 8421), enable each tap under the custom button function and set its shell to:

```
/home/matsurinoyama/divineMedium/pisugar_button.sh single
/home/matsurinoyama/divineMedium/pisugar_button.sh double
/home/matsurinoyama/divineMedium/pisugar_button.sh long
```

There is no hold-to-pause on the button; a single tap during the dialogue discards the question take in progress instead.

### Buttons

`control.py` ends with a commented `attach_buttons()` that wires one or two GPIO buttons to the same events the keys set. Uncomment it there and in `main.py`; both paths can run at once, so the keyboard keeps working as an operator override.

### Developing without the panel

Off a Pi, the driver import fails and `Display` falls back automatically to writing `screen.png` and printing the wrapped text to the terminal. `EPAPER_MOCK=1` forces that on a Pi too.

```bash
python debug_display.py                   # a sample english answer
python debug_display.py --ja              # a sample japanese answer, set vertically
python debug_display.py "your text here"
python debug_display.py --budget          # check the length budgets against the geometry
python debug_display.py --real "text"     # push to the actual panel
```

### Debugging: reusing a recorded prayer

If `debug.wav` exists in the project directory when a session starts, the prayer recording is skipped and that file is used instead — useful for testing the dialogue without re-recording a prayer each time.

## Typesetting

Japanese is set vertically (縦書き): columns run top to bottom, right to left, as a prayer or an oracle would be written. Latin is set horizontally. The orientation is chosen per utterance from the script of the text, so a bilingual session switches on its own; `VERTICAL=always` or `never` overrides it.

Pillow has no vertical text support without libraqm, which is absent from most wheels, so `display.py` sets the columns itself. That means it also handles what vertical setting requires:

- `、` and `。` are drawn in the upper right of their square rather than on the baseline.
- `ー`, dashes, ellipses and every kind of bracket are turned a quarter turn clockwise.
- Runs of latin and digits are set sideways as whole words, not stacked letter by letter.
- Kinsoku shori: no line or column opens with closing punctuation. It hangs past the margin instead (burasage), which the centred block absorbs.

Text is fitted rather than paged. `layout()` walks down a ladder of font sizes and keeps the largest at which the whole utterance still fits one screen; if even the smallest overflows, the text is trimmed back to the last sentence that fits, so a severed clause is never shown. The entity is told its budget up front (`display.BUDGET_WORDS`, `display.BUDGET_CJK_CHARS`), so trimming is a backstop rather than a routine event. Re-run `debug_display.py --budget` after changing the font, the geometry or the size ladder.

The font is FOT-ShinGyoRei StdN, a semi-cursive brush face — the hand of something writing on the panel rather than a machine printing to it. It draws latin in its own idiosyncratic way (its `e` is close to a Greek epsilon); set `LATIN_FONT_PATH` to use another face for latin while keeping ShinGyoRei for Japanese.

## Entity Behaviour

The entity is powered by `gemini-flash-latest`. Its persona is defined in [prompts.py](prompts.py):

- It speaks as a deity.
- It draws directly on the prayer: the stated fears, desires and circumstances inform every answer.
- It never offers choices. It speaks the singular truth.
- It answers in the language of the question (English and Japanese).
- Answer length scales with question length, capped at what one screen holds.
- When a situation seems hopeless, it may give prophecy.

The underlying principle, encoded explicitly in the system prompt, is Jaynes': the voice of god is a reflection of the seeker's own mind.

Safety filters are set to `BLOCK_NONE`. A prayer is often about illness, debt, death or a failing relationship, and the entity answers in the voice of a deity; the default filters refuse both halves of that, and a refusal printed on the panel ends the piece.

## Bringing the voice back

The audio layer was removed, not redesigned around. When it returns:

- `base/tuhanKami/audio.py` holds the playback chain — pitch shift, reverse reverb, the interruptible main voice, the bells, and `BackgroundEchoPlayer` for the surrounding speakers.
- `base/tuhanKami/tts.py` holds ElevenLabs cloning and synthesis, and `ask_echo` in `base/tuhanKami/llm.py` generates the fragmented echo text.
- `display.show()` blocks for the full refresh. Speaking while the panel paints means handing it to a thread and deciding what the participant is meant to be doing during those twenty seconds — the panel's slowness is a timing constraint on the piece, not just on the code.

## Credits and AI Disclosure

Base code and documentation for the original was written by Anthropic's [Claude](https://claude.ai/). This version was adapted with Claude as well.

This program requires [Gemini](https://aistudio.google.com) to function. As part of our compliance, any stored user data (user audio, user interactions) is deleted after every session.
