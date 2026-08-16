"""Turning transcribed entries into people and the links between them.

Everything upstream describes the document: pages, bands, crops, candidate
transcriptions. This is where it becomes a family record — a set of people, each
with a father, reconstructed from what the chart prints.

The reconstruction is cheap because the 雁序圖 prints the links rather than
implying them. Each entry carries its own generation id, its father's id (or his
name, where the id was not known to the compiler), and its rank among his sons.
So a father link is a lookup, not an inference.

That also makes the graph a strong check on the transcription, which is the
second reason it exists. Three things must hold if the reading is right, and
none of them needs ground truth:

- every father id resolves to somebody in the previous generation;
- one id belongs to one person;
- sons of the same father carry distinct, ascending birth ranks.

A recognizer error breaks one of these far more often than it produces a
plausible wrong answer, so the failures point at the entries worth a human's
attention. Nothing here rewrites a transcription — findings are reported and the
text on record stays exactly what was read.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable

from familyocr.ocr.fields import ParsedEntry, parse_entry
from familyocr.validation.numerals import parse_numeral


def person_key(label: str, value: int) -> str:
    return f"{label}:{value}"


def named_father_key(label: str, name: str) -> str:
    """A family key for a father the chart names but does not number.

    Distinguished from a person key by its separator so the two can never be
    mistaken for one another: `富#存省` is a family, `富:349` is a man.
    """
    return f"{label}#{name}"


@dataclass
class EntryRow:
    """One entry, with the best transcription available for it.

    Two different notions of "band" live here and must not be confused.
    `band_label` is the generation the text says this person belongs to — his
    identity. `geometric_label` is the band he was cut from on the page — his
    address. They usually agree; where a misreading makes them differ, identity
    decides who he is and address decides where the reviewer must look.
    """
    source_region_id: int
    page_index: int
    band_label: str
    geometric_label: str
    entry_index: int
    text: str
    source: str          # human | ocr
    parsed: ParsedEntry
    own_value: int | None
    parent_value: int | None


@dataclass
class GraphFinding:
    kind: str            # unresolved_father | duplicate_id | order_conflict | …
    band_label: str
    page_index: int | None
    entry_index: int | None
    expected: str | None
    observed: str | None


@dataclass
class GraphResult:
    people: int
    entries: int
    by_generation: dict[str, int]
    link_status: dict[str, int]
    findings: list[GraphFinding] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = defaultdict(int)
        for f in self.findings:
            out[f.kind] += 1
        return dict(out)


def _value_of(text: str | None) -> int | None:
    """The numeric part of a generation id, e.g. 庶三百三十五 -> 335."""
    if not text or len(text) < 2:
        return None
    try:
        return parse_numeral(text[1:])
    except Exception:
        return None


def resolve_entry(text: str, geometric: str, charted: list[str],
                  parent_of: dict[str, str | None]) -> tuple[str, ParsedEntry]:
    """Decide which generation an entry belongs to, then parse it as that.

    `parse_entry` needs to be told which label is the person's own, because the
    same character is an own id in one band and a father's id in the next. Band
    position usually answers that, but not on pages carrying fewer bands than
    the volume charts — there, index 1 is 教, not 富, and parsing it as 富 takes
    the father's number as the son's.

    So each charted generation is tried and the reading that holds together wins:
    an own id was found, and the father's label is the generation directly above
    it. Ties go to the label the page geometry suggested.
    """
    best: tuple[int, str, ParsedEntry] | None = None
    for label in charted:
        parsed = parse_entry(text, own_label=label, trust_band=False)
        score = 0
        if parsed.own_id:
            score += 2
        if parsed.parent_id and parsed.parent_id[0] == parent_of.get(label):
            score += 2
        elif parsed.parent_name:
            score += 1
        if label == geometric:
            score += 1        # only a tiebreak; evidence on the page outranks it
        if best is None or score > best[0]:
            best = (score, label, parsed)
    if best is None:
        return geometric, parse_entry(text, own_label=geometric, trust_band=False)
    if best[2].own_id is None:
        # No charted generation reads as this entry's own label, which is the
        # case `trust_band` exists for: 丙辰庶富教2 returns 庚 for 庶 on most of
        # that band, and 庚 is not a generation at all. The band came from rule
        # and frame detection, independently of the recognizer, so it can supply
        # the label the glyph lost — and the substitution is recorded.
        #
        # Only ever as a last resort. Forcing geometry onto an entry whose label
        # *did* read makes the parser take the father's number as the son's.
        recovered = parse_entry(text, own_label=geometric, trust_band=True)
        if recovered.own_id is not None:
            return geometric, recovered
    return best[1], best[2]


def build_entries(rows: Iterable[dict[str, Any]], band_of: dict[int, str],
                  parent_of: dict[str, str | None]) -> list[EntryRow]:
    """Parse each entry into the generation the text says it belongs to.

    Deliberately without `trust_band`: forcing the geometric label onto an entry
    whose own label was read fine makes the parser take the *father's* number as
    the person's own — `允二十七 … 庻四十` becomes 庶二十七, a person who does not
    exist, colliding with the one who does. An entry that will not parse is left
    unlinked, which is visible; a confidently wrong link is not.
    """
    out: list[EntryRow] = []
    charted = [band_of[i] for i in sorted(band_of)]
    for r in rows:
        geometric = band_of.get(r["band_index"], str(r["band_index"]))
        text = r["text"] or ""
        label, parsed = resolve_entry(text, geometric, charted, parent_of)
        out.append(EntryRow(
            source_region_id=r["source_region_id"],
            page_index=r["page_index"],
            band_label=label,
            geometric_label=geometric,
            entry_index=r["entry_index"],
            text=text,
            source=r["source"],
            parsed=parsed,
            own_value=_value_of(parsed.own_id),
            parent_value=_value_of(parsed.parent_id),
        ))
    return out


def build_graph(entries: list[EntryRow], parent_of: dict[str, str | None],
                charted: set[str] | None = None) -> GraphResult:
    """Assemble people from entries and resolve each father link.

    `parent_of` maps a generation label to the label of the generation above it,
    which comes from the document profile rather than being assumed: the volumes
    do not all chart the same generations.

    `charted` is the set of generations this volume actually contains. The
    oldest one charted has its fathers in the *previous* volume — every 庶 entry
    names a 允 father, and 允 is not in this book — so those links are recorded
    as reaching outside the volume rather than counted as failures. Without the
    distinction the first generation reads as 1153 broken links.
    """
    charted = charted if charted is not None else set()
    findings: list[GraphFinding] = []

    # One id, one person. A repeat means two entries were read as the same
    # person, which is a transcription error rather than a family fact.
    people: dict[str, EntryRow] = {}
    for e in entries:
        if e.own_value is None:
            continue
        key = person_key(e.band_label, e.own_value)
        if key in people:
            first = people[key]
            findings.append(GraphFinding(
                kind="duplicate_id", band_label=e.geometric_label,
                page_index=e.page_index, entry_index=e.entry_index,
                expected=f"{key} already on page {first.page_index}",
                observed=e.text.replace("\n", " ")[:40],
            ))
            continue
        people[key] = e

    link_status: dict[str, int] = defaultdict(int)
    resolved: dict[str, str | None] = {}
    for key, e in people.items():
        above = parent_of.get(e.band_label)
        if above is None:
            resolved[key] = None
            link_status["root"] += 1
            continue
        if above not in charted:
            # His father is in the preceding volume. The link is real and the
            # id is recorded; it just cannot be resolved from this book alone.
            resolved[key] = None
            link_status["outside_volume"] += 1
            continue
        if e.parent_value is not None:
            fkey = person_key(above, e.parent_value)
            if fkey in people:
                resolved[key] = fkey
                link_status["resolved"] += 1
            else:
                # The father's id was read, but nobody in the generation above
                # carries it. Either his entry was misread or this one was.
                resolved[key] = None
                link_status["unresolved"] += 1
                findings.append(GraphFinding(
                    kind="unresolved_father", band_label=e.geometric_label,
                    page_index=e.page_index, entry_index=e.entry_index,
                    expected=fkey, observed=e.text.replace("\n", " ")[:40],
                ))
        elif e.parsed.parent_name:
            # The chart names the father instead of numbering him, which the
            # project owner confirmed happens where his id was not known. He
            # cannot be matched to a person record — the numbered generations
            # carry no names — but his sons are still each other's brothers, so
            # they are grouped under a key made from the name. The grouping is
            # weaker evidence than a resolved id and is labelled as such; where
            # two men share a name, the birth-order check below says so.
            resolved[key] = named_father_key(above, e.parsed.parent_name)
            link_status["named_only"] += 1
        else:
            resolved[key] = None
            link_status["unresolved"] += 1
            findings.append(GraphFinding(
                kind="no_father_field", band_label=e.geometric_label,
                page_index=e.page_index, entry_index=e.entry_index,
                expected=f"a {above} id or a name",
                observed=e.text.replace("\n", " ")[:40],
            ))

    findings.extend(_order_conflicts(people, resolved))

    by_gen: dict[str, int] = defaultdict(int)
    for e in people.values():
        by_gen[e.band_label] += 1

    return GraphResult(
        people=len(people), entries=len(entries),
        by_generation=dict(by_gen), link_status=dict(link_status),
        findings=findings,
    )


def _order_conflicts(people: dict[str, EntryRow],
                     resolved: dict[str, str | None]) -> list[GraphFinding]:
    """Sons of one father must carry distinct ranks, ascending with their ids.

    This is the check that catches what per-crop confidence cannot: a 五子 read
    as 子 sits in an otherwise perfect 長-次-三-四-六 run and is obvious only
    once the siblings are side by side.
    """
    siblings: dict[str, list[EntryRow]] = defaultdict(list)
    for key, fkey in resolved.items():
        if fkey:
            siblings[fkey].append(people[key])

    findings: list[GraphFinding] = []
    for fkey, kids in siblings.items():
        kids.sort(key=lambda e: e.own_value or 0)
        ranks = [(e, e.parsed.order_rank) for e in kids]
        seen: dict[int, EntryRow] = {}
        for e, rank in ranks:
            if rank is None:
                continue
            if rank in seen:
                findings.append(GraphFinding(
                    kind="order_conflict", band_label=e.geometric_label,
                    page_index=e.page_index, entry_index=e.entry_index,
                    expected=f"a rank not already used among {fkey}'s sons",
                    observed=f"{e.parsed.order} duplicates page "
                             f"{seen[rank].page_index}",
                ))
            seen[rank] = e
        known = [(e, r) for e, r in ranks if r is not None]
        for (e1, r1), (e2, r2) in zip(known, known[1:]):
            if r2 < r1:
                findings.append(GraphFinding(
                    kind="order_reversed", band_label=e2.geometric_label,
                    page_index=e2.page_index, entry_index=e2.entry_index,
                    expected=f"a rank above {e1.parsed.order}",
                    observed=f"{e2.parsed.order} after {e1.parsed.order}",
                ))
    return findings


def store_graph(conn, document_id: str, entries: list[EntryRow],
                parent_of: dict[str, str | None],
                charted: set[str] | None = None) -> GraphResult:
    """Rebuild both derived tables for a document, then resolve the links."""
    conn.execute("DELETE FROM persons WHERE document_id = ?", (document_id,))
    conn.execute("DELETE FROM parsed_entries WHERE document_id = ?", (document_id,))

    entry_ids: dict[int, int] = {}
    for e in entries:
        p = e.parsed
        flags = {
            "label_from_geometry": p.label_from_geometry,
            "observed_label": p.observed_label,
            "label_variant": p.label_variant,
            "numeral_repairs": p.numeral_repairs,
        }
        cur = conn.execute(
            """INSERT INTO parsed_entries
               (document_id, source_region_id, page_index, band_label,
                entry_index, source, text, own_id, own_value, parent_id,
                parent_value, parent_name, birth_order, order_rank, leftover,
                flags_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (document_id, e.source_region_id, e.page_index, e.band_label,
             e.entry_index, e.source, e.text, p.own_id, e.own_value,
             p.parent_id, e.parent_value, p.parent_name, p.order,
             p.order_rank, p.leftover,
             json.dumps(flags, ensure_ascii=False)),
        )
        entry_ids[e.source_region_id] = int(cur.lastrowid)

    charted = charted if charted is not None else set()
    result = build_graph(entries, parent_of, charted)

    # Insert people first without their father link, then fill it in: a father
    # is another row in this same table and may not exist yet.
    people: dict[str, EntryRow] = {}
    for e in entries:
        if e.own_value is None:
            continue
        key = person_key(e.band_label, e.own_value)
        people.setdefault(key, e)

    # One place decides a person's father key, so the insert and the later
    # status pass cannot disagree — they did, and named fathers ended up with a
    # status of named_only and no key to group their sons by.
    def father_key_of(e: EntryRow) -> str | None:
        above = parent_of.get(e.band_label)
        if above is None or above not in charted:
            return None
        if e.parent_value is not None:
            return person_key(above, e.parent_value)
        if e.parsed.parent_name:
            return named_father_key(above, e.parsed.parent_name)
        return None

    row_ids: dict[str, int] = {}
    for key, e in people.items():
        fkey = father_key_of(e)
        cur = conn.execute(
            """INSERT INTO persons
               (document_id, person_key, generation, own_id, own_value,
                parsed_entry_id, father_key, father_name, birth_order,
                order_rank, link_status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (document_id, key, e.band_label, e.parsed.own_id, e.own_value,
             entry_ids.get(e.source_region_id),
             fkey, e.parsed.parent_name, e.parsed.order, e.parsed.order_rank,
             "pending"),
        )
        row_ids[key] = int(cur.lastrowid)

    for key, e in people.items():
        above = parent_of.get(e.band_label)
        if above is None:
            status, father_row = "root", None
        elif above not in charted:
            status, father_row = "outside_volume", None
        elif e.parent_value is not None:
            father_row = row_ids.get(person_key(above, e.parent_value))
            status = "resolved" if father_row else "unresolved"
        elif e.parsed.parent_name:
            status, father_row = "named_only", None
        else:
            status, father_row = "unresolved", None
        conn.execute(
            "UPDATE persons SET father_person_id = ?, link_status = ? WHERE id = ?",
            (father_row, status, row_ids[key]),
        )
    conn.commit()
    return result
