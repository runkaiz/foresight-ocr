"""Review data layer.

The load-bearing test here is that a human correction survives reprocessing.
The spec requires it, the schema was designed for it (corrections key on the
stable document/page/band/entry tuple rather than on a candidate row), and until
now nothing exercised it.
"""

import json
import sqlite3

import pytest

from familyocr.persistence.db import init_schema
from familyocr.regions import store
from familyocr.regions.model import geometry_hash
from familyocr.review.data import (
    export_verified,
    page_entries,
    progress,
    save_correction,
)


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    conn.execute(
        "INSERT INTO documents (id, title, source_path, checksum, page_count, "
        "created_at) VALUES ('doc','t','p','c',1,'now')"
    )
    conn.execute(
        "INSERT INTO processing_runs (document_id, stage, params_json, "
        "params_hash, compute_backend, pipeline_version, git_commit, started_at) "
        "VALUES ('doc','segment','{}','h','local','0','x','now')"
    )
    return conn


def _segment(conn, crop_suffix="v1"):
    """Simulate a segmentation pass: layout, bands, regions and their crops.

    A pass re-cuts the crops and keeps the regions, which is what reconcile does
    and what the review path now reads: the region is the column, and the crop
    is one rendering of the pixels it currently names.
    """
    conn.execute("DELETE FROM page_layouts")
    cur = conn.execute(
        "INSERT INTO page_layouts (document_id, page_index) VALUES ('doc', 58)"
    )
    layout_id = cur.lastrowid
    conn.execute(
        "INSERT INTO bands (page_layout_id, band_index, label, bbox_json) "
        "VALUES (?,0,'庶','[]')", (layout_id,)
    )
    for entry in (0, 1):
        bbox = [1000.0 - 300 * entry, 0.0, 1300.0 - 300 * entry, 900.0]
        digest = geometry_hash("doc", 58, bbox)
        row = conn.execute(
            "SELECT id FROM regions WHERE document_id='doc' AND page_index=58 "
            "AND reading_order = ?", (entry,)
        ).fetchone()
        if row is None:
            region = store.create_region(
                conn, "doc", 58, bbox,
                band_label="庶", band_ordinal=0,
                reading_order=entry, entry_index=entry,
            )
            region_id = region.id
        else:
            region_id = row["id"]
        conn.execute(
            "INSERT INTO region_crops (region_id, geometry_hash, context, pad_frac, "
            "variant, pixel_bbox_json, crop_key, path, created_at) "
            "VALUES (?,?, 'tight', 0.0, 'maxrgb', ?, ?, ?, 'now')",
            (region_id, digest, json.dumps([int(v) for v in bbox]),
             f"crop_{crop_suffix}_{entry}", f"/crops/{crop_suffix}_{entry}.png"),
        )
    conn.commit()


def _add_ocr(conn, texts, tag="t"):
    conn.execute(
        "INSERT INTO models (id, name, version, backend) "
        "VALUES ('m','paddleocr_vl','1.6','paddleocr_vl') "
        "ON CONFLICT(id) DO NOTHING"
    )
    cur = conn.execute(
        "INSERT INTO ocr_runs (run_id, model_id, input_variant, tag) "
        "VALUES (1,'m','maxrgb',?)", (tag,)
    )
    run = cur.lastrowid
    # The newest crop of each region is what a pass would have read.
    crops = conn.execute(
        "SELECT region_id, crop_key, MAX(id) FROM region_crops GROUP BY region_id "
        "ORDER BY region_id"
    ).fetchall()
    for crop, text in zip(crops, texts):
        conn.execute(
            "INSERT INTO ocr_candidates (region_id, ocr_run_id, crop_key, "
            "cache_key, transcription) VALUES (?,?,?,?,?)",
            (crop["region_id"], run, crop["crop_key"],
             f"{crop['crop_key']}:{run}", text),
        )
    conn.commit()


