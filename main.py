"""
The ritual. The covenant waits on the panel; once it is given, the seeker prays,
the Entity borrows their voice and draws near, speaks with them for as long as
they wish, bids them farewell, and gives the voice back. No trace remains.

    python3 main.py
"""

from __future__ import annotations

import os
import random
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
# Before the rest: they read their settings from the environment as they wake.
load_dotenv(os.path.join(HERE, ".env"))

import anthropic
import numpy as np
from elevenlabs.client import ElevenLabs

import consent
import echo
import entity
import listen
import plugs
import voice
from audio import (
    NO_BED,
    PLAYBACK_RATE,
    PRAYER_SAMPLE_RATE,
    choose_bell,
    compress_for_ivc,
    is_silent,
    play,
    play_stream,
    receive_prayer,
    receive_question,
    toll,
)
from control import (
    ARRIVAL,
    DIALOGUE,
    FAREWELL,
    IDLE,
    PRAYER,
    RitualBroken,
    Session,
    abide,
    button_pipe,
    raw_keyboard,
    read_key,
)
from display import BACKGROUND, BLACK, BLUE, GOLD, RED, TEXT_COLOR, WHITE, Display
from entity import CHARACTER_COUNTED, entity_answers, inscribe
from prompts import FAREWELL_REQUEST, GREETING_REQUEST, IMPATIENCE

DEBUG_WAV = os.path.join(HERE, "debug.wav")         # if it exists, it is the prayer
PRAYER_WAV = os.path.join(HERE, "prayer.wav")
PRAYER_UPLOAD_WAV = os.path.join(HERE, "prayer_upload.wav")   # made small, to be lent
QUESTION_WAV = os.path.join(HERE, "question.wav")

# The Entity that already knows: on this share of questions it cuts the seeker off
# once enough is said, an amount drawn afresh each time, so it cannot be learned.
FOREKNOW_CHANCE = 0.35
FOREKNOW_AT = {False: (7, 14), True: (16, 30)}   # words, or characters (CHARACTER_COUNTED)

# The Entity takes its time. The bell never tolls sooner than this after the
# prayer, however quickly the voice is borrowed, unless the seeker hastens it.
ARRIVAL_SECS = 60.0

# (ink, ground) for each screen. The prayer is blue, for the words are the seeker's;
# the greeting vermilion, the red of shrine gates and seals; the farewell gold on
# black. The Entity's answers are black on white, as clear as the panel allows.
Style = tuple[tuple[int, int, int], tuple[int, int, int]]
PRAYER_STYLE = (BLUE, BACKGROUND)
TRANSITION_STYLE = (WHITE, BLACK)       # the dark before the Entity arrives
LISTENING_STYLE = (TEXT_COLOR, WHITE)   # blank while the seeker speaks,
HEARD_STYLE = (TEXT_COLOR, RED)         # red the moment the question is taken
GREETING_STYLE = (RED, BACKGROUND)
FAREWELL_STYLE = (GOLD, BLACK)

DEFAULT_LANGUAGE = os.getenv("LANGUAGE_OVERRIDE") or consent.DEFAULT_LANGUAGE


def words_heard(text: str, language: str) -> int:
    return len(text.replace(" ", "")) if language in CHARACTER_COUNTED else len(text.split())


def borrowed_voice(eleven: ElevenLabs | None, voice_id: str | None, text: str) -> Iterator[bytes] | None:
    # text in the seeker's voice, as it flows; None when there is no voice to borrow.
    if eleven is None or voice_id is None:
        return None
    return voice.speak(eleven, voice_id, text)


