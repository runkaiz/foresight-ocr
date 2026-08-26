"""Splitting an entry transcription into its printed fields.

A 雁序圖 entry carries three core things, and the OCR smoke test made that
visible:

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

Some later pages append biographical text, most often a birth date. The splitter
is deliberately shallow: it labels the core fields and hands back everything
else in `leftover` rather than forcing that free-form information into a date
model.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from dataclasses import field as dc_field
from typing import Any

from foresight_ocr.validation.numerals import CONTRACTED, DIGITS, UNITS

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
    from foresight_ocr.context import get_profile

    return get_profile().label_variants.get(ch, LABEL_VARIANTS.get(ch, ch))


def parent_label(own_label: str) -> str | None:
    """The label one generation above `own_label`, per the active profile."""
    from foresight_ocr.context import generation_chain

    chain = generation_chain() or list(GENERATION_CHAIN)
    if own_label not in chain:
        return None
    i = chain.index(own_label)
    return chain[i - 1] if i > 0 else None


NUMERAL_CHARS = "".join(sorted(set(DIGITS) | set(UNITS) | set(CONTRACTED)))

_LABEL_CHARS = "".join(ALL_LABELS) + "".join(LABEL_VARIANTS)
_ID_RE = re.compile(f"([{_LABEL_CHARS}])([{NUMERAL_CHARS}]+)")
# 長子 / 次子 / 三子 / 幼子 / 繼子 / 長女 …
# 繼子 is a relationship designation rather than an ordinal number, but it is
# printed in the same field and must not leak into free-form additional info.
_ORDER_RE = re.compile(f"([長次幼元繼{NUMERAL_CHARS}])([子女])")


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
    # Known library-logo text ignored before field parsing. The raw OCR remains
    # in `text`, so this derived cleanup is inspectable rather than destructive.
    watermark_noise: list[str] = dc_field(default_factory=list)
    # A parent label recovered from the document's generation structure. This
    # is separate from orthographic variants: the raw glyph may be a true OCR
    # error such as 尤 for 允, and must remain visible for audit.
    parent_label_from_structure: bool = False
    observed_parent_label: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def field(self, name: str) -> str | None:
        return {
            "own_id": self.own_id,
            "parent_id": self.parent_id,
            "order": self.order,
            "parent_name": self.parent_name,
        }.get(name)

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
            return -1  # last child; position unknown without siblings
        if head == "繼":
            return None  # relationship known, ordinal position unknown
        value = DIGITS.get(head)
        return value if value else None


def _numeral_confusions() -> dict[str, str]:
    """Safe global repairs plus the active document's reviewed confusions."""
    from foresight_ocr.context import get_profile

    configured = get_profile().numeral_confusions
    # A configured repair must remain one glyph wide so spans into the raw OCR
    # stay valid and auditable.
    return {
        wrong: right
        for wrong, right in {**NUMERAL_CONFUSABLES, **configured}.items()
        if len(wrong) == len(right) == 1 and right in NUMERAL_CHARS
    }


def _numeral_pattern() -> tuple[re.Pattern[str], re.Pattern[str], set[str]]:
    from foresight_ocr.context import get_profile

    repairs = _numeral_confusions()
    chars = set(NUMERAL_CHARS) | set(repairs)
    escaped = re.escape("".join(sorted(chars)))
    labels = re.escape(_LABEL_CHARS + "".join(get_profile().label_variants))
    return (
        re.compile(f"([{labels}])([{escaped}]+)"),
        re.compile(f"^(?P<pre>[^{escaped}\\s])?\\s*(?P<num>[{escaped}]{{2,}})"),
        chars,
    )


