from __future__ import annotations

import os
import random
import re

import anthropic

from display import BUDGET_CJK_CHARS, BUDGET_WORDS, fits, too_wide
from prompts import INSCRIPTION_PROMPT, build_system_prompt

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
# helpline. The prompt forbids all of it; this catches what gets through anyway.
# Sending the seeker to a doctor, a counsellor or a care manager is not among
# them: the entity is asked for exactly that kind of concrete course.
# "AI" is matched case-sensitively — "ai" sits inside "again" and "remain" — and
# by its neighbours rather than \b, which fails against kana ("AIです").
BREAK_RE = re.compile(
    r"\b(role[- ]?play\w*|persona|(out of|in|break\w*|my) character|step\w* (out|outside|back)"
    r"|artificial intelligence|language model|chatbot|computer program|art installation"
    r"|I (cannot|can't|can not) promise|helpline|hotline)\b"
    r"|ロールプレイ|キャラクター|人工知能|プログラム|インスタレーション|相談窓口|演じ",
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
    # The SDK retries dropped connections, 429 and 5xx itself, with backoff;
    # five tries rides out a short Wi-Fi drop instead of failing the session.
    return anthropic.Anthropic(max_retries=5)


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
    foreknown: bool = False,
) -> str:
    """
    Generate the Entity's response to the seeker's question.

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
            "and let their prayer tell you what they meant. Begin by naming, plainly and "
            "specifically, the question they were about to ask, as if you had heard it to the "
            "end — then answer it. Do not say that you knew it or that they need say no more: "
            "showing it is enough."
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
    r"|you('ve| have)? (gave|given)|tell me what)\b|詩|碑文|刻",
    re.IGNORECASE,
)


def format_inscription(text: str) -> str:
    # Clean the poem but keep its line breaks: here they are the verse.
    text = MARKDOWN_RE.sub("", TAG_RE.sub("", text))
    lines = (" ".join(line.split()).strip('"“”「」') for line in text.splitlines())
    return "\n".join(line for line in lines if line)


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
# surrounding speakers — has no text-only equivalent and is dropped here. When the
# audio layer comes back, port ask_echo() from base/tuhanKami/llm.py; it needs
# nothing from this module beyond gemini.generate().