class Panel:
    """
    One screen, drawn in the background, so its twenty seconds pass beside the
    voice or the listening. join() raises what went wrong.

    after: the screen before. This one waits behind it, and since a repaint once
    begun cannot be stopped, a screen not yet begun when a newer one comes (the
    red of a question already answered) is never drawn at all.
    """

    def __init__(
        self,
        display: Display,
        text: str,
        style: Style,
        after: Panel | None = None,
        dense: bool = False,
    ) -> None:
        self.failure: BaseException | None = None
        self.started = threading.Event()
        self.skipped = False
        self.lock = threading.Lock()
        if after is not None:
            after.supersede()
        self.thread = threading.Thread(
            target=self.run, args=(display, text, style, after, dense), daemon=True
        )
        self.thread.start()

    def supersede(self) -> None:
        with self.lock:
            if not self.started.is_set():
                self.skipped = True

    def run(self, display: Display, text: str, style: Style, after: Panel | None, dense: bool) -> None:
        if after is not None:
            after.thread.join()
        with self.lock:
            self.started.set()
            if self.skipped:
                return
        try:
            display.show(text, *style, dense=dense)
        except BaseException as e:
            self.failure = e

    def join(self) -> None:
        self.thread.join()
        if self.failure is not None:
            raise self.failure


def utter(
    display: Display,
    mind: anthropic.Anthropic,
    eleven: ElevenLabs | None,
    voice_id: str | None,
    text: str,
    language: str,
    *stops: threading.Event,
    style: Style = (TEXT_COLOR, BACKGROUND),
    after: Panel | None = None,
    bed: np.ndarray = NO_BED,
    voice_offset_secs: float = 0.0,
    reverb: bool = True,
) -> None:
    """
    The Entity speaks text, aloud and in verse, each the moment it is ready and
    neither waiting for the other: the voice about a second after the words exist,
    the verse as soon as it is written. The two arrive apart, as from something
    present but not quite here. bed sounds beneath the voice from the start (the
    greeting's bell), the voice entering voice_offset_secs later. The echo is told
    at once, and follows a few seconds behind.
    """
    if voice_id is not None:
        echo.utter(text)
    sounding = threading.Thread(
        target=play_stream,
        args=(borrowed_voice(eleven, voice_id, text), *stops),
        kwargs={"bed": bed, "voice_offset_secs": voice_offset_secs, "reverb": reverb},
        daemon=True,
    )
    sounding.start()
    try:
        verse = inscribe(mind, text, language)
    except Exception as e:
        print(f"  [no verse could be written: {type(e).__name__}: {e}]")
        verse = text
    print(f"  Panel: {verse!r}")
    panel = Panel(display, verse, style, after=after)
    sounding.join()
    panel.join()


