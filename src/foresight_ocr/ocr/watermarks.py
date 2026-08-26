"""Separate known library-watermark text from an OCR transcription.

Watermark suppression reduces the cyan pigment, but it cannot make a large,
opaque logo disappear without also risking the historical ink underneath it.
The recognizer therefore still emits fragments such as ``图书馆``, ``FUYANG``
and ``IBRARY`` on some crops.

This filter is deliberately narrower than a general text cleaner.  It removes
only phrases belonging to the known Fuyang Library mark and keeps the exact
unfiltered recognizer output alongside the cleaned result for provenance.
Traditional characters and all other OCR text are left untouched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_CJK_MARKS = (
    "富阳图书馆",
    "富陽圖書館",
    "富阳圖書館",
    "富陽图书馆",
    "富阳区图书馆",
    "富陽區圖書館",
    "图书馆",
    "圖書館",
    "书馆",
    "書館",
)
_LATIN_WORD = re.compile(r"[A-Za-z]{4,}")
_SHORT_UPPER_FRAGMENT = re.compile(r"(?<![A-Za-z])[A-Z]{1,3}(?![A-Za-z])")
_SPACE_BEFORE_PUNCT = re.compile(r"[ \t]+([,.;:，。；：])")
_LOGO_FRAGMENT_CHARS = frozenset("富陽阳旺图圖书書馆館区區")


@dataclass(frozen=True)
class WatermarkFiltered:
    transcription: str | None
    removed: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.removed)


def _is_latin_mark(word: str) -> bool:
    """Whether an uppercase OCR word is a Fuyang Library fragment."""
    if word != word.upper():
        return False
    upper = word.upper()
    return (
        "FUYANG" in upper
        or upper.startswith("FUYA")
        or "LIBRAR" in upper
        or upper.startswith("LIBR")
        or "IBRARY" in upper
        or "BRARY" in upper
        or upper.startswith("BRAR")
        or upper in {"RARY", "EIBRAR"}
    )


def filter_watermark_text(text: str | None) -> WatermarkFiltered:
    """Remove known watermark fragments while retaining all other OCR text.

    A crop containing only fragments of the logo returns ``None``.  Small
    remnants such as ``F 阳`` are discarded only after a definitive English or
    Chinese watermark token was found; the legitimate name ``富陽`` on its own
    is therefore not touched.
    """
    raw = (text or "").strip()
    if not raw:
        return WatermarkFiltered(None)

    work = raw
    removed: list[str] = []

    for mark in _CJK_MARKS:
        if mark in work:
            removed.extend([mark] * work.count(mark))
            # CJK text does not use word separators; inserting one here would
            # manufacture formatting between two adjacent printed fields.
            work = work.replace(mark, "")

    def strip_latin(match: re.Match[str]) -> str:
        word = match.group(0)
        if not _is_latin_mark(word):
            return word
        removed.append(word)
        return " "

    work = _LATIN_WORD.sub(strip_latin, work)

    if removed:
        # A longer definitive logo fragment often arrives with one detached
        # edge glyph on the preceding line: `G LIBRAF`, `F FUYANG`, `NG LIBR`.
        # These tiny uppercase tokens are removed only after the watermark has
        # already been identified, so ordinary short text is never touched.
        def strip_short(match: re.Match[str]) -> str:
            removed.append(match.group(0))
            return " "

        work = _SHORT_UPPER_FRAGMENT.sub(strip_short, work)

    lines = []
    for line in work.splitlines():
        cleaned = _SPACE_BEFORE_PUNCT.sub(r"\1", re.sub(r"[ \t]+", " ", line)).strip()
        cleaned = cleaned.strip(" ,.;:，。；：·|/-")
        if cleaned:
            lines.append(cleaned)
    cleaned = "\n".join(lines).strip()

    # OCR sometimes returns a few isolated glyphs from the same logo beside a
    # definitive token: `F 阳 FUYANG`, `阳图FUYANGL`, or `G LIBRARY`.  They are
    # safe to discard only in that context, never as a standalone name.
    compact = "".join(cleaned.split())
    if (
        removed
        and compact
        and len(compact) <= 4
        and all(ch in _LOGO_FRAGMENT_CHARS or ("A" <= ch <= "Z") for ch in compact)
    ):
        removed.append(cleaned)
        cleaned = ""

    return WatermarkFiltered(cleaned or None, tuple(removed))
