from __future__ import annotations

import os
import threading

from dotenv import load_dotenv
import anthropic
import numpy as np
from elevenlabs.client import ElevenLabs
from google import genai

# Before the local imports: gemini, audio and display read their settings from the
# environment at import time.
load_dotenv()

import consent
import gemini
import llm
import tts
# import plugs                      # smart plugs — see the commented calls below
from audio import (
    bell_mix,
    build_voice,
    compress_for_ivc,
    is_silent,
    play,
    random_bell,
    record_prayer,
    record_question,
)
from control import (
    DIALOGUE,
    FAREWELL,
    IDLE,
    PRAYER,
    RitualAborted,
    Session,
    button_pipe,
    check,
    raw_keyboard,
    read_key,
)
from display import BACKGROUND, BLACK, BLUE, GOLD, RED, TEXT_COLOR, WHITE, Display
from llm import ask_entity
from prompts import FAREWELL_REQUEST, GREETING_REQUEST
from stt import transcribe

# File paths used during a session
DEBUG_WAV = "debug.wav"        # if present, skips prayer recording and uses this file
PRAYER_WAV = "prayer.wav"      # recorded prayer audio
QUESTION_WAV = "question.wav"  # recorded question audio
PRAYER_UPLOAD_WAV = "prayer_upload.wav"  # prayer downsampled for the voice clone

# (text, background) for the screens that are not answers. The prayer the seeker
# reads is blue — their words, not the entity's. The greeting arrives in
# vermilion, the red of shrine gates and seals; the farewell is gold on black.
# Answers stay black on white, the most legible the panel gets.
PRAYER_STYLE = (BLUE, BACKGROUND)
# The wait between the prayer and the greeting: white on black, the dark before
# the voice arrives.
TRANSITION_STYLE = (WHITE, BLACK)
GREETING_STYLE = (RED, BACKGROUND)
FAREWELL_STYLE = (GOLD, BLACK)

# The language the consent form opens in. The seeker can switch it with a long
# press, and the language they consent in is the session's language.
DEFAULT_LANGUAGE = os.getenv("LANGUAGE_OVERRIDE") or consent.DEFAULT_LANGUAGE


def synthesize(eleven: ElevenLabs | None, voice_id: str | None, text: str) -> bytes | None:
    # The utterance in the cloned voice, or None when there is no voice this session.
    if eleven is None or voice_id is None:
        return None
    try:
        return tts.speak(eleven, voice_id, text)
    except Exception as e:
        print(f"  [voice failed: {type(e).__name__}: {e}]")
        return None


Style = tuple[tuple[int, int, int], tuple[int, int, int]]


class Panel:
    # One panel refresh run in the background, so the ~20 seconds it takes can
    # overlap with playback or recording. join() re-raises a failed refresh.
    # after: a refresh still in flight that this one must wait its turn behind.
    def __init__(
        self, display: Display, text: str, style: Style, after: Panel | None = None
    ) -> None:
        self.failure: BaseException | None = None
        self.thread = threading.Thread(
            target=self.run, args=(display, text, style, after), daemon=True
        )
        self.thread.start()

    def run(self, display: Display, text: str, style: Style, after: Panel | None) -> None:
        if after is not None:
            after.thread.join()
        try:
            display.show(text, *style)
        except BaseException as e:
            self.failure = e

    def join(self) -> None:
        self.thread.join()
        if self.failure is not None:
            raise self.failure


def say(
    display: Display,
    text: str,
    audio: np.ndarray | None,
    *stops: threading.Event,
    style: Style = (TEXT_COLOR, BACKGROUND),
) -> None:
    # Print and speak one utterance, the panel refresh alongside the playback;
    # returns when both are done.
    panel = Panel(display, text, style)
    if audio is not None:
        play(audio, *stops)
    panel.join()


