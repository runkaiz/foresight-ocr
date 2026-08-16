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
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

def _bands() -> dict[int, str]:
    from familyocr.context import band_labels

    return band_labels()


class _BandLabels(dict):
    """Band index -> label, resolved against the active document profile."""

    def get(self, key, default=None):
        return _bands().get(key, default)

    def items(self):
        return _bands().items()

    def __getitem__(self, key):
        return _bands()[key]

    def __contains__(self, key):
        return key in _bands()


BAND_LABELS = _BandLabels()


@dataclass
class PageImage:
    page_index: int
    path: str | None
    width: int
    height: int
    frame_status: str | None


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
        try:
            bbox = json.loads(row["bbox"]) if row["bbox"] else None
        except (TypeError, ValueError):
            bbox = None
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
            human=(corr or {}).get("transcription"),
            unreadable=bool((corr or {}).get("unreadable", 0)),
            note=(corr or {}).get("note"),
            findings=findings.get(key, []),
        ))
    return out


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
