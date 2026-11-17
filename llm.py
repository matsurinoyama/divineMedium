from __future__ import annotations

import re

from google import genai
from google.genai import types

import gemini
from display import BUDGET_CJK_CHARS, BUDGET_WORDS
from prompts import build_system_prompt

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


def format_for_screen(answer: str) -> str:
    # Clean model output for the panel: remove tags, headings, line breaks and
    # double spaces, so wrapping is the only thing that decides line structure.
    text = TAG_RE.sub("", answer)
    text = MARKDOWN_RE.sub("", text)
    text = " ".join(text.split())
    if not text:
        return "Speak, and I shall answer."
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


def ask_entity(
    client: genai.Client,
    history: list[types.Content],
    question: str,
    prayer_text: str,
    language: str = "en",
) -> str:
    """
    Generate the Entity's response to the seeker's question.

    history is the running conversation (mutated in place) in Gemini's Content
    format. The answer is capped to what a single e-paper screen holds, because
    the panel has no partial refresh and paging would cost another 20 seconds.
    """
    history.append(types.Content(role="user", parts=[types.Part(text=question)]))

    budget, max_tokens = length_instruction(question, language)
    lang_name = LANGUAGE_NAMES.get(language, language)
    system = build_system_prompt(prayer_text) + (
        f"\n\nRespond exclusively in {lang_name}. "
        f"Respond in 2 to 3 complete sentences, {budget}. "
        "End on a complete sentence."
    )

    raw = gemini.generate(
        client,
        history,
        label="entity",
        system=system,
        max_tokens=max_tokens,
    )
    answer = format_for_screen(raw)
    history.append(types.Content(role="model", parts=[types.Part(text=answer)]))
    return answer


# ── Echo voice ────────────────────────────────────────────────────────────────
# tuhanKami's second voice — the fragmented, hallucinatory repetition fed to the
# surrounding speakers — has no text-only equivalent and is dropped here. When the
# audio layer comes back, port ask_echo() from base/tuhanKami/llm.py; it needs
# nothing from this module beyond gemini.generate().