def run_ritual(
    client: genai.Client,
    entity: anthropic.Anthropic,
    eleven: ElevenLabs | None,
    display: Display,
    session: Session,
    language: str,
) -> None:
    """
    Full ritual flow, from the prayer recording to the farewell.

    Stages:
      1. Prayer — a short prayer to read appears on the panel while recording
         runs until 'o', or debug.wav is loaded instead
      2. Transcription — Gemini → prayer_text
      3. Voice clone — one ElevenLabs IVC voice from the prayer
      4. Greeting — the entity's welcome, drawn from the prayer, on the panel and
         spoken over a bell, 3 seconds in
      5. Dialogue loop — question → answer → one screen and one spoken answer, until 'e'
      6. Farewell — a last screen and spoken farewell, three bells, then the clone
         is deleted and the consent form returns

    One utterance is one full-panel refresh, roughly 20 seconds; the voice plays
    while the panel draws. Without an ElevenLabs key, or if cloning fails, the
    session carries on text-only with the bells.

    language is the one the seeker consented in: transcription is told to expect
    it and the entity answers in it, for the whole session.
    """
    prayer_audio = PRAYER_WAV
    voice_id: str | None = None
    bell = random_bell()
    invitation: Panel | None = None
    transition: Panel | None = None
    try:
        # plugs.entity_travel(False, 1)
        # plugs.entity_travel(True, 2)

        # ── PRAYER ────────────────────────────────────────────────────────────
        session.stage = PRAYER
        if os.path.exists(DEBUG_WAV):
            print("\n  [Debug mode enabled: using existing recording in debug.wav]")
            prayer_audio = DEBUG_WAV
        else:
            print("\n  [Prayer recording started. Press 'o' to finish.]\n")
            # The short prayer replaces the consent form while recording is
            # already running, so nothing said during the refresh is lost.
            invitation = Panel(display, consent.PRAYERS[language], PRAYER_STYLE)
            record_prayer(session, PRAYER_WAV)
            check(session)
            if not os.path.exists(PRAYER_WAV) or is_silent(PRAYER_WAV):
                print("  (nothing was offered — returning to idle)\n")
                return

        # The prayer screen gives way to the wait, drawn while everything below runs
        transition = Panel(display, consent.RECEIVED[language], TRANSITION_STYLE, after=invitation)

        print("  [Transcribing...]")
        # plugs.entity_travel(True, 1)
        prayer_text, prayer_lang = transcribe(client, prayer_audio, language=language)
        check(session)
        if not prayer_text:
            print("  (no speech found in the prayer — returning to idle)\n")
            return
        print(f"  Prayer received. [{prayer_lang}]\n")

        # ── VOICE CLONE ───────────────────────────────────────────────────────
        if eleven is not None:
            try:
                compress_for_ivc(prayer_audio, PRAYER_UPLOAD_WAV)
                voice_id = tts.clone_voice(eleven, PRAYER_UPLOAD_WAV)
            except Exception as e:
                print(f"  [voice clone failed — continuing text-only: {type(e).__name__}: {e}]")
            check(session)

        # ── GREETING ──────────────────────────────────────────────────────────
        session.stage = DIALOGUE
        history: list[dict] = []  # running conversation, as Messages API turns
        # plugs.entity_travel(False, 1)
        greeting = ask_entity(entity, [], GREETING_REQUEST, prayer_text, language=prayer_lang)
        check(session)
        print(f"\n  They: {greeting}\n")
        pcm = synthesize(eleven, voice_id, greeting)
        check(session)
        transition.join()   # one refresh at a time on the panel
        # plugs.entity_travel(False, 2)
        # The bell tolls once; the greeting rises out of it 3 seconds in
        say(display, greeting, bell_mix(bell, voice=build_voice(pcm) if pcm else None),
            session.abort, style=GREETING_STYLE)
        check(session)
        print("\nTap once to ask a question and again when done, or double press to end.\n")

        # ── DIALOGUE LOOP ─────────────────────────────────────────────────────
        while True:
            ended = record_question(session, QUESTION_WAV)
            check(session)

            if ended:
                break

            if not os.path.exists(QUESTION_WAV) or is_silent(QUESTION_WAV):
                print("  (nothing captured — try again)\n")
                if os.path.exists(QUESTION_WAV):
                    os.remove(QUESTION_WAV)
                continue

            question, q_lang = transcribe(client, QUESTION_WAV, language=language)
            os.remove(QUESTION_WAV)
            check(session)

            if not question:
                print("  (nothing intelligible — try again)\n")
                continue

            print(f"  You: {question}")
            print("  [They speak...]")
            answer = ask_entity(entity, history, question, prayer_text, language=q_lang)
            check(session)
            print(f"\n  They: {answer}\n")
            pcm = synthesize(eleven, voice_id, answer)
            check(session)

            # 'e' cuts the answer short, as a double clap did in tuhanKami
            say(display, answer, build_voice(pcm) if pcm else None, session.abort, session.end)
            check(session)

        # ── FAREWELL ──────────────────────────────────────────────────────────
        session.stage = FAREWELL
        print("  [They depart...]")
        # plugs.entity_travel(True, 1)
        farewell = ask_entity(entity, history, FAREWELL_REQUEST, prayer_text, language=prayer_lang)
        check(session)
        print(f"\n  They: {farewell}\n")
        pcm = synthesize(eleven, voice_id, farewell)
        check(session)
        say(display, farewell, build_voice(pcm) if pcm else None, session.abort,
            style=FAREWELL_STYLE)
        check(session)
        # Three overlapping bells close the ritual
        play(bell_mix(bell, times=3, overlap_secs_min=5.5, overlap_secs_max=7.0), session.abort)
        # plugs.entity_travel(False, 1)
        # plugs.entity_travel(True, 2)

    except RitualAborted:
        pass
    except Exception as e:
        # A session must never take the installation down with it.
        print(f"\n  [Ritual failed: {type(e).__name__}: {e}]\n")
    finally:
        # Let a refresh still in flight finish before main() draws the consent form
        for panel in (invitation, transition):
            if panel is not None:
                try:
                    panel.join()
                except Exception as e:
                    print(f"  [display failed: {e}]")
        # Always clean up — the clone and the recordings are deleted even if the
        # ritual was interrupted
        if eleven is not None and voice_id is not None:
            tts.delete_voice(eleven, voice_id)
        for path in (PRAYER_WAV, PRAYER_UPLOAD_WAV, QUESTION_WAV):
            if os.path.exists(path):
                os.remove(path)
        # The panel is not cleared here: main() puts the consent form back up
        # straight away, so no seeker's answer is left on display for the next one.
        session.stage = IDLE
        print("  [Ritual cleared.]\n")


