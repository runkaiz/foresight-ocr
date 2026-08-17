"""002 — promote crops to regions.

Until now the smallest addressable thing was a crop: a `source_regions` row
naming a PNG that `segment` cut. Three of them describe one printed column,
because tight/medium/full are widths of the same crop, and `segment` deleted and
reinserted the lot on every run — taking every OCR answer with them through
`ON DELETE CASCADE`.

This migration derives the editable document from what is already there. One
`regions` row per printed column, taken from the tight crop because that is the
row that carries the column's own extent; every `source_regions` row of every
width then points at the region it renders, and each existing crop file is
registered under a content address so that a later re-segment of unchanged
geometry can recognise its own output instead of redoing it.

Nothing is deleted and nothing is recomputed from pixels: the 34,776 rows this
reads are the same rows the OCR benchmark was scored against, which makes them
a usable oracle for everything built on top.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


def apply(conn: sqlite3.Connection) -> None:
    if not _has_table(conn, "source_regions"):
        return

    _add_region_link(conn)
    created = _create_regions(conn)
    if created:
        _fill_labels_and_hashes(conn)
    _link_source_regions(conn)
    checksums = _register_normalized_pages(conn)
    _register_existing_crops(conn, checksums)


# --------------------------------------------------------------------------
# schema touch-ups CREATE TABLE IF NOT EXISTS cannot express


def _add_region_link(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(source_regions)")}
    if "region_id" not in columns:
        conn.execute(
            "ALTER TABLE source_regions ADD COLUMN region_id "
            "INTEGER REFERENCES regions(id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_source_regions_region "
            "ON source_regions(region_id)"
        )
    # The lattice coordinates are how a machine proposal finds the region it
    # last produced, both here and in every later re-segment.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_region_lattice "
        "ON regions(document_id, page_index, band_ordinal, entry_index)"
    )


# --------------------------------------------------------------------------
# the backfill


def _create_regions(conn: sqlite3.Connection) -> int:
    """One region per printed column, from the tight crops.

    Tight is the width that describes the column itself; medium and full add
    padding measured in page pitch, so their boxes say as much about their
    neighbours as about the entry. Taking geometry from them would bake a
    benchmark parameter into the document.
    """
    if conn.execute("SELECT 1 FROM regions LIMIT 1").fetchone():
        return 0

    now = _now()
    cur = conn.execute(
        """
        INSERT INTO regions (
            region_uid, document_id, page_index, bbox_json, geometry_hash,
            transform_id, orig_quad_json, band_ordinal, band_label,
            reading_order, entry_index, role, state, created_by,
            origin_run_id, created_at, updated_at)
        SELECT lower(hex(randomblob(16))),      -- same shape as uuid4().hex
               sr.document_id, sr.page_index,
               sr.normalized_bbox_json,
               '',                              -- geometry_hash: Python pass below
               sr.transform_id,
               sr.bbox_json,                    -- the quad back to source pixels
               b.band_index,
               NULL,                            -- band_label: needs the profile
               pe.entry_index,                  -- reading order starts as the lattice
               pe.entry_index,
               sr.role, 'proposed', 'machine',
               pl.run_id, ?, ?
          FROM source_regions sr
          JOIN physical_entries pe ON pe.id = sr.entry_id
          JOIN bands b             ON b.id  = pe.band_id
          JOIN page_layouts pl     ON pl.id = b.page_layout_id
         WHERE sr.context = 'tight'
         ORDER BY sr.document_id, sr.page_index, b.band_index, pe.entry_index
        """,
        (now, now),
    )
    return int(cur.rowcount or 0)


def _fill_labels_and_hashes(conn: sqlite3.Connection) -> None:
    """Attach the generation label and the geometry content address.

    Neither is expressible in the INSERT above: the label lives in a YAML
    profile, and the hash has to agree exactly with the one Python computes when
    a reviewer moves the box, or every region would look changed on first edit.
    """
    from ...document.profile import load_profile
    from ...regions.model import geometry_hash

    configs = _configs_dir(conn)
    documents = [
        row[0] for row in conn.execute("SELECT DISTINCT document_id FROM regions")
    ]

    for document_id in documents:
        labels = load_profile(configs, document_id).band_map()
        updates = [
            (
                labels.get(row["band_ordinal"]),
                geometry_hash(
                    row["document_id"], row["page_index"], json.loads(row["bbox_json"])
                ),
                row["id"],
            )
            for row in conn.execute(
                "SELECT id, document_id, page_index, bbox_json, band_ordinal "
                "FROM regions WHERE document_id = ?",
                (document_id,),
            )
        ]
        conn.executemany(
            "UPDATE regions SET band_label = ?, geometry_hash = ? WHERE id = ?",
            updates,
        )


def _link_source_regions(conn: sqlite3.Connection) -> None:
    """Point every crop row, of every width, at the region it renders."""
    conn.execute(
        """
        UPDATE source_regions SET region_id = (
            SELECT r.id
              FROM regions r
              JOIN physical_entries pe ON pe.id = source_regions.entry_id
              JOIN bands b             ON b.id  = pe.band_id
             WHERE r.document_id  = source_regions.document_id
               AND r.page_index   = source_regions.page_index
               AND r.band_ordinal = b.band_index
               AND r.entry_index  = pe.entry_index
             LIMIT 1)
         WHERE region_id IS NULL
        """
    )


# --------------------------------------------------------------------------
# content addresses for what is already on disk


def _register_normalized_pages(conn: sqlite3.Connection) -> dict[tuple[str, int], str]:
    """Record the normalized pages in `page_assets`, with checksums.

    `normalize` writes these PNGs and registers nothing, so the only page assets
    on record are the original scan and its decode. Without a checksum for the
    page a crop was cut from, a re-warped page silently reuses crops of the old
    geometry — the cache cannot tell "the page moved" from "nothing happened".
    """
    checksums = {
        (row["document_id"], row["page_index"]): row["checksum"]
        for row in conn.execute(
            "SELECT document_id, page_index, checksum FROM page_assets "
            "WHERE role = 'normalized'"
        )
    }

    pages = _pages_root(conn)
    wanted = [
        (row["document_id"], row["page_index"])
        for row in conn.execute(
            "SELECT document_id, page_index FROM pages ORDER BY document_id, page_index"
        )
        if (row["document_id"], row["page_index"]) not in checksums
    ]
    if not wanted:
        return checksums

    from ...provenance import sha256_file

    now = _now()
    todo = [
        (doc, page, pages / doc / "normalized" / f"p{page:04d}.png")
        for doc, page in wanted
    ]
    todo = [(doc, page, path) for doc, page, path in todo if path.exists()]
    if todo:
        print(
            f"familyocr: checksumming {len(todo)} normalized pages "
            "(one-off, so crops can be cached by content)",
            file=sys.stderr,
        )

    rows = []
    for doc, page, path in todo:
        digest = sha256_file(path)
        checksums[(doc, page)] = digest
        rows.append((doc, page, str(path), digest, now))

    conn.executemany(
        "INSERT INTO page_assets (document_id, page_index, role, path, checksum, created_at) "
        "VALUES (?, ?, 'normalized', ?, ?, ?) "
        "ON CONFLICT(document_id, page_index, role) DO UPDATE SET "
        "path = excluded.path, checksum = excluded.checksum",
        rows,
    )
    return checksums


def _register_existing_crops(
    conn: sqlite3.Connection, checksums: dict[tuple[str, int], str]
) -> None:
    """Give every crop already on disk a content address.

    One row per variant actually present, not per `source_regions.crop_path`:
    that column records only the first variant a segmentation run wrote, while
    the recognizers were benchmarked across several. Keying the answers back to
    their pixels needs the variant that produced them.

    The legacy filenames stay exactly where they are — 3.5 GB of them, named
    positionally — because `region_crops.path` is what anything reads. Only the
    key is new, and it is what lets an unchanged region be recognised rather
    than re-cut. The PNG bytes are not hashed: they were not verified when they
    were written, and claiming otherwise here would be inventing provenance.
    """
    if conn.execute("SELECT 1 FROM region_crops LIMIT 1").fetchone():
        return

    from ...ocr.cache import crop_key
    from ...segmentation.entries import CONTEXTS

    crops_root = _artifacts_dir(conn) / "crops"
    variants = {
        document_id: sorted(p.name for p in (crops_root / document_id).iterdir() if p.is_dir())
        for document_id in (
            row[0] for row in conn.execute("SELECT DISTINCT document_id FROM regions")
        )
        if (crops_root / document_id).is_dir()
    }

    now = _now()
    rows = []
    for row in conn.execute(
        "SELECT region_id, document_id, page_index, context, crop_id, "
        "       normalized_bbox_json "
        "FROM source_regions "
        "WHERE region_id IS NOT NULL AND crop_id IS NOT NULL"
    ):
        document_id = row["document_id"]
        page_checksum = checksums.get((document_id, row["page_index"]))
        if not page_checksum:
            continue

        bbox = json.loads(row["normalized_bbox_json"])
        # The slice `src[int(y0):int(y1), int(x0):int(x1)]` is what produced the
        # file, so the integer box is what identifies its pixels.
        pixel_bbox = [int(v) for v in bbox]
        geometry = _geometry_hash_of(document_id, row["page_index"], bbox)

        for variant in variants.get(document_id, ()):
            path = crops_root / document_id / variant / f"{row['crop_id']}.png"
            if not path.exists():
                continue
            rows.append(
                (
                    row["region_id"],
                    geometry,
                    row["context"],
                    CONTEXTS.get(row["context"], 0.0),
                    variant,
                    json.dumps(pixel_bbox),
                    crop_key(
                        document_id, row["page_index"], pixel_bbox, variant, page_checksum
                    ),
                    str(path),
                    now,
                )
            )

    conn.executemany(
        "INSERT OR IGNORE INTO region_crops "
        "(region_id, geometry_hash, context, pad_frac, variant, pixel_bbox_json, "
        " crop_key, path, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        rows,
    )


# --------------------------------------------------------------------------
# helpers


def _geometry_hash_of(document_id: str, page_index: int, bbox: list[float]) -> str:
    from ...regions.model import geometry_hash

    return geometry_hash(document_id, page_index, bbox)


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _artifacts_dir(conn: sqlite3.Connection) -> Path:
    """Where this database lives, and therefore where its artifacts live."""
    for _, name, path in conn.execute("PRAGMA database_list"):
        if name == "main" and path:
            return Path(path).resolve().parent
    raise RuntimeError("cannot locate the artifacts directory from the connection")


def _pages_root(conn: sqlite3.Connection) -> Path:
    return _artifacts_dir(conn) / "pages"


def _configs_dir(conn: sqlite3.Connection) -> Path:
    return _artifacts_dir(conn).parent / "configs"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
