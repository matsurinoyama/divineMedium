from __future__ import annotations

import os
import random
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv
import anthropic
import numpy as np
from elevenlabs.client import ElevenLabs

# Before the local imports: llm, audio and display read their settings from the
# environment at import time.
load_dotenv()

import consent
import listen
import llm
import tts
# import plugs                      # smart plugs — see the commented calls below
from audio import (
    PRAYER_SAMPLE_RATE,
    bell_mix,
    compress_for_ivc,
    is_silent,
    play,
    play_stream,
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
from llm import CHARACTER_COUNTED, ask_entity, inscribe
from prompts import FAREWELL_REQUEST, GREETING_REQUEST

# File paths used during a session
DEBUG_WAV = "debug.wav"        # if present, skips prayer recording and uses this file
PRAYER_WAV = "prayer.wav"      # recorded prayer audio
QUESTION_WAV = "question.wav"  # recorded question audio
PRAYER_UPLOAD_WAV = "prayer_upload.wav"  # prayer downsampled for the voice clone

# The entity that already knows: on this share of questions it cuts the seeker
# off once they have said enough — a number of words (or, in Japanese,
# characters) drawn afresh each time, so the moment cannot be learned.
FOREKNOW_CHANCE = 0.35
FOREKNOW_AT = {False: (7, 14), True: (16, 30)}   # by CHARACTER_COUNTED


def said(text: str, language: str) -> int:
    # How much of a question has been heard: words, or characters where there are no spaces.
    return len(text.replace(" ", "")) if language in CHARACTER_COUNTED else len(text.split())

# (text, background) for the screens that are not answers. The prayer the seeker
# reads is blue — their words, not the entity's. The greeting arrives in
# vermilion, the red of shrine gates and seals; the farewell is gold on black.
# Answers stay black on white, the most legible the panel gets.
PRAYER_STYLE = (BLUE, BACKGROUND)
# The wait between the prayer and the greeting: white on black, the dark before
# the voice arrives.
TRANSITION_STYLE = (WHITE, BLACK)
# A question, without words: the panel goes blank while the seeker speaks, and
# turns red the moment their question is taken.
LISTENING_STYLE = (TEXT_COLOR, WHITE)
HEARD_STYLE = (TEXT_COLOR, RED)
GREETING_STYLE = (RED, BACKGROUND)
FAREWELL_STYLE = (GOLD, BLACK)

# The language the consent form opens in. The seeker can switch it with a long
# press, and the language they consent in is the session's language.
DEFAULT_LANGUAGE = os.getenv("LANGUAGE_OVERRIDE") or consent.DEFAULT_LANGUAGE


def speech(eleven: ElevenLabs | None, voice_id: str | None, text: str) -> Iterator[bytes] | None:
    # The utterance as it streams in the cloned voice, or None when there is no
    # voice this session.
    if eleven is None or voice_id is None:
        return None
    return tts.speak_stream(eleven, voice_id, text)


Style = tuple[tuple[int, int, int], tuple[int, int, int]]


class Panel:
    """
    One panel refresh run in the background, so the ~20 seconds it takes can
    overlap with playback or recording. join() re-raises a failed refresh.

    after: the screen before this one. This one waits its turn behind it, and —
    since a refresh cannot be stopped once it has begun — if that screen has not
    yet begun drawing, it never will: a screen that is already out of date (the
    red of a question whose answer is ready) is skipped rather than shown late.
    """

    def __init__(
        self, display: Display, text: str, style: Style, after: Panel | None = None
    ) -> None:
        self.failure: BaseException | None = None
        self.started = threading.Event()   # drawing has begun (or it was skipped)
        self.skipped = False
        self.lock = threading.Lock()
        if after is not None:
            after.supersede()
        self.thread = threading.Thread(
            target=self.run, args=(display, text, style, after), daemon=True
        )
        self.thread.start()

    def supersede(self) -> None:
        # A newer screen has arrived: if this one has not begun drawing, drop it.
        with self.lock:
            if not self.started.is_set():
                self.skipped = True

    def run(self, display: Display, text: str, style: Style, after: Panel | None) -> None:
        if after is not None:
            after.thread.join()
        with self.lock:
            self.started.set()
            if self.skipped:
                return
        try:
            display.show(text, *style)
        except BaseException as e:
            self.failure = e

    def join(self) -> None:
        self.thread.join()
        if self.failure is not None:
            raise self.failure


def utter(
    display: Display,
    entity: anthropic.Anthropic,
    eleven: ElevenLabs | None,
    voice_id: str | None,
    text: str,
    language: str,
    *stops: threading.Event,
    style: Style = (TEXT_COLOR, BACKGROUND),
    after: Panel | None = None,
    bed: np.ndarray | None = None,
    voice_offset_secs: float = 0.0,
) -> None:
    """
    One utterance, written and spoken, each the moment it is ready and neither
    waiting for the other: the voice streams as it is synthesised, starting
    about a second after the text exists, while the poem is still being written;
    the poem starts drawing as soon as it is written (behind after, the screen
    before it — see Panel). The words and the voice arrive apart, as from
    something present but not quite here. Returns when both are done.

    bed is played under the voice from the start (the greeting's bell), the
    voice coming in voice_offset_secs later.
    """
    voice = threading.Thread(
        target=play_stream,
        args=(speech(eleven, voice_id, text), *stops),
        kwargs={"bed": bed, "voice_offset_secs": voice_offset_secs},
        daemon=True,
    )
    voice.start()
    try:
        poem = inscribe(entity, text, language)
    except Exception as e:
        print(f"  [inscription failed: {type(e).__name__}: {e}]")
        poem = text
    print(f"  Panel: {poem!r}")
    panel = Panel(display, poem, style, after=after)
    voice.join()
    panel.join()


def run_ritual(
    entity: anthropic.Anthropic,
    eleven: ElevenLabs,
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
    wordless: Panel | None = None   # the latest blank or red screen of the dialogue

    def show_wordless(style: Style) -> None:
        nonlocal wordless
        wordless = Panel(display, "", style, after=wordless)
    try:
        # plugs.entity_travel(False, 1)
        # plugs.entity_travel(True, 2)

        # ── PRAYER ────────────────────────────────────────────────────────────
        session.stage = PRAYER
        hearing: listen.Listener | None = None
        if os.path.exists(DEBUG_WAV):
            print("\n  [Debug mode enabled: using existing recording in debug.wav]")
            prayer_audio = DEBUG_WAV
        else:
            print("\n  [Prayer recording started. Press 'o' to finish.]\n")
            # The short prayer replaces the consent form while recording is
            # already running, so nothing said during the refresh is lost.
            invitation = Panel(display, consent.PRAYERS[language], PRAYER_STYLE)
            # Transcribed as it is spoken, so the words are there at the final tap
            hearing = listen.Listener(eleven, language, rate=PRAYER_SAMPLE_RATE)
            try:
                record_prayer(session, PRAYER_WAV, hear=hearing.send)
            finally:
                if session.abort.is_set():
                    hearing.close()
            check(session)
            if not os.path.exists(PRAYER_WAV) or is_silent(PRAYER_WAV):
                hearing.close()
                print("  (nothing was offered — returning to idle)\n")
                return

        # The prayer screen gives way to the wait, drawn while everything below runs
        transition = Panel(display, consent.RECEIVED[language], TRANSITION_STYLE, after=invitation)

        # ── TRANSCRIPTION AND VOICE CLONE, side by side ───────────────────────
        def clone() -> str | None:
            if eleven is None:
                return None
            try:
                compress_for_ivc(prayer_audio, PRAYER_UPLOAD_WAV)
                return tts.clone_voice(eleven, PRAYER_UPLOAD_WAV)
            except Exception as e:
                print(f"  [voice clone failed — continuing text-only: {type(e).__name__}: {e}]")
                return None

        # plugs.entity_travel(True, 1)
        with ThreadPoolExecutor(max_workers=1) as pool:
            cloning = pool.submit(clone)
            print("  [Transcribing...]")
            try:
                prayer_text = hearing.finish() if hearing is not None else ""
                if not prayer_text:
                    # No live transcript (or debug.wav): the recording itself
                    prayer_text = listen.transcribe(eleven, prayer_audio, language=language)
                prayer_lang = language
            finally:
                # Waited for even when the prayer turns out empty or the session
                # ends, so that a clone made for nothing is still deleted below.
                voice_id = cloning.result()
        check(session)
        if not prayer_text:
            print("  (no speech found in the prayer — returning to idle)\n")
            return
        print(f"  Prayer received. [{prayer_lang}]\n")

        # ── GREETING ──────────────────────────────────────────────────────────
        session.stage = DIALOGUE
        history: list[dict] = []  # running conversation, as Messages API turns
        # plugs.entity_travel(False, 1)
        greeting = ask_entity(entity, [], GREETING_REQUEST, prayer_text, language=prayer_lang)
        check(session)
        print(f"\n  They: {greeting}\n")
        # plugs.entity_travel(False, 2)
        # The bell tolls once; the greeting rises out of it 3 seconds in
        utter(display, entity, eleven, voice_id, greeting, language, session.abort,
              style=GREETING_STYLE, after=transition,
              bed=bell_mix(bell), voice_offset_secs=3.0)
        check(session)
        print("\nSpeak a question, or double press to end.\n")

        # ── DIALOGUE LOOP ─────────────────────────────────────────────────────
        while True:
            # The question is transcribed live as it is spoken. Now and then the
            # entity does not wait for the end: once enough has been said, it cuts
            # in and answers what the seeker was about to ask.
            live = listen.Listener(eleven, language, context=prayer_text)
            foreknowing = random.random() < FOREKNOW_CHANCE
            enough = random.randint(*FOREKNOW_AT[language in CHARACTER_COUNTED])
            cut = False

            def cut_in() -> bool:
                nonlocal cut
                cut = said(live.heard, language) >= enough
                return cut

            ended = record_question(
                session, QUESTION_WAV,
                on_start=lambda: show_wordless(LISTENING_STYLE),
                on_end=lambda: show_wordless(HEARD_STYLE),
                hear=live.send,
                cut_in=cut_in if foreknowing else None,
            )
            if ended or session.abort.is_set():
                live.close()
            check(session)
            if ended:
                break

            if cut:
                question = live.heard
                live.close()
            else:
                question = live.finish()
                if not question and not is_silent(QUESTION_WAV):
                    # No live transcript (no network, no ElevenLabs): the recording itself
                    question = listen.transcribe(eleven, QUESTION_WAV, language=language)
            os.remove(QUESTION_WAV)
            check(session)
            # Live transcripts can open on a stray mark from the silence before speech
            question = question.lstrip("。、.,!?！？ 　").strip()

            if not question:
                print("  (nothing intelligible — try again)\n")
                continue

            print(f"  You: {question}{'…  [cut in]' if cut else ''}")
            print("  [They speak...]")
            answer = ask_entity(entity, history, question, prayer_text, language=language,
                                foreknown=cut)
            check(session)
            print(f"\n  They: {answer}\n")
            # 'e' cuts the answer short, as a double clap did in tuhanKami
            utter(display, entity, eleven, voice_id, answer, language,
                  session.abort, session.end, after=wordless)
            wordless = None
            check(session)

        # ── FAREWELL ──────────────────────────────────────────────────────────
        session.stage = FAREWELL
        print("  [They depart...]")
        # plugs.entity_travel(True, 1)
        farewell = ask_entity(entity, history, FAREWELL_REQUEST, prayer_text, language=prayer_lang)
        check(session)
        print(f"\n  They: {farewell}\n")
        utter(display, entity, eleven, voice_id, farewell, language, session.abort,
              style=FAREWELL_STYLE, after=wordless)
        wordless = None
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
        try:
            # The entity withdraws in its own words, after any screen still drawing
            Panel(display, consent.FAILED[language], TRANSITION_STYLE,
                  after=wordless or transition or invitation).join()
        except Exception as e:
            print(f"  [display failed: {e}]")
    finally:
        # Let a refresh still in flight finish before main() draws the consent form
        for panel in (invitation, transition, wordless):
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
    entity = llm.make_client()
    eleven = tts.make_client()
    # Clones a crash or a power cut kept the last session from deleting
    tts.sweep(eleven)
    print(f"  [entity: {llm.MODEL} · transcription: {listen.MODEL_ID}]")

    language = DEFAULT_LANGUAGE
    with Display() as display, raw_keyboard(), button_pipe():
        while True:
            print(
                "\nWaiting for consent...\n"
                "  e  consent and begin the prayer / end the ritual (farewell)\n"
                "  i  switch the form between Japanese and English / interrupt and reset\n"
                "  o  end the prayer\n"
                "  (PiSugar button: tap = o, double tap = e, long press = i)\n"
            )

            language = consent.wait_for_consent(display, language)
            print(f"  [Consent given — ritual unlocked. ({language})]\n")

            session = Session()
            # control.attach_buttons(session)   # GPIO buttons — see control.py
            ritual_thread = threading.Thread(
                target=run_ritual, args=(entity, eleven, display, session, language), daemon=True
            )
            ritual_thread.start()

            try:
                watch(session, ritual_thread)
            except KeyboardInterrupt:
                # The ritual thread is a daemon and would die with the program,
                # skipping its cleanup — and leaving the seeker's cloned voice on
                # ElevenLabs. Stop it and let the cleanup run before exiting.
                print("\n  [Stopping — clearing the session first...]")
                session.abort.set()
                ritual_thread.join(timeout=60)
                raise


def watch(session: Session, ritual_thread: threading.Thread) -> None:
    # Route keys and button taps to the session until the ritual thread ends.
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
        elif key == "o" and session.stage == PRAYER:
            # Ends the prayer (questions are found by speech detection)
            session.tap.set()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  [Exiting.]\n")
