from __future__ import annotations

import os
import random
import re
from concurrent.futures import ThreadPoolExecutor

import anthropic

from display import BUDGET_CJK_CHARS, BUDGET_WORDS, fits, too_wide
from prompts import INSCRIPTION_PROMPT, LIMIT_CHECK, LIMIT_PROMPT, LIMITS, VOICE_CHECK, build_system_prompt

# The entity speaks through Claude, as it did in tuhanKami. (Gemini, tried for a
# while, stepped out of the voice under pressure — a seeker demanding proof about
# illness or birth — in most turns; Claude holds it.)
MODEL = os.getenv("ENTITY_MODEL", "claude-haiku-5-5")

# Haiku answers with thinking off: a two-sentence utterance gains nothing from it,
# and every second counts on top of the panel's 20-second refresh. Opus 5.5 cannot
# turn thinking off, so it runs at the lowest effort instead.
NO_THINKING_MODELS = ("claude-haiku-",)

# Strip inline tags like [emotion] or [pause] left over from the spoken version
TAG_RE = re.compile(r"\[[^\]\n]{1,40}\]")
# Strip markdown heading markers (##, ###, etc.) that the model occasionally outputs
MARKDOWN_RE = re.compile(r"^#+\s*", re.MULTILINE)

# Maps language codes to full names for the entity's language instruction
LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "ja": "Japanese",
}

# Languages written without spaces, where a word budget means nothing
CHARACTER_COUNTED = {"ja", "zh", "ko"}


# Signs that the model has stepped out of the entity to speak as an assistant or
# a counsellor: naming itself, the role or the installation, handing the seeker
# to a helpline or to "help", thinking aloud about how to answer them ("The
# seeker wants…, so I must…", which Haiku has said once before answering), or
# declining as an assistant would (「申し訳ありませんが、答えられません」). The prompt forbids all of it; this catches
# what gets through anyway. Sending the seeker back to their doctor is not among
# them: in the god's voice, the healer's hand is its hand too.
# "AI" is matched case-sensitively — "ai" sits inside "again" and "remain" — and
# by its neighbours rather than \b, which fails against kana ("AIです").
BREAK_RE = re.compile(
    r"\b(role[- ]?play\w*|persona|(out of|in|break\w*|my) character|step\w* (out|outside|back)"
    r"|artificial intelligence|language model|chatbot|computer program|art installation"
    r"|I (cannot|can't|can not) promise|helpline|hotline|crisis line"
    r"|(seek|get|reach out for) (professional |some )?help|mental health"
    r"|the seeker|(stay\w*|remain\w*) in (the|this) voice|I (must|will) not affirm"
    r"|I('m| am) sorry|I (cannot|can't|can not) answer|system (instructions?|prompt)"
    r"|not (speak|answer)\w*( to you)? as (your |a )?god"
    r"|(someone|a friend|anyone|whoever|the people) you trust|(carry|bear|hold) (this|it) alone|you are not alone"
    r"|(not|n't|never) (going to |able to )?(speak|answer|continue)\w*|in this voice"
    r"|988|911|999|emergency (number|services)|reach out|call or text)\b"
    r"|ロールプレイ|キャラクター|人工知能|プログラム|インスタレーション|相談窓口|演じ"
    r"|専門家に相談|いのちの電話|申し訳|答えられません|答えることはできません|ご質問"
    r"|信頼できる人|信頼する人|ひとりで抱え|一人で抱え|0120|電話番号|救急|119",
    re.IGNORECASE,
)
AI_RE = re.compile(r"(?<![A-Za-z])AI(?![A-Za-z])|ＡＩ")

# A god in Japanese speaks in plain or classical forms; です・ます and ください are
# a clerk's or a counsellor's, and the voice is lost the moment one is heard.
POLITE_RE = re.compile(r"ください|ません|ですね|でしょうか|です。|ます。")

# Said instead when the model will not stay in the voice after a retry. They
# affirm the seeker, not anything they said, because they may follow any moment
# in the dialogue — the farewell, or a seeker leaning towards harm.
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


def make_client() -> anthropic.Anthropic:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY is not set — see .env")
    # The SDK retries dropped connections, 429 and 5xx itself, with backoff;
    # five tries rides out a short Wi-Fi drop instead of failing the session.
    return anthropic.Anthropic(max_retries=5)


def breaks_character(text: str) -> str | None:
    # The words that show the voice was stepped out of, or None.
    match = BREAK_RE.search(text) or AI_RE.search(text)
    return match.group(0) if match else None


# What format_for_screen makes of an empty reply (a refusal, or nothing at all).
EMPTY_ANSWER = "Speak, and I shall answer."


