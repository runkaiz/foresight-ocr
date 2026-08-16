"""Handing the reconstructed family off to something that is not this pipeline.

Two formats, for two different readers. The TSV is for a person opening it in a
spreadsheet to check the work; GEDCOM is for genealogy software, which is where
a family record actually gets used.

Only resolved people become GEDCOM families. A father link that did not resolve
is left out rather than guessed at, and the count of what was left out is
returned so the caller can say so.
"""

from __future__ import annotations

from pathlib import Path

_HEADER = (
    "person_key\tgeneration\town_id\town_value\tfather_key\tfather_name\t"
    "birth_order\tlink_status"
)


def write_tsv(conn, document_id: str, path: Path) -> int:
    rows = conn.execute(
        """SELECT person_key, generation, own_id, own_value, father_key,
                  father_name, birth_order, link_status
           FROM persons WHERE document_id = ?
           ORDER BY generation, own_value""",
        (document_id,),
    ).fetchall()
    lines = [f"# {document_id} — reconstructed people", _HEADER]
    lines.extend(
        "\t".join("" if r[k] is None else str(r[k]) for k in (
            "person_key", "generation", "own_id", "own_value", "father_key",
            "father_name", "birth_order", "link_status"))
        for r in rows
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(rows)


def write_gedcom(conn, document_id: str, path: Path) -> tuple[int, int]:
    """Write GEDCOM 5.5.1. Returns (individuals, families)."""
    rows = conn.execute(
        """SELECT id, person_key, generation, own_id, father_person_id,
                  father_key, father_name, birth_order, link_status
           FROM persons WHERE document_id = ?
           ORDER BY generation, own_value""",
        (document_id,),
    ).fetchall()

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
        "1 SOUR familyocr",
        "1 GEDC", "2 VERS 5.5.1", "2 FORM LINEAGE-LINKED",
        "1 CHAR UTF-8",
        f"1 FILE {path.name}",
    ]
    fam_of_father = {fkey: i + 1 for i, fkey in enumerate(sorted(children))}

    for r in rows:
        out.append(f"0 @I{r['id']}@ INDI")
        # The generation id is the only name the chart gives most people.
        out.append(f"1 NAME {r['own_id']}")
        out.append(f"1 _GEN {r['generation']}")
        if r["birth_order"]:
            out.append(f"1 _ORDER {r['birth_order']}")
        fam = fam_of_father.get(r["father_key"])
        if fam:
            out.append(f"1 FAMC @F{fam}@")
        own_fam = fam_of_father.get(r["person_key"])
        if own_fam:
            out.append(f"1 FAMS @F{own_fam}@")

    for fkey, kids in sorted(children.items()):
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