def main() -> None:
    # Event loop: shows the consent form, runs a session once it is given, repeats.
    client = gemini.make_client()
    entity = llm.make_client()
    eleven = tts.make_client()
    print(f"  [entity: {llm.MODEL} · transcription: {gemini.MODEL}]")

    language = DEFAULT_LANGUAGE
    with Display() as display, raw_keyboard(), button_pipe():
        while True:
            print(
                "\nWaiting for consent...\n"
                "  e  consent and begin the prayer / end the ritual (farewell)\n"
                "  i  switch the form between Japanese and English / interrupt and reset\n"
                "  o  end the prayer / start and finish a question\n"
                "  (PiSugar button: tap = o, double tap = e, long press = i)\n"
            )

            language = consent.wait_for_consent(display, language)
            print(f"  [Consent given — ritual unlocked. ({language})]\n")

            session = Session()
            # control.attach_buttons(session)   # GPIO buttons — see control.py
            ritual_thread = threading.Thread(
                target=run_ritual, args=(client, entity, eleven, display, session, language), daemon=True
            )
            ritual_thread.start()

            while ritual_thread.is_alive():
                key = read_key()
                if key in ("i", "p"):
                    # Interrupt: signal abort and unblock any recording in progress
                    print("\n  [Ritual interrupted. Restarting...]\n")
                    session.abort.set()
                    ritual_thread.join()
                    break
                elif key == "e":
                    # End the session — the entity answers once more and departs
                    if session.stage == DIALOGUE:
                        print("\n  [Ending — the entity will say its farewell.]\n")
                        session.end.set()
                    else:
                        print("\n  ['e' only ends the ritual during the dialogue.]\n")
                elif key == "o" and session.stage in (PRAYER, DIALOGUE):
                    # Ends the prayer; in the dialogue, opens and closes a question
                    session.tap.set()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  [Exiting.]\n")
