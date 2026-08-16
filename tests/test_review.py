"""Review data layer.

The load-bearing test here is that a human correction survives reprocessing.
The spec requires it, the schema was designed for it (corrections key on the
stable document/page/band/entry tuple rather than on a candidate row), and until
now nothing exercised it.
"""

import sqlite3

import pytest

from familyocr.persistence.db import init_schema
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
    """Simulate a segmentation pass: layout, bands, entries, regions."""
    conn.execute("DELETE FROM page_layouts")
    cur = conn.execute(
        "INSERT INTO page_layouts (document_id, page_index) VALUES ('doc', 58)"
    )
    layout_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO bands (page_layout_id, band_index, label, bbox_json) "
        "VALUES (?,0,'庶','[]')", (layout_id,)
    )
    band_id = cur.lastrowid
    for entry in (0, 1):
        cur = conn.execute(
            "INSERT INTO physical_entries (band_id, entry_index, bbox_json) "
            "VALUES (?,?,'[]')", (band_id, entry)
        )
        conn.execute(
            "INSERT INTO source_regions (entry_id, document_id, page_index, role, "
            "context, bbox_json, crop_id, crop_path) "
            "VALUES (?, 'doc', 58, 'entry', 'tight', '[]', ?, ?)",
            (cur.lastrowid, f"doc_p0058_b0_e{entry:02d}_{crop_suffix}",
             f"/crops/{crop_suffix}_{entry}.png"),
        )
    conn.commit()


def _add_ocr(conn, texts):
    conn.execute(
        "INSERT INTO models (id, name, version, backend) "
        "VALUES ('m','paddleocr_vl','1.6','paddleocr_vl') "
        "ON CONFLICT(id) DO NOTHING"
    )
    cur = conn.execute(
        "INSERT INTO ocr_runs (run_id, model_id, input_variant, tag) "
        "VALUES (1,'m','maxrgb','t')"
    )
    run = cur.lastrowid
    regions = conn.execute(
        "SELECT id, crop_id FROM source_regions ORDER BY id"
    ).fetchall()
    for region, text in zip(regions, texts):
        conn.execute(
            "INSERT INTO ocr_candidates (source_region_id, ocr_run_id, "
            "transcription) VALUES (?,?,?)", (region["id"], run, text)
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

    # Re-segment: regions, entries and their OCR candidates are all replaced,
    # exactly as `familyocr segment` does when geometry changes.
    conn.execute("DELETE FROM physical_entries")
    conn.execute("DELETE FROM source_regions")
    conn.commit()
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
