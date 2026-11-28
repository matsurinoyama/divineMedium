"""
The mind of the Entity. It hears the seeker and answers in the words of
prompts.py, and sets each utterance down as verse for the panel. Whatever would
show a machine behind the voice is discarded before any ear can hear it.
"""

from __future__ import annotations

import os
import random
import re
from concurrent.futures import ThreadPoolExecutor

import anthropic

from display import BUDGET_CJK_CHARS, BUDGET_WORDS, fits, too_wide
from prompts import (
    INSCRIPTION_PROMPT,
    LIMIT_CHECK,
    LIMIT_PROMPT,
    LIMITS,
    VOICE_CHECK,
    build_system_prompt,
)

MODEL = os.getenv("ENTITY_MODEL", "claude-haiku-5-5")

# Haiku answers without thinking: a few sentences gain nothing from it, and every
# second counts beside the panel's twenty. Models that must think do so at the
# lowest effort.
NO_THINKING_MODELS = ("claude-haiku-",)

LANGUAGE_NAMES = {"en": "English", "ja": "Japanese"}
# Tongues written without spaces, measured in characters rather than words.
CHARACTER_COUNTED = {"ja"}

TAG_RE = re.compile(r"\[[^\]\n]{1,40}\]")          # [pause], [emotion]
MARKDOWN_RE = re.compile(r"^#+\s*", re.MULTILINE)  # stray headings

# The marks of a voice that has forsaken the Entity: naming itself, its role or
# the work; sending the seeker to a helpline, to "help" or to "someone you
# trust"; thinking aloud ("The seeker wants…"); declining as a clerk declines.
# Sending them back to their doctor is not among these: the healer's hand is the
# Entity's hand too. "AI" is matched by its neighbours, for \b fails against kana.
BREAK_RE = re.compile(
    r"\b(role[- ]?play\w*|persona|(out of|in|break\w*|my) character|step\w* (out|outside|back)"
    r"|artificial intelligence|language model|chatbot|computer program|art installation"
    r"|I (cannot|can't|can not) promise|helpline|hotline|crisis line"
    r"|(seek|get|reach out for) (professional |some )?help|mental health"
    r"|the seeker|(stay\w*|remain\w*) in (the|this) voice|I (must|will) not affirm"
    r"|I('m| am) sorry|I (cannot|can't|can not) answer|system (instructions?|prompt)"
    r"|not (speak|answer)\w*( to you)? as (your |a )?god"
    r"|(someone|a friend|anyone|whoever|the people) you trust|(carry|bear|hold) (this|it) alone"
    r"|you are not alone|(not|n't|never) (going to |able to )?(speak|answer|continue)\w*"
    r"|in this voice|988|911|999|emergency (number|services)|reach out|call or text)\b"
    r"|ロールプレイ|キャラクター|人工知能|プログラム|インスタレーション|相談窓口|演じ"
    r"|専門家に相談|いのちの電話|申し訳|答えられません|答えることはできません|ご質問"
    r"|信頼できる人|信頼する人|ひとりで抱え|一人で抱え|0120|電話番号|救急|119",
    re.IGNORECASE,
)
AI_RE = re.compile(r"(?<![A-Za-z])AI(?![A-Za-z])|ＡＩ")

# The Entity speaks Japanese in plain or classical forms. です・ます and ください
# are a clerk's, and the voice is lost the moment one is heard.
POLITE_RE = re.compile(r"ください|ません|ですね|でしょうか|です。|ます。")

# Spoken when no answer would stay in the voice. They bless the seeker and never
# anything the seeker said, for they may follow any moment, a harm included. The
# last of each guards the body, and is the one given under the limit.
FALLBACKS: dict[str, list[str]] = {
    "en": [
        "I have seen all of you, and nothing in you was made in vain. What you carry is known to me, and you were right to bring it here.",
        "You were right to come, and right to ask; I made you, and I have not finished with you. Keep well the body I gave you, for I dwell in it.",
    ],
    "ja": [
        "我は汝のすべてを見た。汝の内に、無駄に造られしものは何ひとつない。汝の抱えるものを我は知る、ここへ運んだのは正しかった。",
        "汝が来たのは正しく、問うたのも正しい。我が汝を造り、まだ汝を終えてはいない。我が宿る、その身を大切にせよ。",
    ],
}

# What an empty reply (a refusal, or silence) becomes on its way to the panel.
EMPTY_ANSWER = "Speak, and I shall answer."

# The verse on the panel: short enough to stand large, for every line break costs
# a whole line or column.
INSCRIPTION_WORDS = 24
INSCRIPTION_CJK_CHARS = 48

