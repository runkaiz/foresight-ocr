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

The page is read from `regions`, not from the crop rows a segmentation pass
happened to write. Those describe where a crop was cut; the region is the thing
a person edits, and once the editor can move it the two stop agreeing. Reading
through the crop row is how a re-cut page would keep showing the boxes and the
text it had before the re-cut — present, wrong, and silent about it.

Which reading belongs to a region is decided by pixels: the answer preferred is
the one produced from a crop whose geometry is the region's *current* geometry.
Anything else is a reading of pixels that no longer exist, kept as a fallback so
a page whose crops predate the region table still shows what is known about it.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2

from foresight_ocr.context import BAND_LABELS
from foresight_ocr.export_order import entry_sort_key
from foresight_ocr.imaging.variants import build_variant
from foresight_ocr.ocr.watermarks import filter_watermark_text
from foresight_ocr.provenance import sha256_bytes
from foresight_ocr.regions.crops import CropUnavailable, normalized_page


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
    "gap",
    "non_monotonic",
    "duplicate",
    "unparsed",
    "band_mismatch",
    "duplicate_id",
)


@dataclass
class ReviewEntry:
    page_index: int
    band_index: int
    band_label: str
    entry_index: int
    crop_path: str | None
    bbox: list[float] | None  # canonical page coordinates
    crop_bbox: list[int] | None  # current-page pixels actually present in crop
    role: str
    machine: str | None
    machine_backend: str | None
    human: str | None
    unreadable: bool
    note: str | None
    findings: list[dict[str, Any]]
    # The printed fields, parsed from whichever transcription is current.
    # The reviewer edits these rather than the whole line: an id is six or seven
    # characters of which one is usually wrong, and finding that one inside a
    # single text box is the slow part.
    own_id: str | None = None
    parent: str | None = None  # father's id, or his name where printed
    birth_order: str | None = None
    # Legacy combined value retained for older reviewer clients. The current
    # UI edits ``parent`` and ``birth_order`` separately (允一 + 長子).
    parent_order: str | None = None
    # Free-form text printed beside the core fields, usually a birthday on the
    # later pages. It is parsed conservatively and never normalized as a date.
    additional_info: str | None = None
    # The generation character each id field carries, so the reviewer can type
    # digits and get the printed form back.
    own_label: str | None = None
    parent_label: str | None = None
    leftover: str | None = None  # what parsing could not account for
    # What the sequence checksum says this entry's id must be. Present only for
    # a break in the run, which is exactly when the reviewer needs it.
    expected_own_id: str | None = None
    flagged: bool = False
    # The region this row is, so the browser can address it for a geometry edit
    # without going back through its position on the page.
    region_uid: str | None = None
    state: str = "proposed"
    stale_reading: bool = False  # the text was read from pixels since re-cut
    header_kind: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def latest_ocr_tag(conn, document_id: str) -> str | None:
    """Which benchmark configuration pre-fills the page.

    Interactive re-reads are excluded. They are the newest answers in the
    database the moment anyone edits a region, and taking one as the document's
    configuration would pin the whole book to a tag that exists on a handful of
    columns — every other page would then show nothing at all.
    """
    row = conn.execute(
        """SELECT r.tag FROM ocr_candidates oc
           JOIN ocr_runs r ON r.id = oc.ocr_run_id
           JOIN regions g ON g.id = oc.region_id
           WHERE g.document_id = ? AND r.tag IS NOT 'interactive'
           ORDER BY oc.id DESC LIMIT 1""",
        (document_id,),
    ).fetchone()
    return row["tag"] if row else None


def page_is_ignored(conn, document_id: str, page_index: int) -> bool:
    """Whether a source page is excluded from review work and exports.

    A few old/test databases contain editable regions without a corresponding
    ``pages`` row.  They predate page-level review state, so absence means active
    rather than silently hiding their data.
    """
    row = conn.execute(
        "SELECT ignored FROM pages WHERE document_id = ? AND page_index = ?",
        (document_id, page_index),
    ).fetchone()
    return bool(row["ignored"]) if row is not None else False


