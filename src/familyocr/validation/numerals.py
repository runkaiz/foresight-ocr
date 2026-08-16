"""Chinese numeral parsing for 雁序圖 entry IDs.

Entry IDs are written as a generation label plus a numeral — 庶五十九, 富六百一十,
教千百九十三 in one volume, 清千八百四十九 in another. The numerals use the
ordinary multiplicative system,
with two wrinkles common in printed genealogies:

- a leading unit with an implied 一 (千百九十三 = 1193, not 100+93)
- the contracted forms 廿 (20), 卅 (30), 卌 (40)

Parsing is deliberately strict: anything it cannot read returns `None` rather
than a best guess. An ID silently coerced to a plausible number would defeat the
sequence check that this whole validation stage rests on.
"""

from __future__ import annotations

import re
from functools import lru_cache
from dataclasses import dataclass

DIGITS: dict[str, int] = {
    "〇": 0, "零": 0,
    "一": 1, "壹": 1,
    "二": 2, "貳": 2, "贰": 2, "两": 2, "兩": 2,
    "三": 3, "參": 3, "叁": 3,
    "四": 4, "肆": 4,
    "五": 5, "伍": 5,
    "六": 6, "陸": 6, "陆": 6,
    "七": 7, "柒": 7,
    "八": 8, "捌": 8,
    "九": 9, "玖": 9,
}

UNITS: dict[str, int] = {
    "十": 10, "拾": 10,
    "百": 100, "佰": 100,
    "千": 1000, "仟": 1000,
}

CONTRACTED: dict[str, int] = {"廿": 20, "卅": 30, "卌": 40}

# Which characters can open an id is document data, not a constant: 丙辰庶富教1
# uses 庶/富/教 and 丙辰清廉麗熙2 uses 清/廉/麗/熙. Hard-coding the first corpus
# here made `parse_entry_id` reject every id in the other volumes outright.
#
# The band cannot simply be "any Han character" either — `五十` would then read
# as band 五, number 十. So the set comes from the active document profile, with
# the union of every label seen in the corpus as the fallback for callers that
# have not set one.
_FALLBACK_LABELS = ("允", "庶", "富", "教", "清", "廉", "麗", "熙")


@lru_cache(maxsize=16)
def _id_re(labels: tuple[str, ...]) -> re.Pattern[str]:
    return re.compile(
        r"^\s*(?P<band>[" + "".join(labels) + r"])?\s*(?P<num>[^\s]+?)\s*$"
    )


def _band_chars() -> tuple[str, ...]:
    """Every character that may open an id: the corpus union plus this volume's.

    A union rather than only the active profile's labels, because no generation
    label is also a numeral character — so admitting the others costs no
    ambiguity, and the parser then works whether or not a profile has been set.
    """
    from familyocr.context import generation_chain

    return tuple(dict.fromkeys((*_FALLBACK_LABELS, *generation_chain())))


@dataclass
class ParsedNumeral:
    text: str
    band: str | None
    value: int | None
    ok: bool
    reason: str = ""


def parse_numeral(text: str) -> int | None:
    """Convert a Chinese numeral to an int, or None if it is not well formed."""
    if not text:
        return None
    total = 0
    section = 0
    digit: int | None = None
    seen = False

    for ch in text:
        if ch in DIGITS:
            digit = DIGITS[ch]
            seen = True
        elif ch in CONTRACTED:
            if digit is not None:
                return None       # 三廿 is not a number
            section += CONTRACTED[ch]
            seen = True
        elif ch in UNITS:
            unit = UNITS[ch]
            # A unit with no digit in front carries an implied 一, which is how
            # 千百九十三 reaches 1193.
            section += (digit if digit is not None else 1) * unit
            digit = None
            seen = True
        else:
            return None
    if not seen:
        return None
    total = section + (digit or 0)
    return total


def parse_entry_id(text: str) -> ParsedNumeral:
    """Parse a full entry ID such as `庶五百八十七`."""
    raw = (text or "").strip()
    m = _id_re(_band_chars()).match(raw)
    if not m:
        return ParsedNumeral(raw, None, None, False, "unrecognised id format")
    band = m.group("band")
    value = parse_numeral(m.group("num"))
    if value is None:
        return ParsedNumeral(raw, band, None, False, "numeral not parseable")
    return ParsedNumeral(raw, band, value, True)


_UNIT_ORDER = ((1000, "千"), (100, "百"), (10, "十"))
_DIGIT_CHARS = "〇一二三四五六七八九"


def format_numeral(value: int) -> str:
    """Render an int the way this book writes it. Used in validation messages."""
    if value < 0:
        raise ValueError("negative")
    if value == 0:
        return "〇"
    out: list[str] = []
    rest = value
    for unit_value, unit_char in _UNIT_ORDER:
        count, rest = divmod(rest, unit_value)
        if count:
            if count > 1:
                out.append(_DIGIT_CHARS[count])
            out.append(unit_char)
    if rest:
        out.append(_DIGIT_CHARS[rest])
    return "".join(out)
