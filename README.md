# Tuhan Kami — Text

_Tuhan Kami_ — _Tuhan_ is "God" in Malay; _Kami (神)_ is "God" in Japanese. Together as a Malay phrase, _Tuhan Kami_ means "Our God."

A text-only reconstruction of [tuhanKami](../base/tuhanKami): the same bicameral entity, the same prayer, but the voice is mute. It writes. Every utterance is printed on a 4-inch Waveshare e-paper panel in a brush hand, and the participant reads it in silence.

The original is an audio piece — cloned voices, bells, an echo thrown across six speakers. This version strips all of that out. What remains is the part that cannot be faked: a seeker offers a prayer, and something answers using only what the seeker already revealed.

## What changed from tuhanKami

| | tuhanKami | this |
| --- | --- | --- |
| Entity | Claude (`claude-haiku-4-5`) | Claude (`claude-haiku-5-5`) |
| Transcription | local Whisper `small` | ElevenLabs Scribe, realtime (`scribe_v2_realtime`), as each word is spoken |
| Output | two cloned voices, bells, echo speakers | one e-paper screen per utterance, spoken in the cloned voice over bells; echoes on four speakers driven by a Mac |
| Voice cloning | ElevenLabs IVC + remix | ElevenLabs IVC only — the seeker's own voice, no remix or pitch shift, reverse reverb only; the echo uses the same clone (on the Mac) |
| Discrete input | double clap, detected on the microphone | PiSugar button or keyboard (GPIO buttons ready in `control.py`) |
| Consent | — | a consent form on the panel; nothing is recorded until it is given |
| Smart plugs | driven through the ritual | driven through the ritual: fans while the god arrives and departs, lights off while it is present |
| Platform | macOS | Raspberry Pi (the panel is SPI/GPIO) |

Questions are found by the same voice-activity detection, but transcribed live as they are spoken (ElevenLabs Scribe, realtime): the answer is ready about a second after the seeker stops — and sometimes the entity does not wait for them to stop.

## How It Works

0. **Consent** — Between sessions the panel shows a consent form, in Japanese. A long press switches it to English (and back); the language the participant consents in is the session's language. A double press gives consent.
1. **Prayer** — Recording starts as soon as consent is given. The panel shows the invocation, set small to fit: about a minute to read aloud, enough for a good clone, with one blank for the seeker's own question (*So tell me this: ______?*). The participant reads it aloud and double-presses to end it. The entity learns the seeker's voice and their question from what they offer.
2. **Arrival** — As the prayer ends, the panel turns white on black: *Your prayer is heard. Be still, and wait. I am drawing near.* Meanwhile ElevenLabs clones the voice from it; the prayer was transcribed as it was spoken, so its words are already there. The god never arrives sooner than a minute after the prayer (`ARRIVAL_SECS`) — unless the seeker double-presses to hurry it, in which case it comes at once, and its greeting opens in anger at their impatience. The clone takes only seconds, but the god takes its time: the bell never tolls sooner than 15 seconds after the prayer (`ARRIVAL_SECS` in `main.py`). A bell tolls; three seconds in, the entity speaks a greeting drawn from the prayer, in the seeker's own voice, while the panel paints the same words (about twenty seconds).
3. **Dialogue** — The participant simply speaks; the microphone detects the question and it is transcribed as it is spoken. The panel goes blank as they begin and red when the question is taken. On about a third of questions, chosen at random, the entity cuts in once enough has been said — as a god that already knows what you are going to ask — and answers the whole question from its first half. The voice speaks the whole answer; the panel shows a short poem distilled from it, in the same voice, so a long answer still reads whole on one screen.
4. **Departure** — A double press. The entity speaks and prints a farewell, in the seeker's plain cloned voice with no reverb (the echoes keep theirs), three bells toll, the voice clone and the recordings are deleted, and the consent form returns. No trace remains.

### Signal flow