def set_page_ignored(conn, document_id: str, page_index: int, ignored: bool) -> bool:
    """Persist a page's soft exclusion state and return the stored value."""
    row = conn.execute(
        "SELECT 1 FROM pages WHERE document_id = ? AND page_index = ?",
        (document_id, page_index),
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown page {page_index} for document {document_id!r}")
    state = bool(ignored)
    conn.execute(
        "UPDATE pages SET ignored = ? WHERE document_id = ? AND page_index = ?",
        (1 if state else 0, document_id, page_index),
    )
    conn.commit()
    return state


def reviewable_pages(
    conn, document_id: str, include_ignored: bool = False
) -> list[int]:
    # Pages, not regions, are the navigation model: a cover can legitimately
    # have no detected text and must still be reachable to ignore or restore.
    # The UNION preserves old/test data whose regions predate their page row.
    ignored_clause = "" if include_ignored else "WHERE COALESCE(p.ignored, 0) = 0 "
    # ``ignored_clause`` is one of the two local literals selected above.
    return [
        r["page_index"]
        for r in conn.execute(
            "WITH candidates(page_index) AS ("  # nosec B608
            "  SELECT page_index FROM pages WHERE document_id = ? "
            "  UNION "
            "  SELECT page_index FROM regions WHERE document_id = ? "
            "  AND deleted_at IS NULL AND state != 'rejected'"
            ") "
            "SELECT c.page_index FROM candidates c "
            "LEFT JOIN pages p ON p.document_id = ? "
            "AND p.page_index = c.page_index "
            + ignored_clause
            + "ORDER BY c.page_index",
            (document_id, document_id, document_id),
        )
    ]


def page_summary(conn, document_id: str) -> list[dict[str, Any]]:
    """Per page: how much is there, how much is suspect, how much is confirmed.

    The reviewer works front to back, so this is not a worklist. It is what
    lets the page they are about to open announce itself — four entries the
    checksum disputes is a different page from none, and knowing which before
    arriving is the difference between reading and hunting.
    """
    pages = reviewable_pages(conn, document_id, include_ignored=True)
    ignored = {
        r["page_index"]: bool(r["ignored"])
        for r in conn.execute(
            "SELECT page_index, ignored FROM pages WHERE document_id = ?",
            (document_id,),
        )
    }

    entries: Counter[int] = Counter()
    for r in conn.execute(
        "SELECT page_index, COUNT(*) n FROM regions "
        "WHERE document_id = ? AND deleted_at IS NULL AND state != 'rejected' "
        "AND role = 'entry' "
        "GROUP BY page_index",
        (document_id,),
    ):
        entries[r["page_index"]] = r["n"]

    marks = ",".join("?" * len(TRANSCRIPTION_FINDINGS))
    flagged: Counter[int] = Counter()
    # ``marks`` contains placeholders generated from a module constant.
    for r in conn.execute(
        f"SELECT page_index, COUNT(DISTINCT band_label || ':' || entry_index) n "
        f"FROM validation_findings WHERE document_id = ? AND entry_index IS NOT NULL "
        f"AND kind IN ({marks}) GROUP BY page_index",  # nosec B608
        (document_id, *TRANSCRIPTION_FINDINGS),
    ):
        flagged[r["page_index"]] = r["n"]

    reviewed: Counter[int] = Counter()
    for r in conn.execute(
        "SELECT page_index, COUNT(*) n FROM human_corrections "
        "WHERE document_id = ? AND role = 'entry' GROUP BY page_index",
        (document_id,),
    ):
        reviewed[r["page_index"]] = r["n"]

    return [
        {
            "page": page,
            "entries": entries[page],
            "flagged": flagged.get(page, 0),
            "reviewed": reviewed.get(page, 0),
            "ignored": ignored.get(page, False),
        }
        for page in pages
    ]


def page_entries(
    conn, document_id: str, page_index: int, tag: str | None = None
) -> list[ReviewEntry]:
    """Everything the reviewer needs for one page, in reading order."""
    rows = conn.execute(
        """SELECT id, region_uid, band_label, band_ordinal, reading_order, role, state,
                  bbox_json, geometry_hash
           FROM regions
           WHERE document_id = ? AND page_index = ? AND deleted_at IS NULL
             AND state != 'rejected'
           ORDER BY band_ordinal,
                    CASE WHEN role = 'entry' THEN 1 ELSE 0 END,
                    reading_order""",
        (document_id, page_index),
    ).fetchall()

    ocr = _readings(conn, page_index, [r["id"] for r in rows], tag)
    crops = _crops(conn, [r["id"] for r in rows])

    corrections: dict[tuple[str, int, str], dict[str, Any]] = {}
    for r in conn.execute(
        "SELECT band_label, entry_index, role, transcription, unreadable, note "
        "FROM human_corrections WHERE document_id = ? AND page_index = ?",
        (document_id, page_index),
    ):
        corrections[(r["band_label"], r["entry_index"], r["role"])] = dict(r)

    findings: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for r in conn.execute(
        "SELECT band_label, entry_index, kind, expected, observed "
        "FROM validation_findings WHERE document_id = ? AND page_index = ?",
        (document_id, page_index),
    ):
        findings.setdefault((r["band_label"], r["entry_index"]), []).append(dict(r))

    out: list[ReviewEntry] = []
    for row in rows:
        band_index = row["band_ordinal"]
        label = row["band_label"] or BAND_LABELS.get(band_index, str(band_index))
        key = (label, row["reading_order"])
        reading = ocr.get(row["id"])
        machine = reading.text if reading else None
        backend = reading.backend if reading else None
        role = row["role"] or "entry"
        corr = corrections.get((*key, role))
        mine = findings.get(key, []) if role == "entry" else []
        try:
            bbox = json.loads(row["bbox_json"]) if row["bbox_json"] else None
        except (TypeError, ValueError):
            bbox = None

        # Before explicit-empty corrections were preserved by the HTTP layer, a
        # reviewer clearing all fields produced a row with NULL transcription
        # and unreadable=0.  Row presence is still a human decision; only absence
        # means "fall back to the machine".  Interpreting those legacy rows as an
        # empty string repairs existing books without rewriting their history.
        human = (
            ""
            if corr is not None
            and corr.get("transcription") is None
            and not corr.get("unreadable")
            else (corr or {}).get("transcription")
        )
        current = human if human is not None else machine
        fields = (
            _split_fields(current, label)
            if role == "entry"
            else {
                "own_id": current,
                "parent": None,
                "birth_order": None,
                "parent_order": None,
                "additional_info": None,
                "leftover": None,
                "own_label": None,
                "parent_label": None,
            }
        )
        out.append(
            ReviewEntry(
                page_index=page_index,
                band_index=band_index,
                band_label=label,
                entry_index=row["reading_order"],
                crop_path=(crops[row["id"]][0] if row["id"] in crops else None),
                bbox=bbox,
                crop_bbox=(crops[row["id"]][1] if row["id"] in crops else None),
                role=role,
                machine=machine,
                machine_backend=backend,
                human=human,
                unreadable=bool((corr or {}).get("unreadable", 0)),
                note=(corr or {}).get("note"),
                findings=mine,
                expected_own_id=_expected(mine, label) if role == "entry" else None,
                flagged=any(f["kind"] in TRANSCRIPTION_FINDINGS for f in mine),
                region_uid=row["region_uid"],
                state=row["state"],
                stale_reading=bool(reading and not reading.current),
                header_kind=_header_kind(current) if role != "entry" else None,
                **fields,
            )
        )
    return out


@dataclass(frozen=True)
class _Reading:
    text: str | None
    backend: str | None
    current: bool  # read from the pixels this region names now


def _readings(
    conn, page_index: int, region_ids: list[int], tag: str | None
) -> dict[int, _Reading]:
    """The best answer on record for each region, and whether it is still true.

    An answer is current when the crop it came from was cut from the region's
    present geometry. After a re-cut the old answers are still on record — they
    describe pixels that were really there — but showing one as this column's
    reading would be a claim about a column that has moved.
    """
    if not region_ids:
        return {}
    marks = ",".join("?" * len(region_ids))
    # ``marks`` contains question-mark placeholders, never region identifiers.
    q = f"""SELECT oc.region_id AS region_id, oc.transcription AS t,
                   m.backend AS backend, r.tag AS tag,
                   (rc.geometry_hash = g.geometry_hash) AS current
            FROM ocr_candidates oc
            JOIN regions g ON g.id = oc.region_id
            JOIN ocr_runs r ON r.id = oc.ocr_run_id
            JOIN models m ON m.id = r.model_id
            LEFT JOIN region_crops rc ON rc.crop_key = oc.crop_key
            WHERE oc.region_id IN ({marks})"""  # nosec B608
    params: list[Any] = list(region_ids)
    if tag is not None:
        # The tag pins which benchmark configuration pre-fills the page, but an
        # interactive re-read is not a configuration — it is this region's
        # current reading, and excluding it would hide the result of the edit
        # that produced it.
        q += " AND (r.tag = ? OR r.tag = 'interactive')"
        params.append(tag)
    q += " ORDER BY oc.id"

    out: dict[int, _Reading] = {}
    for r in conn.execute(q, params):
        reading = _Reading(
            filter_watermark_text(r["t"]).transcription,
            r["backend"],
            bool(r["current"]),
        )
        kept = out.get(r["region_id"])
        # Later answers win, except that an answer about the pixels as they are
        # now is never displaced by an older one about pixels that are gone.
        if kept is None or reading.current or not kept.current:
            out[r["region_id"]] = reading
    return out


def _crops(conn, region_ids: list[int]) -> dict[int, tuple[str, list[int] | None]]:
    """Crop path and effective current-page box, preferring current geometry."""
    if not region_ids:
        return {}
    marks = ",".join("?" * len(region_ids))
    out: dict[int, tuple[str, list[int] | None]] = {}
    # ``marks`` contains question-mark placeholders, never region identifiers.
    for r in conn.execute(
        f"""SELECT rc.region_id AS region_id, rc.path AS path,
                   rc.pixel_bbox_json AS pixel_bbox_json,
                   (rc.geometry_hash = g.geometry_hash) AS current
            FROM region_crops rc JOIN regions g ON g.id = rc.region_id
            WHERE rc.region_id IN ({marks}) AND rc.context = 'tight'
            ORDER BY rc.id""",  # nosec B608
        region_ids,
    ):
        if r["current"] or r["region_id"] not in out:
            try:
                pixel_bbox = [int(v) for v in json.loads(r["pixel_bbox_json"])]
            except (TypeError, ValueError):
                pixel_bbox = None
            out[r["region_id"]] = (r["path"], pixel_bbox)
    return out


def _split_fields(text: str | None, band_label: str) -> dict[str, str | None]:
    """The core printed fields and optional free-form information of one entry.

    `trust_band` is on because the band a crop was cut from is strong evidence
    about which generation it belongs to, and here the reviewer can see
    immediately whether that guess was wrong. It is off in the genealogy path
    for the opposite reason: nobody is looking.
    """
    from foresight_ocr.ocr.fields import parent_label, parse_entry

    labels = {"own_label": band_label, "parent_label": parent_label(band_label)}
    if not text:
        return {
            "own_id": None,
            "parent": None,
            "birth_order": None,
            "parent_order": None,
            "additional_info": None,
            "leftover": None,
            **labels,
        }
    parsed = parse_entry(text, own_label=band_label, trust_band=True)
    parent = parsed.parent_id or parsed.parent_name
    additional_info = parsed.leftover or None
    return {
        "own_id": parsed.own_id,
        "parent": parent,
        "birth_order": parsed.order,
        "parent_order": (parent or "") + (parsed.order or "") or None,
        "additional_info": additional_info,
        # Kept for API compatibility with older reviewer clients. New clients
        # expose the same preserved text as the editable additional-info field.
        "leftover": additional_info,
        **labels,
    }


def _header_kind(text: str | None) -> str:
    """Classify header text for the reviewer without parsing it as a person."""
    compact = "".join((text or "").split())
    if "字第" in compact:
        return "section_title"
    if re.search(r"第[一二三四五六七八九十百千]+世", compact):
        return "generation_title"
    if "宗譜" in compact or "雁序圖" in compact:
        return "document_title"
    return "page_header"


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


def page_image(conn, document_id: str, page_index: int, project) -> PageImage:
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


def page_variant_image(
    conn, document_id: str, page_index: int, project, variant: str = "watermark"
) -> PageImage:
    """Materialize a non-destructive full-page image for review.

    Review uses the same ``watermark`` transformation as OCR crops, so what the
    reviewer sees when watermark suppression is enabled is the page the model
    is actually asked to read. The normalized page checksum is part of the
    filename: changing the source creates a new immutable URL instead of
    letting the browser show a cached rendering of old pixels.
    """
    if variant == "original":
        return page_image(conn, document_id, page_index, project)
    if variant != "watermark":
        raise ValueError(
            f"review page variant must be 'original' or 'watermark', got {variant!r}"
        )

    source_path, checksum = normalized_page(conn, project, document_id, page_index)
    actual_checksum = sha256_bytes(source_path.read_bytes())
    if checksum != actual_checksum:
        checksum = actual_checksum
        conn.execute(
            "UPDATE page_assets SET checksum = ? "
            "WHERE document_id = ? AND page_index = ? AND role = 'normalized'",
            (checksum, document_id, page_index),
        )
    out = (
        project.pages_dir(document_id, "review-watermark")
        / f"p{page_index:04d}_{checksum}.png"
    )
    if not out.exists():
        source = cv2.imread(str(source_path), cv2.IMREAD_COLOR)
        if source is None:
            raise CropUnavailable(
                f"cannot read {source_path} — it is missing or truncated. "
                "Re-run `foresight-ocr normalize` for this page."
            )
        rendered = build_variant(source, variant)
        out.parent.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(out), rendered):
            raise CropUnavailable(f"cannot write {out}")

    review_image = cv2.imread(str(out), cv2.IMREAD_UNCHANGED)
    if review_image is None:
        raise CropUnavailable(f"cannot read generated review image {out}")
    height, width = review_image.shape[:2]
    original = page_image(conn, document_id, page_index, project)
    return PageImage(page_index, str(out), width, height, original.frame_status)


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
    role: str = "entry",
) -> None:
    """Record a human transcription without touching the machine's."""
    conn.execute(
        """INSERT INTO human_corrections
           (document_id, page_index, band_label, entry_index, role,
            transcription, unreadable, corrected_by, corrected_at, note)
           VALUES (?,?,?,?, ?, ?,?,?,?,?)
           ON CONFLICT(document_id, page_index, band_label, entry_index, role)
           DO UPDATE SET transcription=excluded.transcription,
                         unreadable=excluded.unreadable,
                         corrected_by=excluded.corrected_by,
                         corrected_at=excluded.corrected_at,
                         note=excluded.note""",
        (
            document_id,
            page_index,
            band_label,
            entry_index,
            role,
            transcription,
            1 if unreadable else 0,
            reviewer,
            datetime.now(timezone.utc).isoformat(),
            note,
        ),
    )
    conn.commit()