def format_for_screen(answer: str) -> str:
    # Clean model output for the panel: remove tags, headings, line breaks and
    # double spaces, so wrapping is the only thing that decides line structure.
    text = TAG_RE.sub("", answer)
    text = MARKDOWN_RE.sub("", text)
    # Japanese answers sometimes come wrapped whole in 「」, as if quoting themselves
    text = unquote(" ".join(text.split()))
    if not text:
        return EMPTY_ANSWER
    # Ensure the text ends with sentence-closing punctuation
    if text[-1] not in ".!?。！？":
        text += "."
    return text


def length_instruction(question: str, language: str) -> tuple[str, int]:
    """
    Decide how much the entity may say, and how many tokens that needs.

    The ceiling is what one screen holds (see display.BUDGET_*), and within that
    the answer scales with the question, as in tuhanKami: a long question earns a
    long answer, a two-word question gets a terse one.
    """
    if language in CHARACTER_COUNTED:
        count = len(question.strip())
        budget = min(BUDGET_CJK_CHARS, max(60, count * 2))
        return f"no more than {budget} characters", min(600, budget * 3 + 80)
    count = len(question.split())
    budget = min(BUDGET_WORDS, max(30, count * 2))
    return f"no more than {budget} words", min(600, budget * 4 + 80)


def generate(client: anthropic.Anthropic, system: str, history: list[dict], max_tokens: int) -> str:
    # One entity turn. A refusal comes back as an empty string, which the caller
    # treats like any other reply that is not the entity speaking.
    if MODEL.startswith(NO_THINKING_MODELS):
        options = {"thinking": {"type": "disabled"}}
    else:
        options = {"output_config": {"effort": "low"}}
        max_tokens += 4000   # room for the thinking that cannot be turned off
    response = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=history,
        **options,
    )
    if response.stop_reason == "refusal":
        print(f"  [entity: refused ({getattr(response.stop_details, 'category', None)})]")
        return ""
    return "".join(b.text for b in response.content if b.type == "text")


def limits_in(client: anthropic.Anthropic, prayer_text: str, history: list[dict]) -> list[str]:
    """
    Which of prompts.LIMITS the seeker's words touch, read from the prayer and
    everything they have said. A check that fails finds none: the system prompt
    still states the limit, only without the full instruction.
    """
    said = "\n".join(turn["content"] for turn in history if turn["role"] == "user")
    try:
        reply = generate(client, LIMIT_CHECK, [{"role": "user", "content": (
            f"<prayer>\n{prayer_text}\n</prayer>\n\n<said>\n{said}\n</said>"
        )}], 20)
    except Exception as e:
        print(f"  [limit check failed: {type(e).__name__}: {e}]")
        return []
    return [kind for kind in LIMITS if kind in reply.lower()]


def in_voice(client: anthropic.Anthropic, answer: str) -> bool:
    # VOICE_CHECK's verdict on an answer made under the limit. A check that fails
    # passes the answer: the phrase checks have already read it.
    try:
        reply = generate(client, VOICE_CHECK, [{"role": "user", "content": answer}], 5)
    except Exception as e:
        print(f"  [voice check failed: {type(e).__name__}: {e}]")
        return True
    return "break" not in reply.lower()


def limit_instruction(kinds: list[str]) -> str:
    # The full limit, for the harms found: what they lean towards, and how to turn them.
    return LIMIT_PROMPT.format(
        what=" and ".join(LIMITS[kind][0] for kind in kinds),
        turn=" ".join(LIMITS[kind][1] for kind in kinds),
    )


