#!/usr/bin/env python3
"""
Render text to the panel, or to screen.png, without running a ritual.

  python debug_display.py                     # a sample english answer
  python debug_display.py --ja                # a sample japanese answer
  python debug_display.py "your text here"    # anything you like
  python debug_display.py --budget            # check BUDGET_* against the geometry
  python debug_display.py --real "text"       # push it to the actual panel

Without --real it always writes screen.png, so layout can be tuned on a laptop.
"""

from __future__ import annotations

import sys

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


# Filler of a given average word length, for measuring capacity rather than taste.
# ORNATE is the realistic ceiling for this prompt's register — long words, but
# still carrying the short function words that any English sentence must have.
ORDINARY = "have I not placed the work in your hands turn and walk the way you refused"
ORNATE = (
    "the abundant blessing I promised is already yours and the troubles that "
    "trouble you have been forgiven since the beginning"
)
# No real sentence looks like this; it is here to prove the trim backstop fires.
PATHOLOGICAL = "intercession unconditionally transfiguration remembrance"


def fill(source: str, words: int) -> str:
    pool = source.split()
    return " ".join((pool * (words // len(pool) + 1))[:words])


def budget_report() -> None:
    """
    Check the budgets against the real font metrics.

    A budget is sound when prose at that length still fits without being trimmed.
    Landing on the floor size is acceptable; being TRIMMED is not — lower the
    budget in display.py until it is not.
    """
    print(f"  panel {display.EPD_WIDTH}x{display.EPD_HEIGHT}, sizes {FONT_SIZES}")
    cases = [
        ("ordinary", fill(ORDINARY, BUDGET_WORDS), f"{BUDGET_WORDS} words", True),
        ("ornate", fill(ORNATE, BUDGET_WORDS), f"{BUDGET_WORDS} words", True),
        ("japanese", "祈りの言葉" * (BUDGET_CJK_CHARS // 5), f"{BUDGET_CJK_CHARS} chars", True),
        ("worst case", fill(PATHOLOGICAL, BUDGET_WORDS), f"{BUDGET_WORDS} words", False),
    ]
    for name, text, label, must_fit in cases:
        # Japanese is measured through the vertical path, which is how it is set.
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
