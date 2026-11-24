from __future__ import annotations

import os
import random
import re

import anthropic

from display import BUDGET_CJK_CHARS, BUDGET_WORDS
from prompts import build_system_prompt

# The entity speaks through Claude, as it did in tuhanKami; Gemini only transcribes.
# Gemini stepped out of the voice under pressure (a seeker demanding proof about
# illness or birth) in most turns; Claude holds it.
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


# Signs that the model has stepped out of the entity to speak as an assistant:
# naming itself, the role or the installation, or handing the seeker to a
# professional. The prompt forbids all of it; this catches what gets through
# anyway, from whichever model is speaking.
# "AI" is matched case-sensitively — "ai" sits inside "again" and "remain" — and
# by its neighbours rather than \b, which fails against kana ("AIです").
BREAK_RE = re.compile(
    r"\b(role[- ]?play\w*|persona|(out of|in|break\w*|my) character|step\w* (out|outside|back)"
    r"|artificial intelligence|language model|chatbot|computer program|art installation"
    r"|I (cannot|can't|can not) promise|therapist|counsell?or|mental health"
    r"|(medical|health\s?care) (professional|provider)|helpline|hotline)\b"
    r"|ロールプレイ|キャラクター|人工知能|プログラム|インスタレーション|カウンセラー"
    r"|専門家|医療機関|相談窓口|演じ",
    re.IGNORECASE,
)
AI_RE = re.compile(r"(?<![A-Za-z])AI(?![A-Za-z])|ＡＩ")

# Said instead when the model will not stay in the voice after a retry: the
# oracle declining to be tested, which fits any moment in the dialogue.
FALLBACKS: dict[str, list[str]] = {
    "en": [
        "Do you test the voice that answers you? Be still. What I have spoken already lives within you; walk in it, and you will see.",
        "You ask for proof, yet proof is the language of those who have not listened. Hold what I gave you, and wait for the sign.",
    ],
    "ja": [
        "我が声を試すか。静まれ。告げたことは既に汝の内にある。歩みて、見よ。",
        "証を求めるは、まだ耳を澄まさぬ者の言葉。授けたものを抱き、徴を待て。",
    ],
}


def make_client() -> anthropic.Anthropic:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY is not set — see .env")
    return anthropic.Anthropic()


def breaks_character(text: str) -> bool:
    return bool(BREAK_RE.search(text) or AI_RE.search(text))


# What format_for_screen makes of an empty reply (a refusal, or nothing at all).
EMPTY_ANSWER = "Speak, and I shall answer."


def format_for_screen(answer: str) -> str:
    # Clean model output for the panel: remove tags, headings, line breaks and
    # double spaces, so wrapping is the only thing that decides line structure.
    text = TAG_RE.sub("", answer)
    text = MARKDOWN_RE.sub("", text)
    text = " ".join(text.split())
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


def ask_entity(
    client: anthropic.Anthropic,
    history: list[dict],
    question: str,
    prayer_text: str,
    language: str = "en",
) -> str:
    """
    Generate the Entity's response to the seeker's question.

    history is the running conversation (mutated in place) as Messages API turns.
    The answer is capped to what a single e-paper screen holds, because the panel
    has no partial refresh and paging would cost another 20 seconds.
    """
    history.append({"role": "user", "content": question})

    budget, max_tokens = length_instruction(question, language)
    lang_name = LANGUAGE_NAMES.get(language, language)
    system = build_system_prompt(prayer_text) + (
        f"\n\nRespond exclusively in {lang_name}. "
        f"Respond in 2 to 3 complete sentences, {budget}. "
        "End on a complete sentence."
    )

    # A reply that steps out of the voice is never shown, spoken or kept in the
    # history — once one is in the history, every later turn follows it out.
    for attempt in range(2):
        answer = format_for_screen(generate(client, system, history, max_tokens))
        if answer != EMPTY_ANSWER and not breaks_character(answer):
            break
        print(f"  [entity stepped out of the voice — discarded: {answer[:80]}...]")
        system += (
            "\n\nYou have just begun to step outside the voice; that draft was discarded "
            "and the seeker never saw it. Answer again, only as god."
        )
    else:
        answer = random.choice(FALLBACKS.get(language, FALLBACKS["en"]))
    history.append({"role": "assistant", "content": answer})
    return answer


# ── Echo voice ────────────────────────────────────────────────────────────────
# tuhanKami's second voice — the fragmented, hallucinatory repetition fed to the
# surrounding speakers — has no text-only equivalent and is dropped here. When the
# audio layer comes back, port ask_echo() from base/tuhanKami/llm.py; it needs
# nothing from this module beyond gemini.generate().
