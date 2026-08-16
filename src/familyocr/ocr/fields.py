"""Splitting an entry transcription into its printed fields.

A 雁序圖 entry carries three things, and the OCR smoke test made that visible:

    庶三百三十五   允二百八十六   次子
    own id         father's id     birth order

The father's id names a person in the *previous* generation band, so the parent
links are printed on the page rather than inferred from geometry. That is why
this splitter belongs here and not in a later semantic stage: exact-field
accuracy on the two ids is the metric that actually matters for the family tree,
and it can only be measured once the fields are separated.

Two conventions confirmed with the project owner:

- Where the father's generation id is unknown, the page prints his **name**
  instead. So the parent slot holds either an id or a name, and both are links
  to the previous generation.
- A bare `子` marks an **only son** and ranks as 長子. It is kept verbatim and
  ranked through `order_rank`, never rewritten.

The splitter is deliberately shallow. It labels what it recognizes and hands
back everything else in `leftover` rather than forcing a parse.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field as dc_field
from typing import Any

from familyocr.validation.numerals import CONTRACTED, DIGITS, UNITS

# Generation chain, oldest first. The three bands printed on a page are 庶 / 富 /
# 教 (generations 31–33); 允 is generation 30 and survives only as a parent
# reference from the preceding volume.
#
# Which label is "own" and which is "parent" depends on the band the crop came
# from: 庶 is the own id of a top-band entry and the *parent* id of a middle-band
# one. Treating the three as interchangeable own-labels and taking whichever
# appeared first — as an earlier version of this file did — silently swapped the
# two fields on the majority of 富 and 教 entries.
# Defaults for the first corpus; the active document profile overrides them.
GENERATION_CHAIN = ("允", "庶", "富", "教")
BAND_LABELS = ("庶", "富", "教")
# The regexes are built from a superset so one compiled pattern serves every
# volume: 允庶富教 for the 庶富教 books, 清廉麗熙 for the others. Which of these
# is an *own* label on a given page still comes from the profile.
ALL_LABELS = ("允", "庶", "富", "教", "清", "廉", "麗", "熙")

# Orthographic variants of the band labels that recognizers legitimately emit.
# 敎 (U+654E) is the same character as 教 (U+6559) in a different printed form,
# and the block prints it that way; without this the entire 教 band fails to
# parse. This is a deliberately narrow allowance: it applies only to the band
# label, and `ParsedEntry.label_variant` records that a non-canonical form was
# seen, so the substitution is auditable rather than silent. Glyphs that merely
# *resemble* a label — 族 / 康 / 鹿 for 庶 — are not listed here. Those are
# recognition errors and must keep failing loudly.
LABEL_VARIANTS: dict[str, str] = {
    "敎": "教",
    "庻": "庶",
}

# Glyphs that are not numerals at all but are recognized in place of one. 干
# (U+5E72) differs from 千 by a single stroke and accounts for 231 of the 337
# entries the full-book run could not parse — the whole 1000s range of the
# volume. Unlike LABEL_VARIANTS these are recognition *errors*, not alternate
# printed forms, so a repair is recorded per entry in
# `ParsedEntry.numeral_repairs` and the raw transcription is left untouched.
#
# The bar for adding an entry here: the character must never occur legitimately
# in a numeral, so substituting it cannot mask a real reading. 大 for 六 and 庚
# for 庶 are NOT listed — 庚 is handled by the band geometry, and 大 sits close
# enough to plausible text that a blanket rewrite would be guessing.
NUMERAL_CONFUSABLES: dict[str, str] = {
    "干": "千",
}

def canonical_label(ch: str) -> str:
    return LABEL_VARIANTS.get(ch, ch)


def parent_label(own_label: str) -> str | None:
    """The label one generation above `own_label`, per the active profile."""
    from familyocr.context import generation_chain

    chain = generation_chain() or list(GENERATION_CHAIN)
    if own_label not in chain:
        return None
    i = chain.index(own_label)
    return chain[i - 1] if i > 0 else None

NUMERAL_CHARS = "".join(sorted(set(DIGITS) | set(UNITS) | set(CONTRACTED)))

_LABEL_CHARS = "".join(ALL_LABELS) + "".join(LABEL_VARIANTS)
_ID_RE = re.compile(f"([{_LABEL_CHARS}])([{NUMERAL_CHARS}]+)")
# 長子 / 次子 / 三子 / 幼子 / 長女 …
_ORDER_RE = re.compile(f"([長次幼元{NUMERAL_CHARS}])([子女])")


@dataclass
class ParsedEntry:
    own_id: str | None
    parent_id: str | None
    order: str | None
    leftover: str
    text: str
    # Non-canonical band-label forms seen in this entry, e.g. {'敎': '教'}.
    label_variant: dict[str, str] = dc_field(default_factory=dict)
    # The father named rather than numbered — see `parse_entry`.
    parent_name: str | None = None
    # True when the band label was supplied by page geometry because the
    # recognized glyph was not a usable label (庶 read as 庚, 允 as 尤, …).
    label_from_geometry: bool = False
    # The glyph that was actually printed in the label position, kept so the
    # substitution can be audited.
    observed_label: str | None = None
    # Non-numeral glyphs repaired inside a numeral, e.g. {'干': '千'}.
    numeral_repairs: dict[str, str] = dc_field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def field(self, name: str) -> str | None:
        return {"own_id": self.own_id, "parent_id": self.parent_id,
                "order": self.order, "parent_name": self.parent_name}.get(name)

    @property
    def order_rank(self) -> int | None:
        """Birth order as a number, or None if it cannot be read.

        A bare 子 means an only son and ranks first. The rank is derived here
        rather than by rewriting `order`, so the transcription on record stays
        exactly what the page says.
        """
        if not self.order:
            return None
        head = self.order[0]
        if head in ("子", "女"):
            return 1
        if head in ("長", "元"):
            return 1
        if head == "次":
            return 2
        if head == "幼":
            return -1          # last child; position unknown without siblings
        value = DIGITS.get(head)
        return value if value else None


_LEADING_NUMERAL = re.compile(
    f"^(?P<pre>[^{NUMERAL_CHARS}\\s])?\\s*(?P<num>[{NUMERAL_CHARS}]{{2,}})"
)


def parse_entry(
    text: str | None,
    own_label: str | None = None,
    trust_band: bool = False,
) -> ParsedEntry:
    """Label the fields of one entry, retrying without the recognizer's breaks.

    Line breaks are the recognizer's doing, not the page's. 丙辰庶富教1 comes back
    one field per line, but 丙辰庶富教2 frequently breaks inside an id — `庶` on
    one line and `千二百` on the next — leaving an id that cannot be assembled at
    all; half that volume failed to parse for this reason alone.

    Joining the fragments is not safe in general, which is why it is a fallback
    rather than a normalization: `教二十 富十 三子` joined reads as father 富十三
    and order 子, getting *both* fields wrong where the spaced form had them
    right. So the text is parsed as it came first, and the joined form is used
    only when that yields no id — an entry that already parses is never touched.
    """
    raw = (text or "").strip()
    joined = "".join(raw.split())
    # Escalating, safest first. Reading the label off the page is always better
    # than borrowing it from geometry, so both spellings are tried on their own
    # before the band is trusted at all — otherwise `庶 / 干 / 二百` is rescued
    # from its own last line as 庶二百, and a wrong id that looks right is worse
    # than none.
    attempts = [(raw, False), (joined, False)]
    if trust_band:
        attempts += [(raw, True), (joined, True)]

    fallback: ParsedEntry | None = None
    for candidate, trust in attempts:
        if candidate != raw and candidate == joined and joined == raw:
            continue
        parsed = _parse_fields(candidate, own_label, trust)
        if parsed.own_id is not None:
            parsed.text = raw   # the transcription on record is what was read
            return parsed
        if fallback is None:
            fallback = parsed
    fallback.text = raw
    return fallback


def _parse_fields(
    text: str | None,
    own_label: str | None = None,
    trust_band: bool = False,
) -> ParsedEntry:
    """Label the fields of one entry transcription.

    `own_label` is the band the crop came from. Supply it whenever it is known —
    without it the split between own id and parent id is a guess, because the
    same character is an own label in one band and a parent label in the next.

    `trust_band` allows the id's numeral to be read even when the label glyph
    came back wrong (庶 as 庚, 允 as 尤 — stroke-dense characters that blur before
    the numerals do). This is not the forbidden "repair from linguistic
    plausibility": which band a crop belongs to was established by rule and frame
    detection, entirely independently of the recognizer, so requiring OCR to
    re-derive it discards a correct numeral for no gain. The substitution is
    always recorded in `label_from_geometry` / `observed_label`, and the raw
    transcription is left untouched.
    """
    raw = (text or "").strip()
    if not raw:
        return ParsedEntry(None, None, None, "", raw)

    # Matching runs against a repaired copy; `raw` is what gets reported.
    numeral_repairs: dict[str, str] = {}
    work = raw
    for wrong, right in NUMERAL_CONFUSABLES.items():
        if wrong in work:
            numeral_repairs[wrong] = right
            work = work.replace(wrong, right)

    own_id = parent_id = None
    observed_label: str | None = None
    label_from_geometry = False
    consumed: list[tuple[int, int]] = []
    expected_parent = parent_label(own_label) if own_label else None

    variants_seen: dict[str, str] = {}
    for m in _ID_RE.finditer(work):
        raw_label, numeral = m.group(1), m.group(2)
        label = canonical_label(raw_label)
        if label != raw_label:
            variants_seen[raw_label] = label
        end = m.end()
        # Backends concatenate the printed fields, so an id numeral runs straight
        # into the birth-order marker: `富二百九十五` + `四子` arrives as
        # `富二百九十五四子` and the greedy numeral swallows the 四.
        #
        # `允二百十九子` is genuinely ambiguous as text — id `…十九` + marker `子`,
        # or id `…十` + marker `九子`. The notation settles it: two bare digits
        # cannot sit next to each other inside one numeral (a unit separates
        # them), so a digit *following another digit* cannot belong to the id and
        # must start the marker. Where that test does not fire, the id keeps the
        # character rather than being trimmed on a guess.
        if (
            len(numeral) > 1
            and end < len(work)
            and work[end] in "子女"
            and numeral[-1] in DIGITS
            and numeral[-2] in DIGITS
        ):
            numeral = numeral[:-1]
            end -= 1
        token = label + numeral
        if own_label is not None:
            if label == own_label and own_id is None:
                own_id = token
                consumed.append((m.start(), end))
            elif label == expected_parent and parent_id is None:
                parent_id = token
                consumed.append((m.start(), end))
            continue
        # No band known: fall back to generation order — the older label is the
        # parent, the younger one is the entry itself.
        if own_id is None:
            own_id = token
            consumed.append((m.start(), end))
        elif parent_id is None:
            from familyocr.context import generation_chain as _chain
            order = _chain() or list(GENERATION_CHAIN)
            if own_id[0] not in order or label not in order:
                continue
            older = min(own_id[0], label, key=order.index)
            if label == older:
                parent_id = token
            else:
                parent_id, own_id = own_id, token
            consumed.append((m.start(), end))

    if own_id is None and trust_band and own_label:
        # No id carried the expected label. A line that is a numeral run behind
        # a single glyph is the id, and that glyph is the misrecognized label.
        #
        # Every line, not just the first: 丙辰庶富教1 prints the id first, but
        # 丙辰庶富教2 prints it last, behind the father's name and the birth
        # order, so looking only at the head missed that volume's 庶 band almost
        # entirely. Name and order lines cannot match — the pattern needs two
        # adjacent numeral characters, and `純一`, `武功` and `三子` have one at
        # most.
        offset = 0
        for line in work.split("\n"):
            m = _LEADING_NUMERAL.match(line.strip())
            if m:
                own_id = own_label + m.group("num")
                observed_label = m.group("pre")
                label_from_geometry = True
                start = offset + line.index(line.strip())
                consumed.append((start, start + m.end()))
                break
            offset += len(line) + 1

    order = None
    for om in _ORDER_RE.finditer(work):
        # An id numeral immediately followed by 子/女 would match here too; only
        # accept a match that starts outside every id already claimed.
        if not any(a <= om.start() < b for a, b in consumed):
            order = om.group(0)
            consumed.append(om.span())
            break

    if order is None:
        # A bare 子 is the marker for an only son. It carries the same rank as
        # 長子 but is written differently, so it is recorded as it appears and
        # ranked by `order_rank` rather than rewritten.
        for i, ch in enumerate(work):
            if ch in "子女" and not any(a <= i < b for a, b in consumed):
                order = ch
                consumed.append((i, i + 1))
                break

    kept = "".join(
        ch for i, ch in enumerate(work)
        if not any(a <= i < b for a, b in consumed)
    ).strip()

    # When the father's generation id is unknown the page names him instead of
    # numbering him, so an unclaimed short run of non-numeral characters in an
    # entry that has no parent id is a parent *name*, not stray text.
    parent_name = None
    if parent_id is None and kept:
        candidate = "".join(ch for ch in kept if ch not in "\n\r\t ")
        if 1 <= len(candidate) <= 4 and not any(
            ch in NUMERAL_CHARS or ch in _LABEL_CHARS for ch in candidate
        ):
            parent_name = candidate
            kept = ""

    return ParsedEntry(
        own_id, parent_id, order, kept, raw, variants_seen, parent_name,
        label_from_geometry, observed_label, numeral_repairs,
    )


# Scored fields. `parent_name` is scored alongside `parent_id` because the two
# are alternatives on the page: whichever one the entry carries is the link to
# the previous generation.
FIELDS = ("own_id", "parent_id", "parent_name", "order")
