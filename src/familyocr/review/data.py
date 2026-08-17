"""Review data access.

The reviewer works a page at a time, so everything needed for one page is
assembled in a single query pass: the entries in reading order, the machine
transcription, any existing human correction, and the validation findings that
apply.

Machine output and human output are kept in separate fields all the way to the
browser. The UI pre-fills the editable box with the machine text so the reviewer
edits rather than retypes, but the machine transcription is never overwritten —
a correction is a new row in `human_corrections`, keyed on the stable
(document, page, band, entry, role) tuple so it survives reprocessing.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from familyocr.context import BAND_LABELS


@dataclass
class PageImage:
    page_index: int
    path: str | None
    width: int
    height: int
    frame_status: str | None


#: Findings that say a transcription is wrong, as opposed to findings about the
#: family structure. The reviewer is reading glyphs, so these are the ones that
#: should send them to an entry; `no_father_field` usually means the page simply
#: does not print one.
TRANSCRIPTION_FINDINGS = (
    "gap", "non_monotonic", "duplicate", "unparsed", "band_mismatch",
    "duplicate_id",
)


@dataclass
class ReviewEntry:
    page_index: int
    band_index: int
    band_label: str
    entry_index: int
    crop_path: str | None
    bbox: list[float] | None      # canonical page coordinates
    role: str
    machine: str | None
    machine_backend: str | None
    human: str | None
    unreadable: bool
    note: str | None
    findings: list[dict[str, Any]]
    # The three printed fields, parsed from whichever transcription is current.
    # The reviewer edits these rather than the whole line: an id is six or seven
    # characters of which one is usually wrong, and finding that one inside a
    # single text box is the slow part.
    own_id: str | None = None
    parent: str | None = None          # father's id, or his name where printed
    birth_order: str | None = None
    # The generation character each id field carries, so the reviewer can type
    # digits and get the printed form back.
    own_label: str | None = None
    parent_label: str | None = None
    leftover: str | None = None        # what parsing could not account for
    # What the sequence checksum says this entry's id must be. Present only for
    # a break in the run, which is exactly when the reviewer needs it.
    expected_own_id: str | None = None
    flagged: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def latest_ocr_tag(conn, document_id: str) -> str | None:
    row = conn.execute(
        """SELECT r.tag FROM ocr_candidates oc
           JOIN ocr_runs r ON r.id = oc.ocr_run_id
           JOIN source_regions sr ON sr.id = oc.source_region_id
           WHERE sr.document_id = ?
           ORDER BY oc.id DESC LIMIT 1""",
        (document_id,),
    ).fetchone()
    return row["tag"] if row else None


def reviewable_pages(conn, document_id: str) -> list[int]:
    return [
        r["page_index"]
        for r in conn.execute(
            "SELECT DISTINCT page_index FROM source_regions "
            "WHERE document_id = ? ORDER BY page_index",
            (document_id,),
        )
    ]


def page_summary(conn, document_id: str) -> list[dict[str, Any]]:
    """Per page: how much is there, how much is suspect, how much is confirmed.

    The reviewer works front to back, so this is not a worklist. It is what
    lets the page they are about to open announce itself — four entries the
    checksum disputes is a different page from none, and knowing which before
    arriving is the difference between reading and hunting.
    """
    entries = Counter()
    for r in conn.execute(
        "SELECT page_index, COUNT(*) n FROM source_regions "
        "WHERE document_id = ? AND context = 'tight' GROUP BY page_index",
        (document_id,),
    ):
        entries[r["page_index"]] = r["n"]

    marks = ",".join("?" * len(TRANSCRIPTION_FINDINGS))
    flagged = Counter()
    for r in conn.execute(
        f"SELECT page_index, COUNT(DISTINCT band_label || ':' || entry_index) n "
        f"FROM validation_findings WHERE document_id = ? AND entry_index IS NOT NULL "
        f"AND kind IN ({marks}) GROUP BY page_index",
        (document_id, *TRANSCRIPTION_FINDINGS),
    ):
        flagged[r["page_index"]] = r["n"]

    reviewed = Counter()
    for r in conn.execute(
        "SELECT page_index, COUNT(*) n FROM human_corrections "
        "WHERE document_id = ? GROUP BY page_index",
        (document_id,),
    ):
        reviewed[r["page_index"]] = r["n"]

    return [
        {
            "page": page,
            "entries": entries[page],
            "flagged": flagged.get(page, 0),
            "reviewed": reviewed.get(page, 0),
        }
        for page in sorted(entries)
    ]


def page_entries(
    conn, document_id: str, page_index: int, tag: str | None = None
) -> list[ReviewEntry]:
    """Everything the reviewer needs for one page, in reading order."""
    rows = conn.execute(
        """SELECT b.band_index AS band_index, pe.entry_index AS entry_index,
                  sr.crop_path AS crop_path, sr.crop_id AS crop_id,
                  sr.normalized_bbox_json AS bbox, sr.role AS role
           FROM source_regions sr
           JOIN physical_entries pe ON pe.id = sr.entry_id
           JOIN bands b ON b.id = pe.band_id
           WHERE sr.document_id = ? AND sr.page_index = ? AND sr.context = 'tight'
           ORDER BY b.band_index, pe.entry_index""",
        (document_id, page_index),
    ).fetchall()

    # Latest machine answer per crop, optionally pinned to one configuration.
    ocr: dict[str, tuple[str | None, str]] = {}
    q = """SELECT sr.crop_id AS crop_id, oc.transcription AS t,
                  m.backend AS backend, r.tag AS tag
           FROM ocr_candidates oc
           JOIN ocr_runs r ON r.id = oc.ocr_run_id
           JOIN models m ON m.id = r.model_id
           JOIN source_regions sr ON sr.id = oc.source_region_id
           WHERE sr.document_id = ? AND sr.page_index = ?"""
    params: list[Any] = [document_id, page_index]
    if tag is not None:
        q += " AND r.tag = ?"
        params.append(tag)
    q += " ORDER BY oc.id"
    for r in conn.execute(q, params):
        ocr[r["crop_id"]] = (r["t"], r["backend"])

    corrections: dict[tuple[str, int], dict[str, Any]] = {}
    for r in conn.execute(
        "SELECT band_label, entry_index, transcription, unreadable, note "
        "FROM human_corrections WHERE document_id = ? AND page_index = ?",
        (document_id, page_index),
    ):
        corrections[(r["band_label"], r["entry_index"])] = dict(r)

    findings: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for r in conn.execute(
        "SELECT band_label, entry_index, kind, expected, observed "
        "FROM validation_findings WHERE document_id = ? AND page_index = ?",
        (document_id, page_index),
    ):
        findings.setdefault((r["band_label"], r["entry_index"]), []).append(dict(r))

    out: list[ReviewEntry] = []
    for row in rows:
        label = BAND_LABELS.get(row["band_index"], str(row["band_index"]))
        key = (label, row["entry_index"])
        machine, backend = ocr.get(row["crop_id"], (None, None))
        corr = corrections.get(key)
        mine = findings.get(key, [])
        try:
            bbox = json.loads(row["bbox"]) if row["bbox"] else None
        except (TypeError, ValueError):
            bbox = None

        human = (corr or {}).get("transcription")
        fields = _split_fields(human if human is not None else machine, label)
        out.append(ReviewEntry(
            page_index=page_index,
            band_index=row["band_index"],
            band_label=label,
            entry_index=row["entry_index"],
            crop_path=row["crop_path"],
            bbox=bbox,
            role=row["role"] or "entry",
            machine=machine,
            machine_backend=backend,
            human=human,
            unreadable=bool((corr or {}).get("unreadable", 0)),
            note=(corr or {}).get("note"),
            findings=mine,
            expected_own_id=_expected(mine, label),
            flagged=any(f["kind"] in TRANSCRIPTION_FINDINGS for f in mine),
            **fields,
        ))
    return out


def _split_fields(text: str | None, band_label: str) -> dict[str, str | None]:
    """The three printed fields of one entry.

    `trust_band` is on because the band a crop was cut from is strong evidence
    about which generation it belongs to, and here the reviewer can see
    immediately whether that guess was wrong. It is off in the genealogy path
    for the opposite reason: nobody is looking.
    """
    from familyocr.ocr.fields import parent_label, parse_entry

    labels = {"own_label": band_label, "parent_label": parent_label(band_label)}
    if not text:
        return {"own_id": None, "parent": None, "birth_order": None,
                "leftover": None, **labels}
    parsed = parse_entry(text, own_label=band_label, trust_band=True)
    return {
        "own_id": parsed.own_id,
        "parent": parsed.parent_id or parsed.parent_name,
        "birth_order": parsed.order,
        "leftover": parsed.leftover or None,
        **labels,
    }


def _expected(findings: list[dict[str, Any]], band_label: str) -> str | None:
    """The id the sequence run requires here, ready to accept in one keystroke.

    `validation_findings.expected` holds the numeral without its generation
    label, because the checker works within a band. The reviewer needs the whole
    id, so the label goes back on.
    """
    for f in findings:
        if f["kind"] in ("gap", "non_monotonic") and f.get("expected"):
            return f"{band_label}{f['expected']}"
    return None


def page_image(conn, document_id: str, page_index: int,
               project) -> PageImage:
    """The normalized page the boxes were cut from, with its pixel size.

    The reviewer verifies segmentation and text at once, so the boxes must be
    drawn on the same image the coordinates refer to — the normalized page, not
    the original scan.
    """
    from PIL import Image

    path = project.pages_dir(document_id, "normalized") / f"p{page_index:04d}.png"
    row = conn.execute(
        "SELECT status FROM transforms WHERE document_id = ? AND page_index = ? "
        "ORDER BY rowid DESC LIMIT 1",
        (document_id, page_index),
    ).fetchone()
    if not path.exists():
        return PageImage(page_index, None, 0, 0, row["status"] if row else None)
    with Image.open(path) as im:
        w, h = im.size
    return PageImage(page_index, str(path), w, h, row["status"] if row else None)


def save_correction(
    conn,
    document_id: str,
    page_index: int,
    band_label: str,
    entry_index: int,
    transcription: str | None,
    unreadable: bool = False,
    reviewer: str = "local",
    note: str | None = None,
) -> None:
    """Record a human transcription without touching the machine's."""
    conn.execute(
        """INSERT INTO human_corrections
           (document_id, page_index, band_label, entry_index, role,
            transcription, unreadable, corrected_by, corrected_at, note)
           VALUES (?,?,?,?, 'entry', ?,?,?,?,?)
           ON CONFLICT(document_id, page_index, band_label, entry_index, role)
           DO UPDATE SET transcription=excluded.transcription,
                         unreadable=excluded.unreadable,
                         corrected_by=excluded.corrected_by,
                         corrected_at=excluded.corrected_at,
                         note=excluded.note""",
        (document_id, page_index, band_label, entry_index,
         transcription, 1 if unreadable else 0, reviewer,
         datetime.now(timezone.utc).isoformat(), note),
    )
    conn.commit()