def test_page_entries_pairs_machine_text_with_crops():
    conn = _db()
    _segment(conn)
    _add_ocr(conn, ["庶一長子", "庶二次子"])
    entries = page_entries(conn, "doc", 58)
    assert [e.entry_index for e in entries] == [0, 1]
    assert [e.machine for e in entries] == ["庶一長子", "庶二次子"]
    assert entries[0].band_label == "庶"
    assert entries[0].human is None


def test_correction_does_not_touch_the_machine_transcription():
    conn = _db()
    _segment(conn)
    _add_ocr(conn, ["庶一長子", "庶二次子"])
    save_correction(conn, "doc", 58, "庶", 0, "庶一次子")
    entries = page_entries(conn, "doc", 58)
    assert entries[0].human == "庶一次子"
    assert entries[0].machine == "庶一長子"   # untouched


def test_correction_survives_reprocessing():
    conn = _db()
    _segment(conn, crop_suffix="v1")
    _add_ocr(conn, ["庶一長子", "庶二次子"])
    save_correction(conn, "doc", 58, "庶", 0, "庶一次子")

    # Re-segment: the page is cut again and read again, exactly as
    # `familyocr segment` does when the pixels change.
    _segment(conn, crop_suffix="v2")
    _add_ocr(conn, ["庶一長子", "庶二次子"])

    entries = page_entries(conn, "doc", 58)
    assert entries[0].human == "庶一次子", "human correction lost on reprocessing"
    assert entries[0].crop_path.endswith("v2_0.png")


def test_marking_unreadable_records_no_transcription():
    conn = _db()
    _segment(conn)
    save_correction(conn, "doc", 58, "庶", 1, None, unreadable=True)
    entries = page_entries(conn, "doc", 58)
    assert entries[1].unreadable is True
    assert entries[1].human is None


def test_re_correcting_updates_in_place():
    conn = _db()
    _segment(conn)
    save_correction(conn, "doc", 58, "庶", 0, "first")
    save_correction(conn, "doc", 58, "庶", 0, "second")
    rows = conn.execute("SELECT COUNT(*) n FROM human_corrections").fetchone()["n"]
    assert rows == 1
    assert page_entries(conn, "doc", 58)[0].human == "second"


def test_progress_counts_reviewed_entries():
    conn = _db()
    _segment(conn)
    assert progress(conn, "doc") == {"entries": 2, "reviewed": 0}
    save_correction(conn, "doc", 58, "庶", 0, "x")
    assert progress(conn, "doc")["reviewed"] == 1


def test_export_writes_only_readable_verified_entries(tmp_path):
    conn = _db()
    _segment(conn)
    save_correction(conn, "doc", 58, "庶", 0, "庶一長子")
    save_correction(conn, "doc", 58, "庶", 1, None, unreadable=True)
    out = tmp_path / "verified.tsv"
    n = export_verified(conn, "doc", out)
    assert n == 1
    body = out.read_text(encoding="utf-8")
    assert "庶一長子" in body
    # An unreadable entry must not be exported as if it were verified text.
    assert body.count("\n58\t") == 0 or body.strip().count("58\t") == 1


def test_findings_are_attached_to_their_entry():
    conn = _db()
    _segment(conn)
    conn.execute(
        "INSERT INTO validation_findings (document_id, band_label, kind, "
        "page_index, entry_index, expected, observed) "
        "VALUES ('doc','庶','gap',58,1,'二','三')"
    )
    conn.commit()
    entries = page_entries(conn, "doc", 58)
    assert entries[0].findings == []
    assert entries[1].findings[0]["kind"] == "gap"


def test_tag_filter_selects_one_ocr_configuration():
    conn = _db()
    _segment(conn)
    _add_ocr(conn, ["庶一長子", "庶二次子"])
    assert page_entries(conn, "doc", 58, tag="t")[0].machine == "庶一長子"
    assert page_entries(conn, "doc", 58, tag="absent")[0].machine is None
