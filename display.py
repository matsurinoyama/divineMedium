from __future__ import annotations

import os
import re
import sys
import time
import types

from PIL import Image, ImageDraw, ImageFont

# The waveshare driver lives in lib/ next to this file, as in the vendor examples.
LIBDIR = os.path.join(os.path.dirname(os.path.realpath(__file__)), "lib")
if os.path.exists(LIBDIR):
    sys.path.append(LIBDIR)

# ── Panel ─────────────────────────────────────────────────────────────────────
# The 4inch (E) is 400x600 portrait and has no partial refresh — every update is a
# full ~15-20 second repaint, so the ritual shows one screen per utterance and
# never paints progress or status.
EPD_WIDTH = 400
EPD_HEIGHT = 600

# The six colours the panel can actually produce. These must be the exact RGB
# values in the driver's palette: getbuffer() quantises with Floyd-Steinberg
# dithering, and only exact matches come through as flat colour instead of noise.
BLACK = (0, 0, 0)
WHITE = (255, 255, 255)
YELLOW = (255, 255, 0)
RED = (255, 0, 0)
BLUE = (0, 0, 255)
GREEN = (0, 255, 0)

TEXT_COLOR = BLACK
BACKGROUND = WHITE

# The panel has no gold; yellow is the nearest colour it can lay down flat.
GOLD = YELLOW

MARGIN_X = 30
MARGIN_Y = 40
LINE_SPACING = 1.38     # multiple of font size, between horizontal lines
COLUMN_SPACING = 1.5    # multiple of font size, between vertical columns
# A blank line in the text ("\n\n") is a paragraph break: this fraction of a line
# (or of a column, set vertically) of extra space instead of a whole empty one.
PARAGRAPH_GAP = 0.5

# Largest first — layout() walks down this ladder and keeps the biggest size whose
# text still fits on one screen. The floor is deliberately high: ShinGyoRei is a
# brush face with thin, broken strokes, and below ~28px those strokes start
# dropping out of a 1-bit-per-colour panel altogether.
FONT_SIZES = [46, 40, 35, 31, 28]

# A semi-cursive brush face — the hand of something writing on the panel rather
# than a machine printing to it.
FONT_PATH = os.getenv(
    "FONT_PATH",
    os.path.join(
        os.path.dirname(os.path.realpath(__file__)), "fonts", "FOT-ShinGyoReiStdN-R.otf"
    ),
)

# Budgets handed to the entity so its answer fits a single screen without dropping
# to the smallest font. These are measured against the font and geometry above by
# `python debug_display.py --budget`; re-run it after changing either.
#
# Horizontally the floor is about 320 characters at 28px. 34 words holds even when
# every one of them is long (~9 characters, counting the space); ordinary prose at
# that length lands at 35px with room to spare. Vertically the floor is about 140
# characters, so 95 lands at 31px.
BUDGET_WORDS = 34       # latin words
BUDGET_CJK_CHARS = 95   # japanese characters

# ── Orientation ───────────────────────────────────────────────────────────────
# Japanese is set vertically (縦書き): columns run top to bottom, and the columns
# themselves run right to left, which is how a prayer or an oracle would be
# written. Latin stays horizontal. VERTICAL=always/never overrides the detection.
VERTICAL_MODE = os.getenv("VERTICAL", "auto").lower()

# Kana and kanji — what actually decides that a line is Japanese. Deliberately
# narrower than CJK_RE, which also matches shared punctuation.
JAPANESE_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿]")
# CJK ideographs, kana and fullwidth forms: these wrap per character, not per word.
CJK_RE = re.compile(
    r"[　-〿぀-ヿ㐀-䶿一-鿿豈-﫿＀-￯]"
)

# Characters that must not begin a line or a column (kinsoku shori, abbreviated).
NO_LINE_START = "、。，．,.!?!?)）]｝」』》〉…・ー:;:;"

