"""
The panel on which the Entity writes, in a brush hand: Japanese in columns, as an
oracle is written, latin across. It is slow — every screen a full repaint of some
twenty seconds — so it bears one utterance at a time and never anything else.
"""

from __future__ import annotations

import os
import re
import sys
import time
import types
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.realpath(__file__))
# The panel's own driver dwells in lib/.
LIBDIR = os.path.join(HERE, "lib")
if os.path.exists(LIBDIR):
    sys.path.append(LIBDIR)

# ── The panel ─────────────────────────────────────────────────────────────────
# Waveshare 4inch (E): 400x600, upright, and no partial refresh.
EPD_WIDTH = 400
EPD_HEIGHT = 600

# The colours the panel can truly lay down, as the driver's palette names them:
# any other is dithered into noise.
BLACK = (0, 0, 0)
WHITE = (255, 255, 255)
YELLOW = (255, 255, 0)
RED = (255, 0, 0)
BLUE = (0, 0, 255)

TEXT_COLOR = BLACK
BACKGROUND = WHITE
GOLD = YELLOW   # the panel knows no gold; yellow is the nearest it lays down flat

MARGIN_X = 30
MARGIN_Y = 40
LINE_SPACING = 1.38     # multiple of font size, between horizontal lines
COLUMN_SPACING = 1.5    # multiple of font size, between vertical columns
# A blank line ("\n\n") parts two stanzas by half a line, not a whole one.
PARAGRAPH_GAP = 0.5

# Largest first: the largest at which the words still fit is the one chosen.
# Below 28px the brush's thin strokes begin to fall out of the panel.
FONT_SIZES = [46, 40, 35, 31, 28]


@dataclass(frozen=True)
class Setting:
    sizes: list[int]          # the ladder, largest first
    line_spacing: float       # multiple of font size, between horizontal lines
    column_spacing: float     # multiple of font size, between vertical columns


NORMAL = Setting(FONT_SIZES, LINE_SPACING, COLUMN_SPACING)
# The prayer alone is set small and close: a minute of reading cannot stand on
# one screen at 28px. Only screens shown with dense=True; never the Entity's words.
DENSE = Setting([24, 22, 21, 20, 19, 18], line_spacing=1.22, column_spacing=1.2)

# A semi-cursive brush: the hand of something writing, not a machine printing.
FONT_PATH = os.getenv("FONT_PATH", os.path.join(HERE, "assets", "fonts", "FOT-ShinGyoReiStdN-R.otf"))
# ShinGyoRei writes latin in its own strange hand (its e is near an epsilon).
LATIN_FONT_PATH = os.getenv("LATIN_FONT_PATH", FONT_PATH)

# How much the Entity may say and still stand on one screen above the floor size,
# measured on this font and geometry by `python tests/panel_test.py --budget`.
BUDGET_WORDS = 34       # latin words
BUDGET_CJK_CHARS = 95   # japanese characters

# The panel hangs upside down; ROTATE=0 sets it upright. screen.png is always upright.
PANEL_ROTATION = int(os.getenv("ROTATE", "180"))

# Japanese is written in columns (縦書き), top to bottom, right to left.
# VERTICAL=always or never overrides what the words themselves decide.
VERTICAL_MODE = os.getenv("VERTICAL", "auto").lower()

# Kana and kanji: what makes a line Japanese. CJK_RE also takes shared punctuation.
JAPANESE_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿]")
# Ideographs, kana and fullwidth forms: each may be parted from the next.
CJK_RE = re.compile(
    r"[　-〿぀-ヿ㐀-䶿一-鿿豈-﫿＀-￯]"
)

# What may never open a line or a column (kinsoku).
NO_LINE_START = "、。，．,.!?!?)）]｝」』》〉…・ー:;:;"

# Turned a quarter turn when written in columns, as in print.
VERTICAL_ROTATE = set("ー〜～−—–‐-—…‥（）()｛｝{}「」『』【】〔〕［］[]〈〉《》＝=：；:;")
# Set in the upper right of their square when written in columns.
VERTICAL_TOP_RIGHT = set("、。，．,.")

_font_cache: dict[tuple[int, str], ImageFont.FreeTypeFont] = {}


def font(size: int, path: str | None = None) -> ImageFont.FreeTypeFont:
    key = (size, path or FONT_PATH)
    if key not in _font_cache:
        try:
            _font_cache[key] = ImageFont.truetype(key[1], size)
        except OSError as e:
            raise RuntimeError(
                f"Cannot load the display font at {key[1]} ({e}). Put "
                "FOT-ShinGyoReiStdN-R.otf in assets/fonts/, or point FONT_PATH at another "
                "face that covers both kana/kanji and latin."
            ) from e
    return _font_cache[key]