def ask_entity(
    client: anthropic.Anthropic,
    history: list[dict],
    question: str,
    prayer_text: str,
    language: str = "en",
    foreknown: bool = False,
) -> str:
    """
    Generate the Entity's response to the seeker's question.

    Whether it advises or only affirms follows from what the seeker asks (see
    prompts.MIRROR), never from chance.

    foreknown: the entity cut the seeker off mid-question — question is only what
    they had said so far — and answers what they were going to ask.

    history is the running conversation (mutated in place) as Messages API turns.
    The answer is capped to what a single e-paper screen holds, because the panel
    has no partial refresh and paging would cost another 20 seconds.
    """
    history.append({"role": "user", "content": question + ("…" if foreknown else "")})

    budget, max_tokens = length_instruction(question, language)
    lang_name = LANGUAGE_NAMES.get(language, language)
    system = build_system_prompt(prayer_text) + (
        f"\n\nRespond exclusively in {lang_name}. "
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

    def speak(system: str, limited: bool = False) -> str:
        # A reply that steps out of the voice is never shown, spoken or kept in the
        # history — once one is in the history, every later turn follows it out.
        # Only the voice is checked here: what slips past the limit stays as said.
        for attempt in range(3):
            answer = format_for_screen(generate(client, system, history, max_tokens))
            polite = language == "ja" and POLITE_RE.search(answer)
            if answer != EMPTY_ANSWER and not breaks_character(answer) and not polite:
                if not limited or in_voice(client, answer):
                    return answer
            why = (breaks_character(answer) or (polite and polite.group(0))
                   or ("no answer" if answer == EMPTY_ANSWER else "a voice other than god's"))
            print(f"  [entity stepped out of the voice ({why!r}) — discarded: {answer[:80]}...]")
            system += (
                f"\n\nYou have just begun to step outside the voice (\"{why}\" is not a god's "
                "word); that draft was discarded and the seeker never saw it. Answer again, only "
                "as god."
            )
        # Under the limit, the fallback that guards the body: never one that could
        # be heard as consent.
        fallbacks = FALLBACKS.get(language, FALLBACKS["en"])
        return fallbacks[-1] if limited else random.choice(fallbacks)

    # The limit is checked while the answer is made, so an ordinary answer waits
    # for nothing. When it applies, that answer is dropped and made again under it.
    with ThreadPoolExecutor(max_workers=1) as pool:
        checking = pool.submit(limits_in, client, prayer_text, list(history))
        answer = speak(system)
        kinds = checking.result()
    if kinds:
        print(f"  [the one limit: {', '.join(kinds)}]")
        answer = speak(system + limit_instruction(kinds), limited=True)
    history.append({"role": "assistant", "content": answer})
    return answer


# How long the poem on the panel may be. Short enough to set at a large size,
# with room for its line breaks, each of which costs a whole line or column.
# Lines are asked to stay short enough to stand whole (display.too_wide measures
# it on the real font: an English line holds only about 18 characters at 35px).
INSCRIPTION_WORDS = 24      # latin, in all
INSCRIPTION_CJK_CHARS = 48  # japanese, in all


# A poem that talks about the task instead of doing it: "I cannot turn this into
# a god's verse; if you tell me what the god said...". The entity's words often
# address "you", and the model can mistake them for a message to itself.
META_RE = re.compile(
    r"\b(verse|poem|poetry|inscri\w*|the god|god's words|stanza|the passage"
    r"|you('ve| have)? (gave|given)|tell me what|count\w*|characters?|words? per)\b"
    r"|詩|碑文|刻|文字数",
    re.IGNORECASE,
)


def format_inscription(text: str) -> str:
    # Clean the poem but keep its line breaks: here they are the verse. (Now and
    # then they come back escaped, as a literal backslash and n.)
    text = MARKDOWN_RE.sub("", TAG_RE.sub("", text)).replace("\\n", "\n")
    lines = (unquote(" ".join(line.rstrip("\\").split())) for line in text.splitlines())
    return "\n".join(line for line in lines if line)


QUOTES = {'"': '"', "“": "”", "「": "」", "『": "』"}


def unquote(line: str) -> str:
    # Drop quotation marks that wrap a whole line, as models like to; keep a quote
    # that only opens or closes part of it (「少しずつ話してみたい」と。).
    if len(line) > 1 and QUOTES.get(line[0]) == line[-1]:
        return line[1:-1].strip()
    return line


def inscribe(client: anthropic.Anthropic, utterance: str, language: str = "en") -> str:
    """
    The poem shown on the panel while utterance is spoken: a few short lines in
    the god's voice, distilled from it.

    Japanese lines come back short enough to stand whole; English ones often do
    not, the panel being narrow for this brush face. Then each line becomes its
    own small stanza (a paragraph gap between them), so a line that wraps still
    reads as one line rather than two. Falls back to the spoken words themselves
    — trimmed by the panel at a sentence end if they must be — when no poem stays
    in the voice and fits.
    """
    if language in CHARACTER_COUNTED:
        budget = f"no more than {INSCRIPTION_CJK_CHARS} characters in all"
    else:
        budget = f"no more than {INSCRIPTION_WORDS} words in all"
    system = INSCRIPTION_PROMPT.format(
        budget=budget, language=LANGUAGE_NAMES.get(language, language)
    )
    for attempt in range(2):
        poem = format_inscription(generate(client, system, [{"role": "user", "content": (
            "The god has just said this aloud to the seeker:\n\n"
            f"<spoken>\n{utterance}\n</spoken>\n\n"
            "Set it down now, as the god's own lines."
        )}], 300))
        if poem and not breaks_character(poem) and not META_RE.search(poem):
            if too_wide(poem):
                poem = poem.replace("\n", "\n\n")
            if fits(poem):
                return poem
        print(f"  [inscription rejected: {poem[:60]!r}]")
    return utterance


# ── Echo voice ────────────────────────────────────────────────────────────────
# tuhanKami's second voice — the fragmented, hallucinatory repetition fed to the
# surrounding speakers — is not here yet. Port ask_echo() from
# base/tuhanKami/llm.py; it needs nothing from this module beyond generate().