# Set sideways when the text runs vertically: the long-vowel mark, dashes, ellipses
# and every kind of bracket. Rotated a quarter turn clockwise, as in print.
VERTICAL_ROTATE = set("ー〜～−—–‐-—…‥（）()｛｝{}「」『』【】〔〕［］[]〈〉《》＝=：；:;")
# Set in the upper right of their square instead of on the baseline.
VERTICAL_TOP_RIGHT = set("、。，．,.")

# ShinGyoRei carries latin too, but draws it in its own idiosyncratic hand — its
# "e" is closer to a Greek epsilon. That reads as part of the voice at display
# sizes; point LATIN_FONT_PATH at another face if it should not.
LATIN_FONT_PATH = os.getenv("LATIN_FONT_PATH", FONT_PATH)

_font_cache: dict[tuple[int, str], ImageFont.FreeTypeFont] = {}


def font(size: int, path: str | None = None) -> ImageFont.FreeTypeFont:
    # One face for both scripts: ShinGyoRei StdN carries kana, kanji and latin.
    key = (size, path or FONT_PATH)
    if key not in _font_cache:
        try:
            _font_cache[key] = ImageFont.truetype(key[1], size)
        except OSError as e:
            raise RuntimeError(
                f"Cannot load the display font at {key[1]} ({e}). Put "
                "FOT-ShinGyoReiStdN-R.otf in fonts/, or point FONT_PATH at another "
                "face that covers both kana/kanji and latin."
            ) from e
    return _font_cache[key]


def is_vertical(text: str) -> bool:
    # Vertical when the utterance is substantially Japanese.
    if VERTICAL_MODE == "always":
        return True
    if VERTICAL_MODE == "never":
        return False
    stripped = "".join(text.split())
    if not stripped:
        return False
    japanese = sum(1 for ch in stripped if JAPANESE_RE.match(ch))
    return japanese >= max(4, len(stripped) * 0.3)


# ── Horizontal layout ─────────────────────────────────────────────────────────


def tokens(text: str) -> list[str]:
    """
    Split text into the smallest units a line may break between.

    Latin runs stay whole so words are never split; each CJK character is its own
    token because Japanese has no spaces to break on. " " is a soft break and
    "\n" a hard one; two in a row leave an empty line, the paragraph break.
    """
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