```
[consent.py] wait_for_consent — the form, until a double press ('e')
       ↓
  [audio.py]  record_prayer — 44.1 kHz, until a double press ('e')
       ↓
  [listen.py] Listener — the prayer transcribed live, ready at the tap
  [tts.py]    clone_voice — ElevenLabs IVC from the prayer
       ↓
  [llm.py]    ask_entity — greeting generated from the prayer
  [tts.py]    speak_stream — greeting in the cloned voice, as it is made
  [audio.py]  play_stream — bell at once, the voice streaming in 3 s later,
              through the reverse reverb
  [display.py] show — one full-panel refresh (~20 s), alongside the voice
       ↓
  ┌─────── Participant asks a question ────────────────────────────────────┐
  │   [audio.py]   record_question — VAD: onset, then 1.5 s of silence   │
  │   [listen.py]  Listener — live transcript while they speak; may cut  │
  │                in midway (FOREKNOW_CHANCE in main.py)                 │
  │   [stt.py]     transcribe — question text + language                  │
  │   [llm.py]     ask_entity — the spoken answer                         │
  │   [llm.py]     inscribe — a short poem of it for the panel            │
  │   [tts.py]     speak — answer in the cloned voice                     │
  │   [audio.py]   play_stream — the voice, ~2 s after the text exists   │
  │   [display.py] show — the poem, as soon as it is written             │
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
| `tts.py` | ElevenLabs: voice cloning, streaming synthesis, deletion |
| `listen.py` | ElevenLabs Scribe: live transcription of the prayer and each question, and the fallback on a recording |
| `display.py` | E-paper rendering: fitting, wrapping, horizontal and vertical setting |
| `audio.py` | Tap-controlled prayer and question capture, streaming reverse reverb, bells, playback |
| `control.py` | `Session` events, the keyboard and button-pipe reader, and the GPIO button block |
| `pisugar_button.sh` | Forwards PiSugar button taps to `main.py` |
| `llm.py` | Claude entity responses, length budgeting, and the out-of-character guard |
| `prompts.py` | Entity system prompt (the persona and its rules) |
| `echo.py` | Sends each utterance to the echo server on the Mac |
| `echo_server/` | Runs on the Mac: echo text, the echo voice, and quad-speaker playback |
| `plugs.py` | TP-Link Tapo plugs: the fans and the lights, switched in the background |
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
- An [Anthropic API key](https://platform.claude.com/settings/keys) (the entity)
- An [ElevenLabs API key](https://elevenlabs.io/app/settings/api-keys) with voice cloning, text to speech and speech to text (without one, sessions run text-only with the bells)

### Setup

```bash
python3 -m venv --system-site-packages .venv   # system packages: lgpio for the panel
source .venv/bin/activate
pip install -r requirements.txt
```

### Environment variables

`.env` is already filled in with the key carried over from [YKYRMOAFIK](../base/YKYRMOAFIK). `.env.example` lists everything that can be set:

```
ANTHROPIC_API_KEY=your_anthropic_key_here
ELEVENLABS_API_KEY=your_elevenlabs_key_here
# ENTITY_MODEL=claude-haiku-5-5      # the Claude model that speaks as the entity
# LANGUAGE_OVERRIDE=en               # the language the consent form opens in (default ja)
# EPAPER_MOCK=1                      # render to screen.png instead of the panel
# SPEECH_INPUT=1                     # sounddevice input index for the microphone
# MAIN_OUTPUT=0                      # sounddevice output index for the voice and bells
# VERTICAL=auto                      # auto / always / never
# ECHO_HOST=mymac.local:8765        # the echo server on the Mac (unset: no echo)
# TPLINK_FAN=172.27.56.184          # Tapo plug for the fans (unset: skipped)
# TPLINK_LIGHT=172.27.56.185        # Tapo plug for the lights
# TPLINK_USERNAME=…  TPLINK_PASSWORD='…'   # the Tapo account; quote the password
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
3. The participant speaks their prayer and double-presses to end it.
4. A bell, then the greeting is spoken and appears on the panel. From here the participant speaks freely; each question is detected, answered aloud and on its own screen.
5. A double press ends the session: a farewell, three bells, and the consent form returns.
6. Press `Ctrl-C` to stop the program.

### Keyboard controls

| Key | Effect |
| --- | --- |
| `e` | On the consent form, consents and begins the prayer; during the prayer, ends it; in the dialogue, ends the ritual with a farewell (and cuts short an answer being spoken) |
| `i`/`p` | On the consent form, switches Japanese / English; otherwise interrupts and returns to the consent form |
| `o` | Nothing (the prayer ends with `e`, a double press; questions are found by speech detection) |
| `Ctrl-C` | Exits |

### PiSugar button

`pisugar_button.sh` turns the PiSugar's custom button into the keyboard keys: single tap is `o` (unused), double tap is `e`, long press is `i` — so a double press consents, ends the prayer and ends the ritual, and a long press is the language switch on the consent form and an interrupt everywhere else. pisugar-server runs the command; the script forwards the tap through `button.fifo`, which `main.py` creates while it runs. In the PiSugar web UI (port 8421), enable each tap under the custom button function and set its shell to:

```
/home/matsurinoyama/divineMedium/pisugar_button.sh single
/home/matsurinoyama/divineMedium/pisugar_button.sh double
/home/matsurinoyama/divineMedium/pisugar_button.sh long
```

In the dialogue, questions need no button: speech is detected. A double press ends the ritual even mid-question; that unfinished question is dropped.

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

The entity is powered by Claude (`claude-haiku-5-5`, set by `ENTITY_MODEL`). It was moved off Gemini (tried for a while), which stepped out of the role and spoke as an AI in most turns when a seeker pressed it for proof about illness or birth. Its persona is defined in [prompts.py](prompts.py):