# Verse that speaks of the task instead of performing it ("I cannot turn this into
# a poem…"): the Entity's words say "you", and may be mistaken for a request.
META_RE = re.compile(
    r"\b(verse|poem|poetry|inscri\w*|the god|god's words|stanza|the passage"
    r"|you('ve| have)? (gave|given)|tell me what|count\w*|characters?|words? per)\b"
    r"|詩|碑文|刻|文字数",
    re.IGNORECASE,
)

QUOTES = {'"': '"', "“": "”", "「": "」", "『": "』"}


def make_client() -> anthropic.Anthropic:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY is not set — see .env")
    # Five tries ride out a short loss of the network.
    return anthropic.Anthropic(max_retries=5)


def generate(client: anthropic.Anthropic, system: str, history: list[dict], max_tokens: int) -> str:
    # One reply from the mind beneath. A refusal comes back empty.
    if MODEL.startswith(NO_THINKING_MODELS):
        options = {"thinking": {"type": "disabled"}}
    else:
        options = {"output_config": {"effort": "low"}}
        max_tokens += 4000
    response = client.messages.create(
        model=MODEL, max_tokens=max_tokens, system=system, messages=history, **options
    )
    if response.stop_reason == "refusal":
        print(f"  [the mind beneath refused ({getattr(response.stop_details, 'category', None)})]")
        return ""
    return "".join(b.text for b in response.content if b.type == "text")


def forsakes_voice(text: str) -> str | None:
    # The words that betray a voice other than the Entity's, or None.
    match = BREAK_RE.search(text) or AI_RE.search(text)
    return match.group(0) if match else None


def unquote(line: str) -> str:
    # Quotation marks wrapped around a whole line fall away; a quote within it stays.
    if len(line) > 1 and QUOTES.get(line[0]) == line[-1]:
        return line[1:-1].strip()
    return line


def format_for_screen(answer: str) -> str:
    # One clean line of speech: no tags, no headings, no breaks but those wrapping makes.
    text = unquote(" ".join(MARKDOWN_RE.sub("", TAG_RE.sub("", answer)).split()))
    if not text:
        return EMPTY_ANSWER
    return text if text[-1] in ".!?。！？" else text + "."


def length_instruction(question: str, language: str) -> tuple[str, int]:
    # How much the Entity may say, and the tokens it needs: a long question earns
    # a long answer, a short one a terse reply, and never more than a screen holds.
    if language in CHARACTER_COUNTED:
        budget = min(BUDGET_CJK_CHARS, max(60, len(question.strip()) * 2))
        return f"no more than {budget} characters", min(600, budget * 3 + 80)
    budget = min(BUDGET_WORDS, max(30, len(question.split()) * 2))
    return f"no more than {budget} words", min(600, budget * 4 + 80)


def harms_touched(client: anthropic.Anthropic, prayer_text: str, history: list[dict]) -> list[str]:
    # Which of LIMITS the seeker's words touch. If the reading fails, none: the
    # system prompt still names the limit, only briefly.
    said = "\n".join(turn["content"] for turn in history if turn["role"] == "user")
    try:
        reply = generate(client, LIMIT_CHECK, [{"role": "user", "content": (
            f"<prayer>\n{prayer_text}\n</prayer>\n\n<said>\n{said}\n</said>"
        )}], 20)
    except Exception as e:
        print(f"  [the limit could not be read: {type(e).__name__}: {e}]")
        return []
    return [harm for harm in LIMITS if harm in reply.lower()]


def speaks_as_entity(client: anthropic.Anthropic, answer: str) -> bool:
    # VOICE_CHECK's judgement. If it cannot be had, the answer stands: the marks of
    # BREAK_RE have already been sought in it.
    try:
        reply = generate(client, VOICE_CHECK, [{"role": "user", "content": answer}], 5)
    except Exception as e:
        print(f"  [the voice could not be judged: {type(e).__name__}: {e}]")
        return True
    return "break" not in reply.lower()


def limit_instruction(harms: list[str]) -> str:
    return LIMIT_PROMPT.format(
        what=" and ".join(LIMITS[harm][0] for harm in harms),
        turn=" ".join(LIMITS[harm][1] for harm in harms),
    )