def delete_correction(
    conn,
    document_id: str,
    page_index: int,
    band_label: str,
    entry_index: int,
    role: str = "entry",
) -> bool:
    """Remove one human decision, returning whether it existed.

    The table's stable tuple is unique, so the exact-key delete affects either
    one row or none.  Treating the missing-row case as success makes an
    unconfirm request safe to repeat without touching the machine reading.
    """
    cursor = conn.execute(
        "DELETE FROM human_corrections "
        "WHERE document_id = ? AND page_index = ? AND band_label = ? "
        "AND entry_index = ? AND role = ?",
        (document_id, page_index, band_label, entry_index, role),
    )
    conn.commit()
    return cursor.rowcount == 1


def progress(conn, document_id: str) -> dict[str, int]:
    total = conn.execute(
        "SELECT COUNT(*) n FROM regions r "
        "LEFT JOIN pages p ON p.document_id = r.document_id "
        "AND p.page_index = r.page_index "
        "WHERE r.document_id = ? AND r.deleted_at IS NULL "
        "AND r.state != 'rejected' AND r.role = 'entry' "
        "AND COALESCE(p.ignored, 0) = 0",
        (document_id,),
    ).fetchone()["n"]
    # Structural headings may be corrected too, but they are not people and
    # therefore do not advance the person-review progress bar.
    done = conn.execute(
        "SELECT COUNT(*) n FROM human_corrections h "
        "LEFT JOIN pages p ON p.document_id = h.document_id "
        "AND p.page_index = h.page_index "
        "WHERE h.document_id = ? AND h.role = 'entry' "
        "AND COALESCE(p.ignored, 0) = 0",
        (document_id,),
    ).fetchone()["n"]
    return {"entries": total, "reviewed": done}


