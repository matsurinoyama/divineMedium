from __future__ import annotations

from control import read_key
from display import Display

# The panel's resting state between sessions. Nothing is recorded until the seeker
# consents with a double press; a long press switches the form between Japanese
# and English, and whichever language the seeker consents in is the language the
# entity hears and answers in for the whole session. The switch is offered in the
# language it switches to, so the person who needs it can read it.
#
# Each form must fit one screen, and both already fill it at the smallest font
# size: every line break costs a whole column (Japanese, set vertically) or line
# (English), and a blank line — the paragraph break — half of one more. If a
# form overflows, the panel trims it at a sentence end and the button
# instructions are the first thing lost. Check a change with
# `python debug_display.py "$(python -c 'import consent; print(consent.FORMS["en"])')"`.
FORMS = {
    "ja": (
        "声と言葉をアンソロピックとイレブンラボへ送り、声を写して神が語ります。"
        "録音と声は終わりに消しますが、両社のサーバーに残る場合があります。\n\n"
        "二度押しで同意。\n"
        "一度押しで祈り終わり。\n\n"
        "Long press for English."
    ),
    "en": (
        "Your voice and words go to Anthropic and ElevenLabs, which clones your voice "
        "for the entity. We delete it all when you leave, but copies may remain "
        "on their servers.\n\n"
        "Double press to consent.\n"
        "Tap once to end your prayer.\n\n"
        "長押しで日本語へ。"
    ),
}

# Shown the moment consent is given, while the prayer is already being recorded:
# what to do next, and a short prayer to read aloud with three blanks to fill —
# a condensed form of the full invocation, so that starting is not daunting. The
# same one-screen limit applies: the Japanese fills the screen, which is why it
# has no counterpart to "Speak to me."
PRAYERS = {
    "ja": (
        "空欄を埋め、声に出してお祈りください。\n\n"
        "数知れぬ御名を持つ大いなる御方よ、\n"
        "私は――――。\n"
        "このごろずっと――――。\n"
        "心の奥で、――――と問うております。\n"
        "畏れつつ、申し上げました。\n\n"
        "祈り終えたら一度押し。"
    ),
    "en": (
        "Pray aloud, filling in the blanks.\n\n"
        "Mighty One of countless names, I have waited long for You.\n"
        "I am ______.\n"
        "I have been ______.\n"
        "In my heart, I ask ______.\n"
        "Speak to me.\n"
        "With awe, I have spoken.\n\n"
        "Tap once when done."
    ),
}

# Shown the moment the prayer ends, while it is transcribed, the voice is cloned
# and the greeting is prepared — some twenty seconds in which the panel would
# otherwise still show the prayer and nothing would seem to happen. The entity's
# first words, before it has any: an acknowledgement and a command to wait.
RECEIVED = {
    "ja": "祈りは聞き届けられた。\n\n静かに待て。\n我は近づいている。",
    "en": "Your prayer is heard.\n\nBe still, and wait.\nI am drawing near.",
}

# Shown when a session cannot go on (the network gone for longer than the
# retries wait, a service down): the entity withdraws rather than the panel
# simply returning to the consent form, which reads as a fault.
FAILED = {
    "ja": "今は、声が届かぬ。\n\n時を置いて、\nまた来なさい。",
    "en": "The way is clouded.\n\nCome again,\nin a little while.",
}

DEFAULT_LANGUAGE = "ja"


def show(display: Display, language: str) -> None:
    # A panel failure must not stop the installation from taking consent: the
    # terminal still shows what the form says.
    try:
        display.show(FORMS[language])
    except Exception as e:
        print(f"  [consent form could not be shown: {e}]")


def wait_for_consent(display: Display, language: str) -> str:
    # Show the form and block until consent. Returns the language consented in.
    if language not in FORMS:
        language = DEFAULT_LANGUAGE
    show(display, language)
    while True:
        key = read_key()
        if key == "e":
            return language
        if key in ("i", "p"):
            language = "en" if language == "ja" else "ja"
            print(f"  [consent form: {language}]")
            show(display, language)