def progress(conn, document_id: str) -> dict[str, int]:
    total = conn.execute(
        "SELECT COUNT(*) n FROM source_regions "
        "WHERE document_id = ? AND context = 'tight'",
        (document_id,),
    ).fetchone()["n"]
    done = conn.execute(
        "SELECT COUNT(*) n FROM human_corrections WHERE document_id = ?",
        (document_id,),
    ).fetchone()["n"]
    return {"entries": total, "reviewed": done}


def export_document(conn, document_id: str, path: Path, tag: str | None = None) -> dict:
    """Write the whole document's transcription, page by page, in reading order.

    This is what the work is for, so it exports everything rather than only the
    entries a person touched: the machine's reading where it was accepted, the
    reviewer's where they changed it, and `source` saying which. An entry marked
    unreadable is written with an empty transcription rather than omitted —
    knowing a column defeated a careful reader is worth more than a silent gap,
    and omitting it would make the file's row count disagree with the book's.
    """
    rows: list[str] = []
    counts = {"entries": 0, "human": 0, "machine": 0, "unreadable": 0, "blank": 0}

    for page in reviewable_pages(conn, document_id):
        for e in page_entries(conn, document_id, page, tag):
            if e.role != "entry":
                continue
            counts["entries"] += 1
            if e.unreadable:
                text, source = "", "unreadable"
                counts["unreadable"] += 1
            elif e.human is not None:
                text, source = e.human, "human"
                counts["human"] += 1
            elif e.machine:
                text, source = e.machine, "machine"
                counts["machine"] += 1
            else:
                text, source = "", "none"
                counts["blank"] += 1
            flat = text.replace("\t", " ").replace("\n", " ").strip()
            rows.append(
                "\t".join([
                    str(e.page_index), e.band_label, str(e.entry_index),
                    e.own_id or "", e.parent or "", e.birth_order or "",
                    flat, source,
                ])
            )

    header = [
        f"# {document_id} — transcription export",
        f"# {counts['entries']} entries: {counts['human']} confirmed by hand, "
        f"{counts['machine']} as recognized, {counts['unreadable']} unreadable, "
        f"{counts['blank']} with no reading",
        "# page\tband\tentry\town_id\tparent\tbirth_order\ttranscription\tsource",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([*header, *rows]) + "\n", encoding="utf-8")
    counts["path"] = str(path)
    return counts


def export_verified(conn, document_id: str, path: Path) -> int:
    """Write the human-verified transcriptions as gold TSV."""
    rows = conn.execute(
        "SELECT page_index, band_label, entry_index, transcription "
        "FROM human_corrections "
        "WHERE document_id = ? AND unreadable = 0 AND transcription IS NOT NULL "
        "ORDER BY page_index, band_label, entry_index",
        (document_id,),
    ).fetchall()
    lines = [
        "# Human-verified entries exported from the review app.",
        "# page\tband\tentry\ttranscription",
    ]
    lines.extend(
        f"{r['page_index']}\t{r['band_label']}\t{r['entry_index']}\t"
        f"{r['transcription']}"
        for r in rows
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(rows)