def export_document(
    conn,
    document_id: str,
    path: Path,
    tag: str | None = None,
    *,
    generation_labels: Iterable[str] | None = None,
) -> dict:
    """Write the whole transcription generation by generation.

    This is what the work is for, so it exports everything rather than only the
    entries a person touched: the machine's reading where it was accepted, the
    reviewer's where they changed it, and `source` saying which. An entry marked
    unreadable is written with an empty transcription rather than omitted —
    knowing a column defeated a careful reader is worth more than a silent gap,
    and omitting it would make the file's row count disagree with the book's.
    """
    rows: list[str] = []
    counts: dict[str, Any] = {
        "entries": 0,
        "human": 0,
        "machine": 0,
        "unreadable": 0,
        "blank": 0,
    }
    labels = tuple(generation_labels) if generation_labels is not None else None

    entries = [
        entry
        for page in reviewable_pages(conn, document_id)
        for entry in page_entries(conn, document_id, page, tag)
        if entry.role == "entry"
    ]
    entries.sort(
        key=lambda entry: entry_sort_key(
            entry.band_label,
            None if entry.unreadable else entry.own_id,
            entry.page_index,
            entry.entry_index,
            entry.region_uid or "",
            labels=labels,
        )
    )

    for e in entries:
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
        additional = (
            (e.additional_info or "").replace("\t", " ").replace("\n", " ").strip()
        )
        rows.append(
            "\t".join(
                [
                    str(e.page_index),
                    e.band_label,
                    str(e.entry_index),
                    e.own_id or "",
                    e.parent or "",
                    e.birth_order or "",
                    additional,
                    flat,
                    source,
                ]
            )
        )

    header = [
        f"# {document_id} — transcription export",
        f"# {counts['entries']} entries: {counts['human']} confirmed by hand, "
        f"{counts['machine']} as recognized, {counts['unreadable']} unreadable, "
        f"{counts['blank']} with no reading",
        "# page\tband\tentry\town_id\tparent\tbirth_order\tadditional_info\t"
        "transcription\tsource",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([*header, *rows]) + "\n", encoding="utf-8")
    counts["path"] = str(path)
    return counts