- It speaks as a deity.
- It draws directly on the prayer: the stated fears, desires and circumstances inform every answer.
- It never offers choices. It speaks the singular truth.
- It is specific. Each answer opens with a cold reading (the Barnum effect: one or two things true of almost anyone in the seeker's situation, drawn from their prayer so they feel personal), then pronounces on their situation — what is really happening, what it costs them, what is coming. Only on about a third of answers (`COMMAND_CHANCE` in `main.py`; never the greeting or farewell) does it also command the single most likely best course of action in concrete terms — what to do this week, who to speak to, what to say. Stock vagueness ("you already know the answer", "the answer is in your heart", "in its own time") is forbidden.
- When it cuts in midway, it names the question the seeker was about to ask, then answers it.
- It answers in the language of the question (English and Japanese).
- Answer length scales with question length.
- The panel never shows the answer word for word: `inscribe()` distils each utterance (greeting, answers, farewell) into a poem of two to five short lines in the god's voice, made alongside the speech synthesis. Japanese lines stand whole; English lines that the narrow panel must wrap are set as small stanzas so they still read as one line each. If no poem comes back that stays in the voice and fits, the spoken words are shown instead.
- When a situation seems hopeless, it may give prophecy.

The underlying principle, encoded explicitly in the system prompt, is Jaynes': the voice of god is a reflection of the seeker's own mind.

- It stays in the voice to the end. The prompt tells it that every seeker consented to an artwork, and that when pressed for proof it may call it a test of faith but still gives something specific to do — never stating a medical outcome as fact.
- As a backstop, `llm.py` discards any reply that names itself an AI, the role or the installation, or points the seeker to a helpline (sending them to a doctor or a counsellor, in the god's voice, is allowed); it asks once more, and if that also fails, a short in-character line is used instead. Neither is ever shown, spoken or kept in the conversation.



## The echo (on a Mac)

The echo is tuhanKami's second voice: broken, looping fragments of every greeting, answer and farewell, played on four speakers around the room. It runs on a Mac, from `echo_server/`, because the Pi is busy with the panel. Unlike in tuhanKami, there is no remix. The echo uses the seeker's own clone with tuhanKami's echo settings (unstable, slow), pitched down 2 semitones and reverberated. The bells only play from the Pi; during the three farewell bells the echoes fade to silence.

The Pi sends only commands and text (`echo.py`): `start` with the clone's voice id, `utter` with each utterance, `fade` with the bells' length, and `end`. The Mac writes the fragments with Claude, synthesises them in the same voice, and picks a speaker at random for each one; every new echo dims the ones still sounding. If `ECHO_HOST` is unset or the Mac can't be reached, the ritual carries on without the echo. `end` is sent before the Pi deletes the clone.

Beneath the echoes, the cave: `CAVESOUNDS.mp3` (in the project folder; not in git — it is over GitHub's 100 MB limit) loops on every speaker from the moment the server starts, between sessions too, while the room waits. Each speaker plays its own stretch of the recording, from a random point, so the cave surrounds rather than coming from the middle. The echoes play over it, and the farewell fade takes only the echoes; the cave plays on. It needs `ffmpeg` (`brew install ffmpeg`).

On the Mac:

```bash
cd echo_server
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python server.py --devices        # find the output device index
```

`echo_server/.env`:

```
ANTHROPIC_API_KEY=...
ELEVENLABS_API_KEY=...            # the same account as the Pi, so the clone is visible
ECHO_OUTPUTS=5:0:4,5:1:4,5:2:4,5:3:4   # device:channel:channels — one 4-channel interface
# ECHO_OUTPUTS=6:0:2,6:1:2,4:0:2,4:1:2 # or two stereo devices
# ECHO_OUTPUTS=6:0:2,6:1:2,4:0+1:2   # 0+1: one speaker on both sides of a device (same echo on L and R)
# ECHO_PORT=8765
# ECHO_MODEL=claude-haiku-5-5
# CAVE_VOLUME=1.0                  # the cave beneath the echoes: 1.0 as recorded, 0 for none
# CAVE_FILE=/path/to/other.mp3     # default: ../CAVESOUNDS.mp3
```

Then run `python server.py`, and set `ECHO_HOST=<mac>.local:8765` in the Pi's `.env`. If `ECHO_OUTPUTS` is unset, the echo plays on the Mac's default left and right outputs, for testing.

## Credits and AI Disclosure

Base code and documentation for the original was written by Anthropic's [Claude](https://claude.ai/). This version was adapted with Claude as well.

This program requires [Claude](https://platform.claude.com) and [ElevenLabs](https://elevenlabs.io) to function. As part of our compliance, any stored user data (user audio, user interactions) is deleted after every session. Quitting with `Ctrl-C` mid-session clears the session first; a voice clone that a crash or power cut kept from being deleted is deleted the next time the program starts.