def wrap(text: str, f: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    # Greedy wrap over tokens(). Returns lines already stripped of trailing spaces.
    lines: list[str] = []
    cur = ""
    for tok in tokens(text):
        if tok == "\n":
            lines.append(cur.rstrip())
            cur = ""
            continue
        if tok == " ":
            if cur:
                cur += " "
            continue

        # Japanese never opens a line with closing punctuation. Such a token is
        # allowed to hang past the right margin instead (burasage) — the text is
        # centred, so the overhang is split between the two margins.
        hang = min(f.size, EPD_WIDTH - 8 - max_width) if tok in NO_LINE_START else 0

        # ...and if it has already been pushed onto a line of its own, pull it
        # back up onto the previous one.
        if not cur and lines and tok in NO_LINE_START:
            if f.getlength(lines[-1] + tok) <= max_width + hang:
                lines[-1] += tok
                continue

        candidate = cur + tok
        if f.getlength(candidate.rstrip()) <= max_width + hang or not cur.strip():
            cur = candidate
        else:
            lines.append(cur.rstrip())
            cur = tok
    if cur.strip():
        lines.append(cur.rstrip())
    return lines


def extent(units: list[str], pitch: int) -> int:
    # Total depth of laid-out lines (or columns): a full pitch for each, and a
    # paragraph gap for each empty one.
    gaps = sum(1 for u in units if not u.strip())
    return int((len(units) - gaps) * pitch + gaps * pitch * PARAGRAPH_GAP)


def hard_lines(text: str) -> int:
    # Lines (or columns) the text asks for itself, blank paragraph breaks included.
    return text.count("\n") + 1


# Verse is kept unwrapped only down to this size; below it, a larger size with a
# wrapped line reads better than small unbroken lines.
UNBROKEN_MIN_SIZE = 35


def unbroken_passes(text: str) -> tuple[bool, ...]:
    # Verse is first fitted without wrapping any of its lines; prose never is.
    return (True, False) if hard_lines(text) > 1 else (False,)


def sizes(unbroken: bool) -> list[int]:
    return [s for s in FONT_SIZES if s >= UNBROKEN_MIN_SIZE] if unbroken else FONT_SIZES


def layout(text: str, path: str | None = None) -> tuple[list[str], ImageFont.FreeTypeFont]:
    """
    Pick the largest font size whose wrapped text fits one screen.

    Text with line breaks of its own (verse) is first tried at sizes where none
    of its lines has to wrap, so a line of a poem is never split in two.

    Returns (lines, font). If even the smallest size overflows, the text is
    trimmed at a sentence boundary and re-laid out at that size.
    """
    avail_w = EPD_WIDTH - 2 * MARGIN_X
    avail_h = EPD_HEIGHT - 2 * MARGIN_Y

    for unbroken in unbroken_passes(text):
        for size in sizes(unbroken):
            f = font(size, path)
            lines = wrap(text, f, avail_w)
            if extent(lines, int(size * LINE_SPACING)) <= avail_h and (
                not unbroken or len(lines) == hard_lines(text)
            ):
                return lines, f

    # Overflow: estimate how much text fits at the smallest size and trim to it.
    size = FONT_SIZES[-1]
    f = font(size, path)
    max_lines = max(1, avail_h // int(size * LINE_SPACING))
    lines = wrap(text, f, avail_w)
    kept = len(" ".join(lines[:max_lines]))
    trimmed = trim_to_sentence(text, kept)
    return wrap(trimmed, f, avail_w)[:max_lines], f


# ── Vertical layout (縦書き) ──────────────────────────────────────────────────


def vertical_tokens(text: str, f: ImageFont.FreeTypeFont) -> list[tuple[str, int]]:
    """
    Split text into (token, cells) pairs for vertical setting.

    A CJK character occupies one square cell. A run of latin is set sideways as a
    single unit and occupies as many cells as its rotated length needs, which
    keeps words intact rather than stacking them letter by letter.
    """
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


def wrap_vertical(text: str, f: ImageFont.FreeTypeFont, per_column: int) -> list[str]:
    # Greedy fill, one column at a time, top to bottom. Columns are returned in
    # reading order: the first is the rightmost on the panel.
    columns: list[str] = []
    cur = ""
    used = 0
    for tok, cells in vertical_tokens(text, f):
        if tok == "\n":
            if cur.strip():
                columns.append(cur)
            elif columns and columns[-1]:
                columns.append("")   # a blank line: the paragraph break
            cur, used = "", 0
            continue
        if tok == " ":
            # A space is only a break opportunity; it never opens a column, and
            # between kana it would read as a gap the entity did not intend.
            if cur and not CJK_RE.match(cur[-1]):
                cur += " "
                used += 1
            continue

        # The same kinsoku rule as horizontally: a column cannot begin with
        # closing punctuation, so it hangs below the bottom margin instead.
        hang = 1 if tok in NO_LINE_START else 0
        if not cur and columns and tok in NO_LINE_START:
            columns[-1] += tok
            continue
        if used + cells <= per_column + hang or not cur:
            cur += tok
            used += cells
        else:
            columns.append(cur)
            cur, used = tok, cells
    if cur.strip():
        columns.append(cur)
    return columns


def layout_vertical(text: str) -> tuple[list[str], ImageFont.FreeTypeFont]:
    # As layout(), but fitting columns across the panel instead of lines down it.
    avail_w = EPD_WIDTH - 2 * MARGIN_X
    avail_h = EPD_HEIGHT - 2 * MARGIN_Y

    for unbroken in unbroken_passes(text):
        for size in sizes(unbroken):
            f = font(size)
            per_column = max(1, avail_h // size)
            columns = wrap_vertical(text, f, per_column)
            if extent(columns, int(size * COLUMN_SPACING)) <= avail_w and (
                not unbroken or len(columns) == hard_lines(text)
            ):
                return columns, f

    size = FONT_SIZES[-1]
    f = font(size)
    per_column = max(1, avail_h // size)
    max_columns = max(1, avail_w // int(size * COLUMN_SPACING))
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
    # One cell of a vertical column: upright, nudged into the corner, or sideways.
    if token in VERTICAL_TOP_RIGHT:
        # Vertical commas and full stops sit in the upper right of their square.
        x0, y0, x1, y1 = f.getbbox(token)
        inset = cell * 0.08
        px = center_x + cell / 2 - inset - x1
        py = cell_top + inset - y0
        draw.text((px, py), token, font=f, fill=color)
        return

    if len(token) == 1 and token in VERTICAL_ROTATE:
        # Brackets, dashes and the long-vowel mark turn a quarter turn clockwise.
        box = cell * 2
        mask = Image.new("L", (box, box), 0)
        ImageDraw.Draw(mask).text((box / 2, box / 2), token, font=f, fill=255, anchor="mm")
        mask = mask.rotate(-90, resample=Image.BICUBIC)
        image.paste(color, (int(center_x - box / 2), int(cell_top + cell / 2 - box / 2)), mask)
        return

    if len(token) > 1 or not CJK_RE.match(token):
        # A latin run, set sideways and reading top to bottom, centred in the
        # cells reserved for it.
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
    # Re-split a laid-out column into the cells it will be drawn in.
    return [(tok, cells) for tok, cells in vertical_tokens(column, f) if cells]


def render_vertical(
    text: str, color: tuple[int, int, int], background: tuple[int, int, int] = BACKGROUND
) -> Image.Image:
    image = Image.new("RGB", (EPD_WIDTH, EPD_HEIGHT), background)
    columns, f = layout_vertical(text)
    if not columns:
        return image

    draw = ImageDraw.Draw(image)
    cell = int(f.size)
    col_w = int(f.size * COLUMN_SPACING)

    # Columns are a block centred horizontally; within it they run right to left.
    block_w = extent(columns, col_w)
    right_edge = (EPD_WIDTH + block_w) / 2
    # All columns start on the same top line, and the longest one centres the block.
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


# ── Rendering ─────────────────────────────────────────────────────────────────


def trim_to_sentence(text: str, keep: int) -> str:
    # Last resort when text overflows even the smallest font: cut back to the last
    # sentence end that fits, so the panel never shows a severed clause.
    cut = text[:keep]
    for mark in ("。", ". ", "! ", "? ", "！", "？", ".", "!", "?"):
        idx = cut.rfind(mark)
        if idx > keep // 3:
            return cut[: idx + len(mark)].strip()
    return cut.rstrip() + "..."


def too_wide(text: str) -> list[str]:
    # The lines of text that would have to wrap even at UNBROKEN_MIN_SIZE: the
    # ones that keep verse from being set whole.
    vertical = is_vertical(text)
    f = font(UNBROKEN_MIN_SIZE, None if vertical else LATIN_FONT_PATH)
    if not vertical:
        width = EPD_WIDTH - 2 * MARGIN_X
        return [line for line in text.splitlines() if f.getlength(line) > width]
    per_column = (EPD_HEIGHT - 2 * MARGIN_Y) // UNBROKEN_MIN_SIZE
    return [
        line for line in text.splitlines()
        # closing punctuation may hang below the column, as in wrap_vertical
        if sum(c for _, c in vertical_tokens(line, f))
        > per_column + (1 if line and line[-1] in NO_LINE_START else 0)
    ]


def fits(text: str) -> bool:
    # True when the whole text fits one screen, at whatever size, with nothing trimmed.
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
) -> Image.Image:
    # Bare text, centred on a blank panel: Japanese vertical, latin horizontal.
    # No header, rule or footer. Both colours should be from the panel's six, or
    # they come out dithered.
    if not text.strip():
        return Image.new("RGB", (EPD_WIDTH, EPD_HEIGHT), background)
    if is_vertical(text):
        return render_vertical(text, color, background)

    image = Image.new("RGB", (EPD_WIDTH, EPD_HEIGHT), background)
    lines, f = layout(text, LATIN_FONT_PATH)
    draw = ImageDraw.Draw(image)
    line_h = int(f.size * LINE_SPACING)
    y = (EPD_HEIGHT - extent(lines, line_h)) // 2

    for line in lines:
        if not line.strip():
            y += line_h * PARAGRAPH_GAP
            continue
        draw.text(((EPD_WIDTH - f.getlength(line)) / 2, y), line, font=f, fill=color)
        y += line_h
    return image


# The refresh itself (~19.5 s) is the panel's own waveform, stored in its
# controller; nothing here can shorten it without leaving ghosting behind. What
# can go is the vendor driver's padding around it: a 200 ms pause after every
# busy-wait (the busy pin has already said the panel is idle) and a 2 s pause
# after the deep-sleep command. That one cannot go entirely: the busy pin drops
# on the sleep command, and gpiozero reports the edge up to ~300 ms later — if
# the pins are closed first, its callback thread raises at shutdown. Together
# about 2.5 s of a ~23 s update.
BUSY_SETTLE_MS = 10      # vendor: 200
SLEEP_SETTLE_MS = 500    # vendor: 2000; 300 measured clean, 100 not


def _read_busy(epd) -> None:
    # epd4in0e.EPD.ReadBusyH with the settle pause shortened.
    from waveshare_epd import epdconfig

    while epdconfig.digital_read(epd.busy_pin) == 0:   # 0: busy, 1: idle
        epdconfig.delay_ms(5)
    epdconfig.delay_ms(BUSY_SETTLE_MS)


class Display:
    """
    The e-paper panel, or a PNG stand-in when no panel is present.

    Each show() runs init -> display -> sleep. The panel is deep-slept between
    updates because the vendor documentation warns against leaving it powered at
    refresh voltage, and a session can idle for minutes between utterances.

    Set EPAPER_MOCK=1 (or simply run off a Pi, where the driver import fails) to
    write screen.png instead and print the text to the terminal.
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
                print(f"  [display: no panel ({e}) — falling back to screen.png]")
                self.mock = True

    def show(
        self,
        text: str,
        color: tuple[int, int, int] = TEXT_COLOR,
        background: tuple[int, int, int] = BACKGROUND,
    ) -> None:
        # Render and push one screen. Blocks for the full refresh, by design:
        # nothing should be recorded while the words are still appearing.
        image = render_text(text, color, background)
        if self.mock:
            self._show_mock(image, text)
            return
        started = time.monotonic()
        self.epd.init()
        self.epd.display(self.epd.getbuffer(image))
        self._sleep()
        print(f"  [displayed in {time.monotonic() - started:.1f}s]")

    def clear(self) -> None:
        # Blank the panel and sleep it. Called at the end of every session.
        if self.mock:
            print("  [display cleared]")
            render_text("").save("screen.png")
            return
        self.epd.init()
        self.epd.Clear()
        self._sleep()

    def _sleep(self) -> None:
        # epd4in0e.EPD.sleep with the settle pause shortened.
        from waveshare_epd import epdconfig

        self.epd.send_command(0x07)   # DEEP_SLEEP
        self.epd.send_data(0xA5)
        epdconfig.delay_ms(SLEEP_SETTLE_MS)
        epdconfig.module_exit()

    def close(self) -> None:
        # Release SPI/GPIO. Safe to call when nothing was ever initialised.
        if self.mock or self.epd is None:
            return
        try:
            from waveshare_epd import epdconfig

            epdconfig.module_exit(cleanup=True)
        except Exception:
            pass

    def _show_mock(self, image: Image.Image, text: str) -> None:
        image.save("screen.png")
        if is_vertical(text):
            columns, f = layout_vertical(text)
            # Printed as it is set: the first column on the right.
            grid = [list(c) for c in columns]
            depth = max((len(c) for c in grid), default=0)
            print(f"  [screen.png — vertical, {int(f.size)}px, {len(columns)} columns]")
            for row in range(depth):
                line = "".join(
                    (col[row] if row < len(col) else "　") for col in reversed(grid)
                )
                print(f"  {line}")
            return
        lines, f = layout(text, LATIN_FONT_PATH)
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