def _repair_numeral(value: str, repairs_seen: dict[str, str]) -> str:
    repairs = _numeral_confusions()
    out = []
    for ch in value:
        fixed = repairs.get(ch, ch)
        if fixed != ch:
            repairs_seen[ch] = fixed
        out.append(fixed)
    return "".join(out)


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
    from foresight_ocr.ocr.watermarks import filter_watermark_text

    observed = (text or "").strip()
    filtered = filter_watermark_text(observed)
    raw = filtered.transcription or ""
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
            # Keep the first reliable own-id interpretation, but let document
            # structure fill a still-missing parent from the *same spelling*.
            # This avoids the joined-text reinterpretation that the early return
            # deliberately guards against while making trust_band useful on an
            # otherwise successfully parsed entry.
            if trust_band and not trust and parsed.parent_id is None:
                augmented = _parse_fields(candidate, own_label, True)
                if (
                    augmented.own_id == parsed.own_id
                    and augmented.parent_id is not None
                ):
                    parsed = augmented
            parsed.text = observed  # the transcription on record is what was read
            parsed.watermark_noise = list(filtered.removed)
            return parsed
        if fallback is None:
            fallback = parsed
    if fallback is None:
        raise RuntimeError("entry parser produced no fallback result")
    fallback.text = observed
    fallback.watermark_noise = list(filtered.removed)
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

    # Matching runs against the OCR text; only characters inside an id numeral
    # are repaired below. `raw` is always what gets reported.
    numeral_repairs: dict[str, str] = {}
    work = raw
    id_re, leading_numeral, numeral_input_chars = _numeral_pattern()

    own_id = parent_id = None
    observed_label: str | None = None
    label_from_geometry = False
    consumed: list[tuple[int, int]] = []
    expected_parent = parent_label(own_label) if own_label else None

    variants_seen: dict[str, str] = {}
    for id_match in id_re.finditer(work):
        raw_label = id_match.group(1)
        numeral = _repair_numeral(id_match.group(2), numeral_repairs)
        label = canonical_label(raw_label)
        if label != raw_label:
            variants_seen[raw_label] = label
        end = id_match.end()
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
                consumed.append((id_match.start(), end))
            elif label == expected_parent and parent_id is None:
                parent_id = token
                consumed.append((id_match.start(), end))
            continue
        # No band known: fall back to generation order — the older label is the
        # parent, the younger one is the entry itself.
        if own_id is None:
            own_id = token
            consumed.append((id_match.start(), end))
        elif parent_id is None:
            from foresight_ocr.context import generation_chain as _chain

            generation_order = _chain() or list(GENERATION_CHAIN)
            if own_id[0] not in generation_order or label not in generation_order:
                continue
            older = min(own_id[0], label, key=generation_order.index)
            if label == older:
                parent_id = token
            else:
                parent_id, own_id = own_id, token
            consumed.append((id_match.start(), end))

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
        own_candidates: list[tuple[str | None, str, int, int]] = []
        offset = 0
        for line in work.split("\n"):
            stripped = line.strip()
            leading_match = leading_numeral.match(stripped)
            if leading_match:
                pre = leading_match.group("pre")
                # A correctly recognized parent id is evidence against treating
                # its numeral as the person's own. The old first-match rule did
                # exactly that whenever the parent line preceded a blurred own
                # label, producing plausible but wrong duplicate ids.
                if pre and canonical_label(pre) == expected_parent:
                    offset += len(line) + 1
                    continue
                start = offset + line.index(stripped)
                own_candidates.append(
                    (
                        pre,
                        leading_match.group("num"),
                        start,
                        start + leading_match.end(),
                    )
                )
            offset += len(line) + 1
        if len(own_candidates) == 1:
            pre, numeral, start, end = own_candidates[0]
            own_id = own_label + _repair_numeral(numeral, numeral_repairs)
            observed_label = pre
            label_from_geometry = True
            consumed.append((start, end))

    if own_id is None and own_label:
        # On the ruled title page the person's own label and numeral occupy two
        # narrow sub-columns with the father's id between them.  OCR preserves
        # that spatial reading order as separate lines:
        #
        #     庶 / 允六 / 二 / 長子
        #
        # This is ``庶二``, not an unparsed 庶 plus a stray 二.  The allowance is
        # deliberately strict: both pieces must be whole lines, the label must
        # canonicalise to this region's known band, and exactly one unclaimed
        # numeral line may exist.  Nothing is filled from sequence expectation.
        label_spans: list[tuple[str, int, int]] = []
        numeral_spans: list[tuple[str, int, int]] = []
        offset = 0
        for line in work.split("\n"):
            stripped = line.strip()
            start = offset + line.index(stripped) if stripped else offset
            end = start + len(stripped)
            claimed = any(a <= start < b for a, b in consumed)
            if not claimed:
                if (
                    len(stripped) == 1
                    and stripped not in numeral_input_chars
                    and stripped not in "子女"
                ):
                    label_spans.append((stripped, start, end))
                if stripped and all(ch in numeral_input_chars for ch in stripped):
                    numeral_spans.append((stripped, start, end))
            offset += len(line) + 1
        exact = [span for span in label_spans if canonical_label(span[0]) == own_label]
        # A recognized parent id anchors the other field: one remaining
        # label-like glyph plus numeral-only lines is the split own id. Without
        # that anchor, an unknown glyph stays unknown rather than becoming a
        # sequence-based guess.
        usable_labels = exact or (label_spans if parent_id is not None else [])
        if len(usable_labels) == 1 and numeral_spans:
            glyph, label_start, label_end = usable_labels[0]
            following = [span for span in numeral_spans if span[1] > label_end]
            # Multiple split digits belong to the label that precedes them. A
            # single digit can sit on either side on the ruled title page, so
            # retain the earlier validated behaviour in that case.
            selected_numerals = following or (
                numeral_spans if len(numeral_spans) == 1 else []
            )
        else:
            selected_numerals = []
        if len(usable_labels) == 1 and selected_numerals:
            glyph, label_start, label_end = usable_labels[0]
            numeral = _repair_numeral(
                "".join(span[0] for span in selected_numerals), numeral_repairs
            )
            own_id = own_label + numeral
            canonical = canonical_label(glyph)
            if canonical == own_label and canonical != glyph:
                variants_seen[glyph] = canonical
            elif canonical != own_label:
                observed_label = glyph
                label_from_geometry = True
            consumed.append((label_start, label_end))
            consumed.extend((start, end) for _, start, end in selected_numerals)

    parent_label_from_structure = False
    observed_parent_label: str | None = None
    if parent_id is None and trust_band and own_id and expected_parent:
        from foresight_ocr.context import get_profile

        configured_labels = get_profile().label_confusions
        numeral_chars = re.escape("".join(sorted(numeral_input_chars)))
        configured_parent = re.compile(f"^(?P<pre>[^\\s])(?P<num>[{numeral_chars}]+)$")
        parent_candidates: list[tuple[str, str, int, int]] = []
        offset = 0
        for line in work.split("\n"):
            stripped = line.strip()
            parent_match = configured_parent.fullmatch(stripped)
            if parent_match:
                start = offset + line.index(stripped)
                if not any(a <= start < b for a, b in consumed):
                    pre = parent_match.group("pre")
                    if configured_labels.get(pre) == expected_parent:
                        parent_candidates.append(
                            (
                                pre,
                                parent_match.group("num"),
                                start,
                                start + parent_match.end(),
                            )
                        )
            offset += len(line) + 1
        if len(parent_candidates) == 1:
            pre, numeral, start, end = parent_candidates[0]
            parent_id = expected_parent + _repair_numeral(numeral, numeral_repairs)
            observed_parent_label = pre
            parent_label_from_structure = canonical_label(pre) != expected_parent
            consumed.append((start, end))

    if parent_id is None and trust_band and own_id and expected_parent:
        # The same ruled title-page layout can split the *parent* id into a label
        # line followed by one or more numeral lines. This recovery requires the
        # expected parent glyph to be visibly present; it never invents a blank
        # identifier from entry order.
        lines: list[tuple[str, int, int]] = []
        offset = 0
        for line in work.split("\n"):
            stripped = line.strip()
            start = offset + line.index(stripped) if stripped else offset
            lines.append((stripped, start, start + len(stripped)))
            offset += len(line) + 1
        labels = [
            (index, start, end)
            for index, (value, start, end) in enumerate(lines)
            if len(value) == 1
            and canonical_label(value) == expected_parent
            and not any(a <= start < b for a, b in consumed)
        ]
        if len(labels) == 1:
            index, label_start, label_end = labels[0]
            pieces: list[tuple[str, int, int]] = []
            for value, start, end in lines[index + 1 :]:
                if not value or not all(ch in numeral_input_chars for ch in value):
                    break
                if any(a <= start < b for a, b in consumed):
                    break
                pieces.append((value, start, end))
            if pieces:
                parent_id = expected_parent + _repair_numeral(
                    "".join(value for value, _, _ in pieces), numeral_repairs
                )
                consumed.append((label_start, label_end))
                consumed.extend((start, end) for _, start, end in pieces)

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
        # ranked by `order_rank` rather than rewritten. Do not take the 子 in
        # 子時 as that marker: later pages print birthdays with the traditional
        # two-hour birth time beside the same core fields.
        for i, ch in enumerate(work):
            followed_by_hour = ch == "子" and work[i + 1 : i + 2] in ("時", "时")
            if (
                ch in "子女"
                and not followed_by_hour
                and not any(a <= i < b for a, b in consumed)
            ):
                order = ch
                consumed.append((i, i + 1))
                break

    kept = "".join(
        ch for i, ch in enumerate(work) if not any(a <= i < b for a, b in consumed)
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
        own_id=own_id,
        parent_id=parent_id,
        order=order,
        leftover=kept,
        text=raw,
        label_variant=variants_seen,
        parent_name=parent_name,
        label_from_geometry=label_from_geometry,
        observed_label=observed_label,
        numeral_repairs=numeral_repairs,
        parent_label_from_structure=parent_label_from_structure,
        observed_parent_label=observed_parent_label,
    )


