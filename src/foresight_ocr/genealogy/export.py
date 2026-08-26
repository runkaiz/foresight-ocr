"""Handing the reconstructed family off to something that is not this pipeline.

Two formats, for two different readers. The TSV is for a person opening it in a
spreadsheet to check the work; GEDCOM is for genealogy software, which is where
a family record actually gets used.

Only resolved people become GEDCOM families. A father link that did not resolve
is left out rather than guessed at, and the count of what was left out is
returned so the caller can say so.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from foresight_ocr.export_order import generation_sort_key

_HEADER = (
    "person_key\tgeneration\town_id\town_value\tfather_key\tfather_name\t"
    "birth_order\tadditional_info\tlink_status"
)


def _one_line(value) -> str:
    """Render one database value without breaking a TSV or GEDCOM record."""
    return "" if value is None else " ".join(str(value).split())


def write_tsv(
    conn,
    document_id: str,
    path: Path,
    *,
    generation_labels: Iterable[str] | None = None,
) -> int:
    labels = tuple(generation_labels) if generation_labels is not None else None
    rows = list(
        conn.execute(
            """SELECT p.id, p.person_key, p.generation, p.own_id, p.own_value,
                  p.father_key, p.father_name, p.birth_order,
                  e.leftover AS additional_info, p.link_status
           FROM persons p
           LEFT JOIN parsed_entries e ON e.id = p.parsed_entry_id
           LEFT JOIN pages pg
             ON pg.document_id = p.document_id
            AND pg.page_index = e.page_index
           WHERE p.document_id = ?
             AND COALESCE(pg.ignored, 0) = 0""",
            (document_id,),
        ).fetchall()
    )
    rows.sort(
        key=lambda row: generation_sort_key(
            row["generation"],
            row["own_value"] is None,
            row["own_value"] if row["own_value"] is not None else 0,
            row["id"],
            labels=labels,
        )
    )
    lines = [f"# {document_id} — reconstructed people", _HEADER]
    lines.extend(
        "\t".join(
            _one_line(r[k])
            for k in (
                "person_key",
                "generation",
                "own_id",
                "own_value",
                "father_key",
                "father_name",
                "birth_order",
                "additional_info",
                "link_status",
            )
        )
        for r in rows
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(rows)


def write_gedcom(
    conn,
    document_id: str,
    path: Path,
    *,
    generation_labels: Iterable[str] | None = None,
) -> tuple[int, int]:
    """Write GEDCOM 5.5.1. Returns (individuals, families)."""
    labels = tuple(generation_labels) if generation_labels is not None else None
    rows = list(
        conn.execute(
            """SELECT p.id, p.person_key, p.generation, p.own_id,
                  p.own_value, p.father_person_id, p.father_key, p.father_name,
                  p.birth_order, e.leftover AS additional_info, p.link_status
           FROM persons p
           LEFT JOIN parsed_entries e ON e.id = p.parsed_entry_id
           LEFT JOIN pages pg
             ON pg.document_id = p.document_id
            AND pg.page_index = e.page_index
           WHERE p.document_id = ?
             AND COALESCE(pg.ignored, 0) = 0""",
            (document_id,),
        ).fetchall()
    )
    rows.sort(
        key=lambda row: generation_sort_key(
            row["generation"],
            row["own_value"] is None,
            row["own_value"] if row["own_value"] is not None else 0,
            row["id"],
            labels=labels,
        )
    )

    # GEDCOM models a family, not a parent link, so sons of one father are
    # collected into a single FAM record. A father the chart names but does not
    # number has no INDI record to point at, yet his sons are still brothers —
    # they get a family with no HUSB rather than no family at all, which is the
    # difference between recording a sibling set and losing it.
    children: dict[str, list] = {}
    for r in rows:
        if r["father_key"]:
            children.setdefault(r["father_key"], []).append(r)
    father_row_of = {
        r["person_key"]: r["id"] for r in rows if r["person_key"] in children
    }

    out: list[str] = [
        "0 HEAD",
        "1 SOUR foresight-ocr",
        "1 GEDC",
        "2 VERS 5.5.1",
        "2 FORM LINEAGE-LINKED",
        "1 CHAR UTF-8",
        f"1 FILE {path.name}",
    ]

    def family_sort_key(father_key: str):
        """Put GEDCOM families in their father's generation/id order too."""
        if ":" in father_key:
            label, raw_value = father_key.split(":", 1)
            try:
                return generation_sort_key(
                    label, 0, int(raw_value), father_key, labels=labels
                )
            except ValueError:
                return generation_sort_key(label, 1, 0, father_key, labels=labels)
        label = father_key.split("#", 1)[0]
        return generation_sort_key(label, 1, 0, father_key, labels=labels)

    ordered_fathers = sorted(children, key=family_sort_key)
    fam_of_father = {fkey: i + 1 for i, fkey in enumerate(ordered_fathers)}

    for r in rows:
        out.append(f"0 @I{r['id']}@ INDI")
        # The generation id is the only name the chart gives most people.
        out.append(f"1 NAME {r['own_id']}")
        out.append(f"1 _GEN {r['generation']}")
        if r["birth_order"]:
            out.append(f"1 _ORDER {r['birth_order']}")
        if r["additional_info"]:
            # GEDCOM is line-oriented. The reviewer may preserve the OCR's line
            # breaks, but a generic note must remain one valid GEDCOM record.
            note = _one_line(r["additional_info"])
            out.append(f"1 NOTE {note}")
        fam = fam_of_father.get(r["father_key"])
        if fam:
            out.append(f"1 FAMC @F{fam}@")
        own_fam = fam_of_father.get(r["person_key"])
        if own_fam:
            out.append(f"1 FAMS @F{own_fam}@")

    for fkey in ordered_fathers:
        kids = children[fkey]
        fam = fam_of_father[fkey]
        out.append(f"0 @F{fam}@ FAM")
        husb = father_row_of.get(fkey)
        if husb:
            out.append(f"1 HUSB @I{husb}@")
        else:
            # Named but unnumbered: record who the sons say he was, so the
            # family is not anonymous even though he has no record of his own.
            name = kids[0]["father_name"]
            if name:
                out.append(f"1 _FATHER_NAME {name}")
        for k in kids:
            out.append(f"1 CHIL @I{k['id']}@")

    out.append("0 TRLR")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return len(rows), len(children)