def is_vertical(text: str) -> bool:
    # In columns, when the words are mostly Japanese.
    if VERTICAL_MODE == "always":
        return True
    if VERTICAL_MODE == "never":
        return False
    stripped = "".join(text.split())
    if not stripped:
        return False
    japanese = sum(1 for ch in stripped if JAPANESE_RE.match(ch))
    return japanese >= max(4, len(stripped) * 0.3)


# ── Writing across ────────────────────────────────────────────────────────────


def tokens(text: str) -> list[str]:
    # The smallest pieces a line may part between: a latin word whole, each
    # Japanese character alone. " " may part; "\n" must; two "\n" part stanzas.
    out: list[str] = []
    buf = ""
    for ch in text:
        if ch == "\n":
            if buf:
                out.append(buf)
                buf = ""
            out.append("\n")
        elif ch.isspace():
            if buf:
                out.append(buf)
                buf = ""
            out.append(" ")
        elif CJK_RE.match(ch):
            if buf:
                out.append(buf)
                buf = ""
            out.append(ch)
        else:
            buf += ch
    if buf:
        out.append(buf)
    return out


def _hang(tok: str, f: ImageFont.FreeTypeFont, max_width: int) -> float:
    # Closing punctuation never opens a line: it may hang past the margin instead
    # (burasage), the overhang shared by both margins.
    return min(f.size, EPD_WIDTH - 8 - max_width) if tok in NO_LINE_START else 0


def _draws_back(lines: list[str], cur: str, tok: str, f: ImageFont.FreeTypeFont, limit: float) -> bool:
    # Closing punctuation pushed down alone is drawn back up, if the line above can bear it.
    return not cur and bool(lines) and tok in NO_LINE_START and f.getlength(lines[-1] + tok) <= limit


def _overruns(cur: str, tok: str, f: ImageFont.FreeTypeFont, limit: float) -> bool:
    # Whether tok would carry a line with words in it past its breadth.
    return bool(cur.strip()) and f.getlength((cur + tok).rstrip()) > limit