# Scored fields. `parent_name` is scored alongside `parent_id` because the two
# are alternatives on the page: whichever one the entry carries is the link to
# the previous generation.
FIELDS = ("own_id", "parent_id", "parent_name", "order")


def compose_entry(
    own_id: str | None,
    parent: str | None,
    birth_order: str | None,
    additional_info: str | None = None,
) -> str:
    """Assemble the printed fields and optional biographical text.

    Both charted volumes print own id, then the father's id or name, then the
    birth-order marker, so joining in that order reproduces the line rather than
    inventing a format. Reading it back through `parse_entry` returns the same
    core fields and free-form remainder, which is the property that lets a
    reviewer edit fields while the record stays a transcription.

    Additional information is separated from the compact core with a newline.
    This keeps a birthday (or any other note the page actually prints) on record
    without making it look like part of the father's id or birth-order marker.

    What is deliberately not preserved is the recognizer's line breaking, or
    noise the reviewer does not put in ``additional_info`` — the library stamp
    reads as 國 and even as `IBRARY` in the middle band. That is not the
    reviewer's text to keep.
    """
    core = "".join(part for part in (own_id, parent, birth_order) if part)
    extra = (additional_info or "").strip()
    if not extra:
        return core
    return f"{core}\n{extra}" if core else extra


def own_id_from_digits(label: str, digits: str) -> str | None:
    """`(庶, "343")` -> `庶三百四十三`, for a reviewer typing arabic numerals.

    Entering 庶三百四十三 by keyboard is several times the work of entering 343,
    and the transcription that gets stored is identical. Returns None when the
    input is not a plain non-negative integer, so a reviewer typing CJK directly
    is left alone.
    """
    from foresight_ocr.validation.numerals import format_numeral

    text = (digits or "").strip()
    if not text.isdigit():
        return None
    return f"{label}{format_numeral(int(text))}"
