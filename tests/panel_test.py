#!/usr/bin/env python3
"""
Words written to the panel, or to screen.png, with no ritual held.

  python tests/panel_test.py                     # an answer in English
  python tests/panel_test.py --ja                # an answer in Japanese
  python tests/panel_test.py "your words here"
  python tests/panel_test.py --budget            # BUDGET_* weighed against the panel
  python tests/panel_test.py --real "words"      # to the panel itself

Without --real, always to screen.png, so the writing can be tuned off the Pi.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import display
from display import (
    BUDGET_CJK_CHARS,
    BUDGET_WORDS,
    Display,
    FONT_SIZES,
    layout,
    layout_vertical,
)

SAMPLE_EN = (
    "Have I not already placed the work in your hands? You ask for a door where "
    "there is only a wall; turn, and walk the corridor you have refused to see. "
    "Three mornings from now, the name you are waiting for will be spoken aloud."
)

SAMPLE_JA = (
    "汝の求めるものは、すでに汝の手の中にある。壁を叩くのをやめ、"
    "背を向けていた道を歩むがよい。三度目の朝に、待ちわびた名が告げられるであろう。"
)


# Filler, to measure how much the panel holds rather than how it reads. ORNATE is
# the longest the Entity's words truly run, short words still among the long.
ORDINARY = "have I not placed the work in your hands turn and walk the way you refused"
ORNATE = (
    "the abundant blessing I promised is already yours and the troubles that "
    "trouble you have been forgiven since the beginning"
)
# No true sentence is like this; it is here to prove the cutting back works.
PATHOLOGICAL = "intercession unconditionally transfiguration remembrance"


def fill(source: str, words: int) -> str:
    pool = source.split()
    return " ".join((pool * (words // len(pool) + 1))[:words])


def budget_report() -> None:
    """
    The budgets, weighed against the font itself. A budget is sound when words of
    that length stand without being cut: the floor size may be reached; TRIMMED
    may not, and then the budget in display.py must be lowered.
    """
    print(f"  panel {display.EPD_WIDTH}x{display.EPD_HEIGHT}, sizes {FONT_SIZES}")
    cases = [
        ("ordinary", fill(ORDINARY, BUDGET_WORDS), f"{BUDGET_WORDS} words", True),
        ("ornate", fill(ORNATE, BUDGET_WORDS), f"{BUDGET_WORDS} words", True),
        ("japanese", "祈りの言葉" * (BUDGET_CJK_CHARS // 5), f"{BUDGET_CJK_CHARS} chars", True),
        ("worst case", fill(PATHOLOGICAL, BUDGET_WORDS), f"{BUDGET_WORDS} words", False),
    ]
    for name, text, label, must_fit in cases:
        # Japanese is weighed in columns, as it is written.
        vertical = display.is_vertical(text)
        pieces, font = (layout_vertical if vertical else layout)(text)
        joined = ("" if vertical else " ").join(pieces)
        trimmed = len(joined.replace(" ", "")) < len(text.replace(" ", ""))
        if trimmed:
            note = "  <- TRIMMED, lower the budget" if must_fit else "  <- trimmed (expected)"
        elif int(font.size) == FONT_SIZES[-1]:
            note = "  <- at the floor size"
        else:
            note = ""
        unit = "columns" if vertical else "lines"
        print(
            f"  {name:10} {label:9} {len(text):3} chars -> "
            f"{int(font.size)}px, {len(pieces):2} {unit}{note}"
        )


def main() -> None:
    args = sys.argv[1:]
    if "--budget" in args:
        budget_report()
        return

    real = "--real" in args
    args = [a for a in args if a != "--real"]
    if "--ja" in args:
        text = SAMPLE_JA
    else:
        words = [a for a in args if not a.startswith("--")]
        text = " ".join(words) if words else SAMPLE_EN

    Display(mock=not real).show(text)


if __name__ == "__main__":
    main()
