# Tuhan Kami — Text

_Tuhan Kami_ — _Tuhan_ is "God" in Malay; _Kami (神)_ is "God" in Japanese. Together as a Malay phrase, _Tuhan Kami_ means "Our God."

A text-only reconstruction of [tuhanKami](../base/tuhanKami): the same bicameral entity, the same prayer, but the voice is mute. It writes. Every utterance is printed on a 4-inch Waveshare e-paper panel in a brush hand, and the participant reads it in silence.

The original is an audio piece — cloned voices, bells, an echo thrown across six speakers. This version strips all of that out. What remains is the part that cannot be faked: a seeker offers a prayer, and something answers using only what the seeker already revealed.

## What changed from tuhanKami

| | tuhanKami | this |
| --- | --- | --- |
| Entity | Claude (`claude-haiku-4-5`) | Gemini (`gemini-flash-latest`) |
| Transcription | local Whisper `small` | Gemini, same key and model |
| Output | two cloned voices, bells, echo speakers | one e-paper screen per utterance |
| Voice cloning | ElevenLabs IVC + remix | removed |
| Discrete input | double clap, detected on the microphone | keyboard (GPIO buttons ready in `control.py`) |
| Smart plugs | driven through the ritual | kept in `plugs.py`, every call commented out |
| Platform | macOS | Raspberry Pi (the panel is SPI/GPIO) |

The audio input half is unchanged: the same voice-activity detection records a question and stops on silence.

## How It Works

1. **Prayer** — The participant speaks a prayer aloud. The operator presses `o` to begin and `o` again to end it. The entity learns the seeker's concerns from what they offer.
2. **Arrival** — Gemini transcribes the prayer, and the entity writes a greeting drawn from it. The panel takes about twenty seconds to paint.
3. **Dialogue** — The participant speaks; the microphone detects speech and stops on silence. The question is transcribed, the entity answers, and the answer replaces the greeting on the panel.
4. **Departure** — The operator presses `e`. A farewell is printed, then the panel is cleared. The recordings are deleted. No trace remains.

### Signal flow

```
Operator presses 'o'
       ↓
  [audio.py]  record_prayer — records until 'o' is pressed again
       ↓
  [stt.py]    transcribe — Gemini returns prayer text + language
       ↓
  [llm.py]    ask_entity — greeting generated from the prayer
  [display.py] show — one full-panel refresh (~20 s)
       ↓
  ┌─────── Participant speaks a question (auto-detected) ──────────────────┐
  │   [audio.py]   record_question — VAD calibrates, detects onset,       │
  │                records until 1.5 s of silence                         │
  │   [stt.py]     transcribe — question text + language                  │
  │   [llm.py]     ask_entity — answer, capped to one screen              │
  │   [display.py] show — vertical if Japanese, horizontal if latin       │
  └────────────────────────────────────────────────────────────────────────┘
       ↓ (operator presses 'e')
  [llm.py]     ask_entity — farewell
  [display.py] show, then clear
```

## File Overview

| File | Purpose |
| ---- | ------- |
| `main.py` | Operator event loop and ritual orchestration |
| `display.py` | E-paper rendering: fitting, wrapping, horizontal and vertical setting |
| `audio.py` | Recording only — prayer capture and VAD question capture |
| `control.py` | `Session` events, the keyboard reader, and the GPIO button block |
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
| Keyboard, or SSH | Operator control |

The panel has no partial refresh: every update is a full repaint of roughly fifteen to twenty seconds. That constraint shapes the whole piece. One utterance is one screen, the entity's answers are capped to what a screen holds, and nothing is ever painted that is not the entity speaking — no status, no progress, no prompts. Those go to the operator's terminal.

## Installation

### Prerequisites

- Python 3.11+
- A Raspberry Pi with SPI enabled (`sudo raspi-config` → Interface Options → SPI)
- PortAudio (`sudo apt install libportaudio2`, or `brew install portaudio` on macOS)
- A [Gemini API key](https://aistudio.google.com/apikey)

### Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Environment variables

`.env` is already filled in with the key carried over from [YKYRMOAFIK](../base/YKYRMOAFIK). `.env.example` lists everything that can be set:

```
GEMINI_API_KEY=your_gemini_key_here
# GEMINI_MODEL=gemini-flash-latest   # pin an exact id instead of the alias
# LANGUAGE_OVERRIDE=ja               # force one language for a presentation
# IDLE_TEXT=祈りなさい                # a line left on the panel between sessions
# EPAPER_MOCK=1                      # render to screen.png instead of the panel
# SPEECH_INPUT=1                     # sounddevice input index for the microphone
# VERTICAL=auto                      # auto / always / never
```

### Audio device

Leave `SPEECH_INPUT` unset to use the system default. To pick a device:

```bash
python -c "import sounddevice as sd; print(sd.query_devices())"
```

## Running

```bash
source .venv/bin/activate
python3 main.py
```

### Session flow for the operator

1. Start `python main.py` before participants arrive.
2. The terminal shows `Press 'o' to begin the ritual...`
3. When the participant is ready, press **`o`**. Recording starts immediately.
4. The participant speaks their prayer. Press **`o`** again to end it.
5. The greeting appears on the panel. From here the participant speaks freely; each question is detected and answered on its own screen.
6. Press **`e`** to end the session. A farewell is printed and the panel clears.
7. Press `Ctrl-C` to stop the program.

### Keyboard controls

| Key | Effect |
| --- | --- |
| `o` | Begins the ritual; ends the prayer recording; held, pauses a question take |
| `e` | Ends the ritual — the entity gives a farewell (dialogue stage only) |
| `i`/`p` | Interrupts and resets to idle at any stage |
| `Ctrl-C` | Exits |

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