class Ritual:
    """
    One ritual, in the tongue the covenant was given in:

      pray      the invocation on the panel, received until a double press
      arrive    the prayer heard, the voice borrowed, the Entity drawing near
      greet     a bell, and three seconds in, the Entity's first words
      converse  each question found by the silence around it, and answered
      depart    the last words, three bells, and the voice given back

    However it ends, clear() leaves no trace. Without ElevenLabs's voice, the
    ritual goes on in words and bells alone.
    """

    def __init__(
        self,
        mind: anthropic.Anthropic,
        eleven: ElevenLabs,
        display: Display,
        session: Session,
        language: str,
    ) -> None:
        self.mind = mind
        self.eleven = eleven
        self.display = display
        self.session = session
        self.language = language
        self.bell = choose_bell()
        self.voice_id: str | None = None
        self.prayer_audio = PRAYER_WAV
        self.prayer_text = ""
        self.prayer_ended = 0.0
        self.hearing: listen.Listener | None = None
        self.history: list[dict] = []
        self.invitation: Panel | None = None
        self.transition: Panel | None = None
        self.wordless: Panel | None = None   # the latest blank or red screen of the dialogue

    def hold(self) -> None:
        try:
            if self.pray() and self.arrive():
                self.greet()
                self.converse()
                self.depart()
        except RitualBroken:
            pass
        except Exception as e:
            # A ritual must never take the installation down with it.
            print(f"\n  [The ritual failed: {type(e).__name__}: {e}]\n")
            self.withdraw()
        finally:
            self.clear()

    def utter(self, text: str, *stops: threading.Event, **kwargs) -> None:
        utter(self.display, self.mind, self.eleven, self.voice_id, text, self.language,
              *stops, **kwargs)

    def answer(self, question: str, history: list[dict] | None = None, **kwargs) -> str:
        answer = entity_answers(self.mind, self.history if history is None else history,
                                question, self.prayer_text, language=self.language, **kwargs)
        abide(self.session)
        return answer

    def show_wordless(self, style: Style) -> None:
        self.wordless = Panel(self.display, "", style, after=self.wordless)

    # ── The prayer ────────────────────────────────────────────────────────────

    def pray(self) -> bool:
        # The prayer, received. False if nothing was offered.
        if os.path.exists(DEBUG_WAV):
            self.session.stage = PRAYER
            print("\n  [debug.wav is offered as the prayer]")
            self.prayer_audio = DEBUG_WAV
            return True

        # Nothing is heard until the invocation stands whole on the panel.
        self.invitation = Panel(self.display, consent.PRAYERS[self.language], PRAYER_STYLE, dense=True)
        print("\n  [The invocation appears...]")
        while self.invitation.thread.is_alive():
            self.invitation.thread.join(0.1)
            abide(self.session)
        self.session.stage = PRAYER
        print("\n  [The prayer is received. A double press ends it.]\n")
        self.hearing = listen.Listener(self.eleven, self.language, rate=PRAYER_SAMPLE_RATE)
        try:
            receive_prayer(self.session, PRAYER_WAV, hear=self.hearing.send)
        finally:
            if self.session.abort.is_set():
                self.hearing.close()
        abide(self.session)
        if not os.path.exists(PRAYER_WAV) or is_silent(PRAYER_WAV):
            self.hearing.close()
            print("  (nothing was offered)\n")
            return False
        return True

    # ── The arrival ───────────────────────────────────────────────────────────

    def arrive(self) -> bool:
        # The prayer heard and the voice borrowed, side by side, while the Entity
        # draws near on the wind. False if no words were found in the prayer.
        self.prayer_ended = time.monotonic()
        self.session.stage = ARRIVAL
        self.transition = Panel(self.display, consent.RECEIVED[self.language], TRANSITION_STYLE,
                                after=self.invitation)
        plugs.fans(True)
        with ThreadPoolExecutor(max_workers=1) as pool:
            borrowing = pool.submit(self.borrow_voice)
            print("  [The prayer is heard...]")
            try:
                self.prayer_text = self.hearing.finish() if self.hearing is not None else ""
                if not self.prayer_text:
                    self.prayer_text = listen.transcribe(self.eleven, self.prayer_audio,
                                                         language=self.language)
            finally:
                # Awaited even if the prayer is empty or the ritual broken, so a
                # voice borrowed for nothing is still given back.
                self.voice_id = borrowing.result()
        abide(self.session)
        if not self.prayer_text:
            print("  (no words were found in the prayer)\n")
            return False
        print(f"  [The prayer is heard. ({self.language})]\n")
        if self.voice_id is not None:
            echo.start(self.voice_id, self.language)
        return True

    def borrow_voice(self) -> str | None:
        if self.eleven is None:
            return None
        try:
            started = time.monotonic()
            compress_for_ivc(self.prayer_audio, PRAYER_UPLOAD_WAV)
            borrowed = voice.borrow_voice(self.eleven, PRAYER_UPLOAD_WAV)
            print(f"  [borrowed in {time.monotonic() - started:.1f}s]")
            return borrowed
        except Exception as e:
            print(f"  [the voice could not be borrowed — words alone: {type(e).__name__}: {e}]")
            return None

    # ── The greeting ──────────────────────────────────────────────────────────

    def greet(self) -> None:
        greeting = None
        if not self.session.hasten.is_set():
            greeting = self.answer(GREETING_REQUEST, history=[])
        self.await_hour()
        if self.session.hasten.is_set():
            greeting = self.answer(GREETING_REQUEST + IMPATIENCE, history=[])
        plugs.fans(False)
        print(f"\n  The Entity: {greeting}\n")
        plugs.lights(False)   # the Entity is here: the world without it goes dark
        self.utter(greeting, self.session.abort, style=GREETING_STYLE, after=self.transition,
                   bed=toll(self.bell), voice_offset_secs=3.0)
        abide(self.session)
        # Only now may the seeker end it: a press while the Entity drew near only
        # hastened it, and is never taken for a farewell.
        self.session.stage = DIALOGUE
        print("\nSpeak a question, or double press to end.\n")

    def await_hour(self) -> None:
        # The Entity comes no sooner than ARRIVAL_SECS after the prayer, unless hastened.
        waiting = ARRIVAL_SECS - (time.monotonic() - self.prayer_ended)
        if waiting <= 0 or self.session.hasten.is_set():
            return
        print(f"  [the Entity arrives in {waiting:.0f}s — a double press hastens it]")
        deadline = time.monotonic() + waiting
        while time.monotonic() < deadline and not self.session.hasten.is_set():
            self.session.abort.wait(0.1)
            abide(self.session)

    # ── The dialogue ──────────────────────────────────────────────────────────

    def converse(self) -> None:
        while True:
            heard = self.hear_question()
            if heard is None:
                return
            question, cut = heard
            if not question:
                print("  (nothing could be understood)\n")
                continue
            print(f"  The seeker: {question}{'…  [cut in]' if cut else ''}")
            print("  [The Entity speaks...]")
            answer = self.answer(question, foreknown=cut)
            print(f"\n  The Entity: {answer}\n")
            # A double press cuts the answer short
            self.utter(answer, self.session.abort, self.session.end, after=self.wordless)
            self.wordless = None
            abide(self.session)

    def hear_question(self) -> tuple[str, bool] | None:
        """
        One question, and whether the Entity cut in on it; None if the seeker took
        leave instead. Now and then the Entity does not wait for the end: once
        enough has been said, it answers what the seeker was about to ask.
        """
        live = listen.Listener(self.eleven, self.language, context=self.prayer_text)
        foreknowing = random.random() < FOREKNOW_CHANCE
        enough = random.randint(*FOREKNOW_AT[self.language in CHARACTER_COUNTED])
        cut = False

        def cut_in() -> bool:
            nonlocal cut
            cut = words_heard(live.heard, self.language) >= enough
            return cut

        ended = receive_question(
            self.session, QUESTION_WAV,
            on_start=lambda: self.show_wordless(LISTENING_STYLE),
            on_end=lambda: self.show_wordless(HEARD_STYLE),
            hear=live.send,
            cut_in=cut_in if foreknowing else None,
        )
        if ended or self.session.abort.is_set():
            live.close()
        abide(self.session)
        if ended:
            return None

        if cut:
            question = live.heard
            live.close()
        else:
            question = live.finish()
            if not question and not is_silent(QUESTION_WAV):
                question = listen.transcribe(self.eleven, QUESTION_WAV, language=self.language)
        os.remove(QUESTION_WAV)
        abide(self.session)
        # The living ear may begin with a stray mark from the silence before speech
        return question.lstrip("。、.,!?！？ 　").strip(), cut

    # ── The farewell ──────────────────────────────────────────────────────────

    def depart(self) -> None:
        self.session.stage = FAREWELL
        print("  [The Entity departs...]")
        plugs.fans(True)   # it departs on the wind it came in on
        farewell = self.answer(FAREWELL_REQUEST)
        print(f"\n  The Entity: {farewell}\n")
        # Its last words in the seeker's own voice, plain, without reverb.
        self.utter(farewell, self.session.abort, style=FAREWELL_STYLE, after=self.wordless,
                   reverb=False)
        self.wordless = None
        plugs.fans(False)
        abide(self.session)
        bells = toll(self.bell, times=3, gap_min=5.5, gap_max=7.0)
        echo.fade(len(bells) / PLAYBACK_RATE)
        play(bells, self.session.abort)
        plugs.lights(True)   # it is gone: the world returns

    # ── The ending, however it came ───────────────────────────────────────────

    def withdraw(self) -> None:
        # The Entity withdraws in its own words, after any screen still being drawn.
        try:
            Panel(self.display, consent.FAILED[self.language], TRANSITION_STYLE,
                  after=self.wordless or self.transition or self.invitation).join()
        except Exception as e:
            print(f"  [the panel failed: {e}]")

    def clear(self) -> None:
        # Nothing remains: the echo hears first, so it makes no echo in a voice
        # about to be given back.
        for panel in (self.invitation, self.transition, self.wordless):
            if panel is not None:
                try:
                    panel.join()
                except Exception as e:
                    print(f"  [the panel failed: {e}]")
        echo.end()
        if self.eleven is not None and self.voice_id is not None:
            voice.release_voice(self.eleven, self.voice_id)
        for path in (PRAYER_WAV, PRAYER_UPLOAD_WAV, QUESTION_WAV):
            if os.path.exists(path):
                os.remove(path)
        plugs.at_rest()
        self.session.stage = IDLE
        print("  [The ritual is cleared.]\n")