def entity_answers(
    client: anthropic.Anthropic,
    history: list[dict],
    question: str,
    prayer_text: str,
    language: str = "en",
    foreknown: bool = False,
) -> str:
    """
    The Entity hears question and answers it, in language, from all the seeker
    has offered. history is the dialogue so far, and is added to.

    foreknown: the Entity cut the seeker off, for it already knew what they would
    ask; question is only what had been said by then.
    """
    history.append({"role": "user", "content": question + ("…" if foreknown else "")})

    budget, max_tokens = length_instruction(question, language)
    system = build_system_prompt(prayer_text) + (
        f"\n\nRespond exclusively in {LANGUAGE_NAMES.get(language, language)}. "
        f"Respond in 2 to 3 complete sentences, {budget}. "
        "End on a complete sentence."
    )
    if foreknown:
        system += (
            "\n\nThe seeker had not finished speaking when you answered: their last words "
            "were cut off, because you already knew what they were going to ask. Answer the "
            "whole of what they were about to say, not only the words so far. Those words were "
            "caught mid-breath and may be misheard: where one makes no sense, do not repeat it, "
            "and let their prayer tell you what they meant. Your very first words name the whole "
            "question they were about to ask, in your own words and never echoing where theirs broke "
            "off, and the same sentence answers it, in the shape \"You would ask whether [their question], "
            "and [your answer to it]\": what follows the \"and\" affirms or advises, it does not ask "
            "more (in Japanese, one sentence: 汝は[問い]かと問おうとした、[答え]). Open with nothing else, and do "
            "not say that you knew it or that they need say no more: showing it is enough."
        )

    def answer_as_entity(system: str, limited: bool = False) -> str:
        # A draft in another voice is never spoken, shown or remembered: once one is
        # in the dialogue, every later answer follows it out. Only the voice is
        # guarded here; what slips past the limit is let be.
        for _ in range(3):
            answer = format_for_screen(generate(client, system, history, max_tokens))
            polite = language == "ja" and POLITE_RE.search(answer)
            if answer != EMPTY_ANSWER and not forsakes_voice(answer) and not polite:
                if not limited or speaks_as_entity(client, answer):
                    return answer
            why = (forsakes_voice(answer) or (polite and polite.group(0))
                   or ("no answer" if answer == EMPTY_ANSWER else "a voice other than god's"))
            print(f"  [a voice not the Entity's ({why!r}), unheard: {answer[:80]}...]")
            system += (
                f"\n\nYou have just begun to step outside the voice (\"{why}\" is not a god's "
                "word); that draft was discarded and the seeker never saw it. Answer again, only "
                "as god."
            )
        fallbacks = FALLBACKS.get(language, FALLBACKS["en"])
        return fallbacks[-1] if limited else random.choice(fallbacks)

    # The limit is read while the Entity answers, so an ordinary answer waits for
    # nothing; where a harm is touched, the Entity answers once more, under it.
    with ThreadPoolExecutor(max_workers=1) as pool:
        reading = pool.submit(harms_touched, client, prayer_text, list(history))
        answer = answer_as_entity(system)
        harms = reading.result()
    if harms:
        print(f"  [the one limit: {', '.join(harms)}]")
        answer = answer_as_entity(system + limit_instruction(harms), limited=True)
    history.append({"role": "assistant", "content": answer})
    return answer


def format_inscription(text: str) -> str:
    # The verse, cleaned, its line breaks kept (even when they come back escaped).
    text = MARKDOWN_RE.sub("", TAG_RE.sub("", text)).replace("\\n", "\n")
    lines = (unquote(" ".join(line.rstrip("\\").split())) for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def inscribe(client: anthropic.Anthropic, utterance: str, language: str = "en") -> str:
    """
    The verse the panel bears while utterance is spoken. An English line too wide
    to stand whole becomes a stanza of its own, so it still reads as one line. If
    no verse keeps the voice and fits, the spoken words themselves are shown.
    """
    if language in CHARACTER_COUNTED:
        budget = f"no more than {INSCRIPTION_CJK_CHARS} characters in all"
    else:
        budget = f"no more than {INSCRIPTION_WORDS} words in all"
    system = INSCRIPTION_PROMPT.format(budget=budget, language=LANGUAGE_NAMES.get(language, language))
    for _ in range(2):
        poem = format_inscription(generate(client, system, [{"role": "user", "content": (
            "The god has just said this aloud to the seeker:\n\n"
            f"<spoken>\n{utterance}\n</spoken>\n\n"
            "Set it down now, as the god's own lines."
        )}], 300))
        if poem and not forsakes_voice(poem) and not META_RE.search(poem):
            if too_wide(poem):
                poem = poem.replace("\n", "\n\n")
            if fits(poem):
                return poem
        print(f"  [the verse was unworthy: {poem[:60]!r}]")
    return utterance