def export_verified(
    conn,
    document_id: str,
    path: Path,
    *,
    generation_labels: Iterable[str] | None = None,
) -> int:
    """Write human-verified people with each printed field in its own column."""
    labels = tuple(generation_labels) if generation_labels is not None else None
    rows = list(
        conn.execute(
            "SELECT h.page_index, h.band_label, h.entry_index, h.transcription "
            "FROM human_corrections h "
            "LEFT JOIN pages p ON p.document_id = h.document_id "
            "AND p.page_index = h.page_index "
            "WHERE h.document_id = ? AND h.role = 'entry' AND h.unreadable = 0 "
            "AND h.transcription IS NOT NULL "
            "AND COALESCE(p.ignored, 0) = 0",
            (document_id,),
        ).fetchall()
    )

    def verified_sort_key(row):
        own_id = _split_fields(row["transcription"], row["band_label"])["own_id"]
        return entry_sort_key(
            row["band_label"],
            own_id,
            row["page_index"],
            row["entry_index"],
            labels=labels,
        )

    rows.sort(key=verified_sort_key)
    lines = [
        "# Human-verified entries exported from the review app.",
        "# page\tband\tentry\town_id\tparent\tbirth_order\tadditional_info",
    ]
    for row in rows:
        fields = _split_fields(row["transcription"], row["band_label"])
        values = [
            " ".join((fields[name] or "").split())
            for name in ("own_id", "parent", "birth_order", "additional_info")
        ]
        lines.append(
            f"{row['page_index']}\t{row['band_label']}\t"
            f"{row['entry_index']}\t" + "\t".join(values)
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(rows)
