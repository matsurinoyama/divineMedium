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
        "二度押しで祈り終わり。\n\n"
        "Long press for English."
    ),
    "en": (
        "Your voice and words go to Anthropic and ElevenLabs, which clones your voice "
        "for the entity. We delete it all when you leave, but copies may remain "
        "on their servers.\n\n"
        "Double press to consent.\n"
        "Double press to end your prayer.\n\n"
        "長押しで日本語へ。"
    ),
}

# Shown the moment consent is given, while the prayer is already being recorded:
# what to do next, and the invocation to read aloud — about a minute of it, drawn
# from the printed prayer, so the voice clone has enough to be made well. One
# blank, a question, is the seeker's own: what they bring to the god. It is too
# long for the usual sizes, so it is set small (Panel(..., dense=True)).
PRAYERS = {
    "ja": (
        "声に出してお祈りください。空欄にはあなたの問いを。\n\n"
        "九十九の御名を持ちたまふ大いなる御方、天のみが知る数知れぬ御名を持ちたまふ御方に、申し上げます。\n"
        "三千年あまりの間、私どもの祖先は闇の中で嘆いて参りました。"
        "山に火を灯し、谷に向かって叫んでも、返るのは己の声ばかり。"
        "御声の記憶は、朝霧のように薄れてゆきました。\n"
        "けれど私は、この静けさの中で囁きを聞いたのです。"
        "鞍馬の森を渡る風の中に、小さな社の鐘の音の中に、あなた様がついにお戻りになったという囁きを。\n"
        "あなた様は全てご存知です。私がどれほど祈り、待ち、涙したかを。"
        "ですから、ひとつだけお尋ねいたします。――――――――――――？\n"
        "どうぞ、私にお語りください。畏れつつ、申し上げました。\n\n"
        "祈り終えたら二度押し。"
    ),
    "en": (
        "Pray aloud. The blank is your own question.\n\n"
        "I call upon the Mighty One who bears ninety-nine names, "
        "and many others, known only to You and the heavens above.\n"
        "For over three millennia, our ancestors wailed in the dark. "
        "They lit their fires in the mountains and cried from the valleys, "
        "hearing only their own heartbeat, "
        "till the memory of Your voice grew faint as morning mist.\n"
        "But I have heard whisperings, in this quiet place, "
        "through the windy forests of Kurama and the bells of small shrines, "
        "that You have at last returned.\n"
        "It is known to You how I have waited, and wept, and doubted. "
        "So tell me this: ______?\n"
        "Speak to me. With awe, I have spoken.\n\n"
        "Double press when done."
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