def wrap(text: str, f: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    # Lines filled one by one until the next piece would overrun the breadth.
    lines: list[str] = []
    cur = ""
    for tok in tokens(text):
        limit = max_width + _hang(tok, f, max_width)
        if tok == "\n":
            lines.append(cur.rstrip())
            cur = ""
        elif tok == " ":
            cur += " " if cur else ""
        elif _draws_back(lines, cur, tok, f, limit):
            lines[-1] += tok
        elif _overruns(cur, tok, f, limit):
            lines.append(cur.rstrip())
            cur = tok
        else:
            cur += tok
    if cur.strip():
        lines.append(cur.rstrip())
    return lines


def extent(units: list[str], pitch: int) -> int:
    # How deep the lines (or columns) reach: a pitch each, half for a stanza's gap.
    gaps = sum(1 for u in units if not u.strip())
    return int((len(units) - gaps) * pitch + gaps * pitch * PARAGRAPH_GAP)


def hard_lines(text: str) -> int:
    # The lines the words ask for themselves, the gaps between stanzas included.
    return text.count("\n") + 1


# Verse is kept whole only down to this size; below it, a larger hand with a line
# parted reads better than small whole lines.
UNBROKEN_MIN_SIZE = 35


def unbroken_passes(text: str) -> tuple[bool, ...]:
    # Verse is first fitted with every line whole; prose never is.
    return (True, False) if hard_lines(text) > 1 else (False,)


def sizes(unbroken: bool, ladder: list[int]) -> list[int]:
    # A ladder wholly below UNBROKEN_MIN_SIZE (the prayer's) keeps lines whole at every size.
    if not unbroken:
        return ladder
    return [s for s in ladder if s >= UNBROKEN_MIN_SIZE] or ladder


def layout(
    text: str, path: str | None = None, setting: Setting = NORMAL
) -> tuple[list[str], ImageFont.FreeTypeFont]:
    """
    The largest hand in which the words fit one screen, and the lines they make.
    Verse is first tried where none of its lines must part. If even the smallest
    overflows, the words are cut back to the last whole sentence that fits.
    """
    avail_w = EPD_WIDTH - 2 * MARGIN_X
    avail_h = EPD_HEIGHT - 2 * MARGIN_Y

    for unbroken in unbroken_passes(text):
        for size in sizes(unbroken, setting.sizes):
            f = font(size, path)
            lines = wrap(text, f, avail_w)
            if extent(lines, int(size * setting.line_spacing)) <= avail_h and (
                not unbroken or len(lines) == hard_lines(text)
            ):
                return lines, f

    # Too much to stand: cut back to what the smallest hand can hold.
    size = setting.sizes[-1]
    f = font(size, path)
    max_lines = max(1, avail_h // int(size * setting.line_spacing))
    lines = wrap(text, f, avail_w)
    kept = len(" ".join(lines[:max_lines]))
    trimmed = trim_to_sentence(text, kept)
    return wrap(trimmed, f, avail_w)[:max_lines], f


# ── Writing in columns (縦書き) ───────────────────────────────────────────────


def vertical_tokens(text: str, f: ImageFont.FreeTypeFont) -> list[tuple[str, int]]:
    # (piece, cells): a Japanese character fills one square; a latin word is
    # laid on its side, whole, across as many squares as it needs.
    cell = int(f.size)
    out: list[tuple[str, int]] = []
    buf = ""

    def flush() -> None:
        nonlocal buf
        if buf:
            out.append((buf, max(1, -(-int(f.getlength(buf)) // cell))))
            buf = ""

    for ch in text:
        if ch == "\n":
            flush()
            out.append(("\n", 0))
        elif ch.isspace():
            flush()
            out.append((" ", 0))
        elif CJK_RE.match(ch):
            flush()
            out.append((ch, 1))
        else:
            buf += ch
    flush()
    return out


def _close_column(columns: list[str], cur: str) -> None:
    # A line's end closes the column; an empty one is the gap between stanzas.
    if cur.strip():
        columns.append(cur)
    elif columns and columns[-1]:
        columns.append("")


def _takes_space(cur: str) -> bool:
    # A space never opens a column, and between kana it would be a gap the Entity
    # did not mean.
    return bool(cur) and not CJK_RE.match(cur[-1])


def wrap_vertical(text: str, f: ImageFont.FreeTypeFont, per_column: int) -> list[str]:
    # Columns filled one by one, top to bottom, in the order they are read: the
    # first stands rightmost. As across, so down: closing punctuation hangs below
    # the column's foot, and is never left to open one.
    columns: list[str] = []
    cur = ""
    used = 0
    for tok, cells in vertical_tokens(text, f):
        hang = 1 if tok in NO_LINE_START else 0
        if tok == "\n":
            _close_column(columns, cur)
            cur, used = "", 0
        elif tok == " ":
            if _takes_space(cur):
                cur += " "
                used += 1
        elif not cur and columns and hang:
            columns[-1] += tok
        elif cur and used + cells > per_column + hang:
            columns.append(cur)
            cur, used = tok, cells
        else:
            cur += tok
            used += cells
    if cur.strip():
        columns.append(cur)
    return columns


def layout_vertical(
    text: str, setting: Setting = NORMAL
) -> tuple[list[str], ImageFont.FreeTypeFont]:
    # As layout(), but columns across the panel rather than lines down it.
    avail_w = EPD_WIDTH - 2 * MARGIN_X
    avail_h = EPD_HEIGHT - 2 * MARGIN_Y

    for unbroken in unbroken_passes(text):
        for size in sizes(unbroken, setting.sizes):
            f = font(size)
            per_column = max(1, avail_h // size)
            columns = wrap_vertical(text, f, per_column)
            if extent(columns, int(size * setting.column_spacing)) <= avail_w and (
                not unbroken or len(columns) == hard_lines(text)
            ):
                return columns, f

    size = setting.sizes[-1]
    f = font(size)
    per_column = max(1, avail_h // size)
    max_columns = max(1, avail_w // int(size * setting.column_spacing))
    columns = wrap_vertical(text, f, per_column)
    kept = len("".join(columns[:max_columns]))
    trimmed = trim_to_sentence(text, kept)
    return wrap_vertical(trimmed, f, per_column)[:max_columns], f


def draw_vertical_glyph(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    token: str,
    f: ImageFont.FreeTypeFont,
    center_x: float,
    cell_top: float,
    cell: int,
    cells: int,
    color: tuple[int, int, int],
) -> None:
    # One square of a column: upright, set in its corner, or laid on its side.
    if token in VERTICAL_TOP_RIGHT:
        x0, y0, x1, y1 = f.getbbox(token)
        inset = cell * 0.08
        px = center_x + cell / 2 - inset - x1
        py = cell_top + inset - y0
        draw.text((px, py), token, font=f, fill=color)
        return

    if len(token) == 1 and token in VERTICAL_ROTATE:
        box = cell * 2
        mask = Image.new("L", (box, box), 0)
        ImageDraw.Draw(mask).text((box / 2, box / 2), token, font=f, fill=255, anchor="mm")
        mask = mask.rotate(-90, resample=Image.BICUBIC)
        image.paste(color, (int(center_x - box / 2), int(cell_top + cell / 2 - box / 2)), mask)
        return

    if len(token) > 1 or not CJK_RE.match(token):
        # A latin word on its side, read top to bottom, centred in its squares.
        width = int(f.getlength(token)) + 4
        height = int(f.size * 1.4)
        mask = Image.new("L", (width, height), 0)
        ImageDraw.Draw(mask).text((width / 2, height / 2), token, font=f, fill=255, anchor="mm")
        mask = mask.rotate(-90, expand=True, resample=Image.BICUBIC)
        image.paste(
            color,
            (
                int(center_x - mask.width / 2),
                int(cell_top + (cells * cell - mask.height) / 2),
            ),
            mask,
        )
        return

    draw.text((center_x, cell_top), token, font=f, fill=color, anchor="ma")


def column_cells(column: str, f: ImageFont.FreeTypeFont) -> list[tuple[str, int]]:
    return [(tok, cells) for tok, cells in vertical_tokens(column, f) if cells]


def render_vertical(
    text: str,
    color: tuple[int, int, int],
    background: tuple[int, int, int] = BACKGROUND,
    setting: Setting = NORMAL,
) -> Image.Image:
    image = Image.new("RGB", (EPD_WIDTH, EPD_HEIGHT), background)
    columns, f = layout_vertical(text, setting)
    if not columns:
        return image

    draw = ImageDraw.Draw(image)
    cell = int(f.size)
    col_w = int(f.size * setting.column_spacing)

    # The columns stand centred together, running right to left, all from one top line.
    block_w = extent(columns, col_w)
    right_edge = (EPD_WIDTH + block_w) / 2
    longest = max(sum(c for _, c in column_cells(col, f)) for col in columns)
    top = (EPD_HEIGHT - longest * cell) / 2

    x = right_edge
    for column in columns:
        if not column.strip():
            x -= col_w * PARAGRAPH_GAP
            continue
        center_x = x - col_w / 2
        x -= col_w
        y = top
        for token, cells in column_cells(column, f):
            draw_vertical_glyph(image, draw, token, f, center_x, y, cell, cells, color)
            y += cells * cell
    return image


# ── Drawing ───────────────────────────────────────────────────────────────────


def trim_to_sentence(text: str, keep: int) -> str:
    # The last whole sentence within keep: the panel never bears a severed clause.
    cut = text[:keep]
    for mark in ("。", ". ", "! ", "? ", "！", "？", ".", "!", "?"):
        idx = cut.rfind(mark)
        if idx > keep // 3:
            return cut[: idx + len(mark)].strip()
    return cut.rstrip() + "..."


def too_wide(text: str) -> list[str]:
    # The lines of verse that could not stand whole even at UNBROKEN_MIN_SIZE.
    vertical = is_vertical(text)
    f = font(UNBROKEN_MIN_SIZE, None if vertical else LATIN_FONT_PATH)
    if not vertical:
        width = EPD_WIDTH - 2 * MARGIN_X
        return [line for line in text.splitlines() if f.getlength(line) > width]
    per_column = (EPD_HEIGHT - 2 * MARGIN_Y) // UNBROKEN_MIN_SIZE
    return [
        line for line in text.splitlines()
        if sum(c for _, c in vertical_tokens(line, f))
        > per_column + (1 if line and line[-1] in NO_LINE_START else 0)
    ]


def fits(text: str) -> bool:
    # Whether all of the words stand on one screen, at any size, with nothing cut.
    if is_vertical(text):
        units, _ = layout_vertical(text)
        shown = "".join(units)
    else:
        units, _ = layout(text, LATIN_FONT_PATH)
        shown = " ".join(units)
    return "".join(shown.split()) == "".join(text.split())


def render_text(
    text: str,
    color: tuple[int, int, int] = TEXT_COLOR,
    background: tuple[int, int, int] = BACKGROUND,
    dense: bool = False,
) -> Image.Image:
    # The words alone, centred, and nothing else: no header, no rule, no footer.
    setting = DENSE if dense else NORMAL
    if not text.strip():
        return Image.new("RGB", (EPD_WIDTH, EPD_HEIGHT), background)
    if is_vertical(text):
        return render_vertical(text, color, background, setting)

    image = Image.new("RGB", (EPD_WIDTH, EPD_HEIGHT), background)
    lines, f = layout(text, LATIN_FONT_PATH, setting)
    draw = ImageDraw.Draw(image)
    line_h = int(f.size * setting.line_spacing)
    y = (EPD_HEIGHT - extent(lines, line_h)) // 2

    for line in lines:
        if not line.strip():
            y += line_h * PARAGRAPH_GAP
            continue
        draw.text(((EPD_WIDTH - f.getlength(line)) / 2, y), line, font=f, fill=color)
        y += line_h
    return image


# The repaint itself (~19.5 s) is the panel's own and cannot be hastened. The
# driver's waiting around it can: 200 ms after every busy-wait, and 2 s after
# sleep. Not all of the latter: gpiozero hears the busy pin fall up to ~300 ms
# after sleep, and raises if the pins are already closed. About 2.5 s are spared.
BUSY_SETTLE_MS = 10      # the driver's: 200
SLEEP_SETTLE_MS = 500    # the driver's: 2000; 300 measured clean, 100 not


def _read_busy(epd) -> None:
    # The driver's ReadBusyH, with less waiting.
    from waveshare_epd import epdconfig

    while epdconfig.digital_read(epd.busy_pin) == 0:   # 0: busy, 1: idle
        epdconfig.delay_ms(5)
    epdconfig.delay_ms(BUSY_SETTLE_MS)


class Display:
    """
    The panel, or where there is none (off the Pi, or EPAPER_MOCK=1), screen.png
    and the terminal. Between screens the panel sleeps deeply, as its makers ask:
    it must not rest at the voltage of a repaint.
    """

    def __init__(self, mock: bool | None = None) -> None:
        self.mock = mock if mock is not None else os.getenv("EPAPER_MOCK") == "1"
        self.epd = None
        if not self.mock:
            try:
                from waveshare_epd import epd4in0e

                self.epd = epd4in0e.EPD()
                self.epd.ReadBusyH = types.MethodType(_read_busy, self.epd)
            except Exception as e:
                print(f"  [no panel is here ({e}) — the words go to screen.png]")
                self.mock = True

    def show(
        self,
        text: str,
        color: tuple[int, int, int] = TEXT_COLOR,
        background: tuple[int, int, int] = BACKGROUND,
        dense: bool = False,
    ) -> None:
        # One screen, waited for to the end: nothing is heard while words still appear.
        image = render_text(text, color, background, dense)
        if self.mock:
            self._show_mock(image, text, dense)
            return
        started = time.monotonic()
        self.epd.init()
        if PANEL_ROTATION:
            image = image.rotate(PANEL_ROTATION, expand=True)
        self.epd.display(self.epd.getbuffer(image))
        self._sleep()
        print(f"  [written in {time.monotonic() - started:.1f}s]")

    def _sleep(self) -> None:
        # The driver's sleep, with less waiting.
        from waveshare_epd import epdconfig

        self.epd.send_command(0x07)   # DEEP_SLEEP
        self.epd.send_data(0xA5)
        epdconfig.delay_ms(SLEEP_SETTLE_MS)
        epdconfig.module_exit()

    def close(self) -> None:
        # The pins released; safe even if the panel was never woken.
        if self.mock or self.epd is None:
            return
        try:
            from waveshare_epd import epdconfig

            epdconfig.module_exit(cleanup=True)
        except Exception:
            pass

    def _show_mock(self, image: Image.Image, text: str, dense: bool = False) -> None:
        image.save("screen.png")
        setting = DENSE if dense else NORMAL
        if is_vertical(text):
            columns, f = layout_vertical(text, setting)
            # As it is written: the first column on the right.
            grid = [list(c) for c in columns]
            depth = max((len(c) for c in grid), default=0)
            print(f"  [screen.png — vertical, {int(f.size)}px, {len(columns)} columns]")
            for row in range(depth):
                line = "".join(
                    (col[row] if row < len(col) else "　") for col in reversed(grid)
                )
                print(f"  {line}")
            return
        lines, f = layout(text, LATIN_FONT_PATH, setting)
        width = max((len(l) for l in lines), default=0)
        print(f"  [screen.png — {int(f.size)}px, {len(lines)} lines]")
        print("  +" + "-" * (width + 2) + "+")
        for line in lines:
            print(f"  | {line.ljust(width)} |")
        print("  +" + "-" * (width + 2) + "+")

    def __enter__(self) -> "Display":
        return self

    def __exit__(self, *_) -> None:
        self.close()