def hold_ritual(
    mind: anthropic.Anthropic,
    eleven: ElevenLabs,
    display: Display,
    session: Session,
    language: str,
) -> None:
    Ritual(mind, eleven, display, session, language).hold()


def keep_watch(session: Session, ritual: threading.Thread) -> None:
    # The keys and the button, given their meaning in the ritual, until it ends.
    while ritual.is_alive():
        key = read_key()
        if key in ("i", "p"):
            print("\n  [The ritual is broken. Beginning again...]\n")
            session.abort.set()
            ritual.join()
            break
        if key == "e":
            if session.stage == PRAYER:
                # A double press, not a single one: too easily given by chance
                # halfway through a minute of reading
                session.tap.set()
            elif session.stage == DIALOGUE:
                print("\n  [The seeker takes leave — the Entity will bid farewell.]\n")
                session.end.set()
            elif session.stage == ARRIVAL and not session.hasten.is_set():
                print("\n  [The seeker would not wait — the Entity comes now, in haste.]\n")
                session.hasten.set()
        elif key == "o" and session.stage == PRAYER:
            print("\n  [The prayer ends with a double press ('e').]\n")


def main() -> None:
    mind = entity.make_client()
    eleven = voice.make_client()
    voice.release_lingering_voices(eleven)
    plugs.at_rest()
    print(f"  [the Entity: {entity.MODEL} · the ear: {listen.MODEL_ID}]")

    language = DEFAULT_LANGUAGE
    with Display() as display, raw_keyboard(), button_pipe():
        while True:
            print(
                "\nThe covenant waits...\n"
                "  e  consent and begin the prayer / end the prayer / end the ritual (farewell)\n"
                "  i  turn the covenant to the other tongue / break the ritual and begin again\n"
                "  (PiSugar button: double tap = e, long press = i)\n"
            )
            language = consent.await_consent(display, language)
            print(f"  [The covenant is given. ({language})]\n")

            session = Session()
            ritual = threading.Thread(
                target=hold_ritual, args=(mind, eleven, display, session, language), daemon=True
            )
            ritual.start()
            try:
                keep_watch(session, ritual)
            except KeyboardInterrupt:
                # The ritual thread would die with the program, its voice still
                # borrowed: break it, and let it give the voice back first.
                print("\n  [Stopping — the ritual is cleared first...]")
                session.abort.set()
                ritual.join(timeout=60)
                raise


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  [The Entity withdraws.]\n")
    finally:
        plugs.flush()   # the room set back before the program goes
