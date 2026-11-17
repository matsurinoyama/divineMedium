from __future__ import annotations

import os
import threading
import time

from dotenv import load_dotenv
from google import genai
from google.genai import types

import gemini
# import plugs                      # smart plugs — see the commented calls below
from audio import is_silent, record_prayer, record_question
from control import (
    DIALOGUE,
    FAREWELL,
    IDLE,
    PRAYER,
    RitualAborted,
    Session,
    check,
    raw_keyboard,
    read_key,
)
from display import Display
from llm import ask_entity
from prompts import FAREWELL_REQUEST, GREETING_REQUEST
from stt import transcribe

load_dotenv()

# File paths used during a session
DEBUG_WAV = "debug.wav"        # if present, skips prayer recording and uses this file
PRAYER_WAV = "prayer.wav"      # recorded prayer audio
QUESTION_WAV = "question.wav"  # recorded question audio

# e.g. "ja" to force the entity to always speak Japanese, for presentations
LANGUAGE_OVERRIDE = os.getenv("LANGUAGE_OVERRIDE") or None
# Optional single line left on the panel between sessions. Empty = blank panel.
IDLE_TEXT = os.getenv("IDLE_TEXT", "")


def run_ritual(client: genai.Client, display: Display, session: Session) -> None:
    """
    Full ritual flow, from the prayer recording to the farewell.

    Stages:
      1. Prayer — record until 'o', or load debug.wav
      2. Transcription — Gemini → prayer_text
      3. Greeting — the entity's welcome, drawn from the prayer, printed on the panel
      4. Dialogue loop — question → answer → one screen, until 'e'
      5. Farewell — a last screen, then the panel is cleared

    There is no voice cloning and no playback: the entity is mute here and speaks
    only in print. One utterance is one full-panel refresh, roughly 20 seconds.
    """
    prayer_audio = PRAYER_WAV
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
            record_prayer(session, PRAYER_WAV)
            check(session)
            if not os.path.exists(PRAYER_WAV) or is_silent(PRAYER_WAV):
                print("  (nothing was offered — returning to idle)\n")
                return

        print("  [Transcribing...]")
        # plugs.entity_travel(True, 1)
        prayer_text, prayer_lang = transcribe(client, prayer_audio, language=LANGUAGE_OVERRIDE)
        check(session)
        if not prayer_text:
            print("  (no speech found in the prayer — returning to idle)\n")
            return
        print(f"  Prayer received. [{prayer_lang}]\n")

        # ── GREETING ──────────────────────────────────────────────────────────
        session.stage = DIALOGUE
        history: list[types.Content] = []  # running conversation, in Gemini's format
        # plugs.entity_travel(False, 1)
        greeting = ask_entity(client, [], GREETING_REQUEST, prayer_text, language=prayer_lang)
        check(session)
        print(f"\n  They: {greeting}\n")
        # plugs.entity_travel(False, 2)
        display.show(greeting)
        check(session)
        print("\nSpeak your question, or press 'e' to end the ritual.\n")

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

            question, q_lang = transcribe(client, QUESTION_WAV, language=LANGUAGE_OVERRIDE)
            os.remove(QUESTION_WAV)
            check(session)

            if not question:
                print("  (nothing intelligible — try again)\n")
                continue

            print(f"  You: {question}")
            print("  [They speak...]")
            answer = ask_entity(client, history, question, prayer_text, language=q_lang)
            check(session)
            print(f"\n  They: {answer}\n")

            display.show(answer)
            check(session)

        # ── FAREWELL ──────────────────────────────────────────────────────────
        session.stage = FAREWELL
        print("  [They depart...]")
        # plugs.entity_travel(True, 1)
        farewell = ask_entity(client, history, FAREWELL_REQUEST, prayer_text, language=prayer_lang)
        check(session)
        print(f"\n  They: {farewell}\n")
        display.show(farewell)
        # plugs.entity_travel(False, 1)
        # plugs.entity_travel(True, 2)

    except RitualAborted:
        pass
    except Exception as e:
        # A session must never take the installation down with it.
        print(f"\n  [Ritual failed: {type(e).__name__}: {e}]\n")
    finally:
        # Always clean up — recordings are deleted even if the ritual was interrupted
        for path in (PRAYER_WAV, QUESTION_WAV):
            if os.path.exists(path):
                os.remove(path)
        try:
            # One last refresh returns the panel to its resting state, so no
            # seeker's answer is left on display for the next one to read.
            if IDLE_TEXT.strip():
                display.show(IDLE_TEXT)
            else:
                display.clear()
        except Exception as e:
            print(f"  [display cleanup failed: {e}]")
        session.stage = IDLE
        print("  [Ritual cleared.]\n")


def main() -> None:
    # Operator event loop: waits for 'o' to unlock, runs a session, repeats.
    client = gemini.make_client()
    print(f"  [entity: {gemini.MODEL}]")

    with Display() as display, raw_keyboard():
        while True:
            print(
                "\nPress 'o' to begin the ritual...\n"
                "  o  begin the prayer / end the prayer / hold to pause a question\n"
                "  e  end the ritual (the entity gives a farewell)\n"
                "  i  interrupt and reset to idle\n"
            )

            while True:
                if read_key() == "o":
                    break

            print("  [Ritual unlocked.]\n")

            session = Session()
            # control.attach_buttons(session)   # GPIO buttons — see control.py
            ritual_thread = threading.Thread(
                target=run_ritual, args=(client, display, session), daemon=True
            )
            ritual_thread.start()

            last_o = 0.0
            while ritual_thread.is_alive():
                key = read_key()
                if key in ("i", "p"):
                    # Interrupt: signal abort and unblock any recording in progress
                    print("\n  [Ritual interrupted. Restarting...]\n")
                    session.abort.set()
                    session.stop.set()
                    session.paused.clear()
                    ritual_thread.join()
                    break
                elif key == "e":
                    # End the session — the entity answers once more and departs
                    if session.stage == DIALOGUE:
                        print("\n  [Ending — the entity will say its farewell.]\n")
                        session.end.set()
                    else:
                        print("\n  ['e' only ends the ritual during the dialogue.]\n")
                elif key == "o":
                    if session.stage == PRAYER:
                        # Finish the prayer recording
                        session.stop.set()
                    elif not session.paused.is_set():
                        # Hold 'o' to pause question recording; release to resume
                        print("\n  [Recording paused]\n")
                        session.paused.set()
                    last_o = time.monotonic()

                # Release (no 'o' for >0.15s) resumes recording
                if session.paused.is_set() and time.monotonic() - last_o > 0.15:
                    session.paused.clear()
                    print("\n  [Recording resumed]\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  [Exiting.]\n")
