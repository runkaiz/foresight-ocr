"""Upgrading a pre-region database.

The migration has one job that matters more than the rest: after it, re-running
segmentation cannot destroy recognizer answers. Before it, `segment` deleted the
crop rows for a page and `ON DELETE CASCADE` took every transcription with them
— whether or not the geometry had moved, and whether or not a person had already
reviewed the page. The last two tests here are that behaviour, asserted as gone.

The database is built with the *old* table definitions on purpose. Creating it
from the current schema would test nothing: the point is that a database written
months ago arrives at the new shape with its data intact.
"""

import sqlite3

import pytest

from foresight_ocr.persistence import migrations
from foresight_ocr.persistence.db import connect, init_schema

DOC = "丙辰庶富教9"

# The definitions as they stood before regions existed. source_regions has no
# region_id; ocr_candidates cascades from it.
_OLD = """
CREATE TABLE source_regions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id          INTEGER REFERENCES physical_entries(id) ON DELETE CASCADE,
    document_id       TEXT NOT NULL,
    page_index        INTEGER NOT NULL,
    role              TEXT NOT NULL,
    context           TEXT NOT NULL,
    bbox_json         TEXT NOT NULL,
    normalized_bbox_json TEXT,
    transform_id      TEXT,
    crop_id           TEXT,
    crop_path         TEXT
);
CREATE TABLE ocr_candidates (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    source_region_id  INTEGER NOT NULL REFERENCES source_regions(id) ON DELETE CASCADE,
    ocr_run_id        INTEGER NOT NULL REFERENCES ocr_runs(id),
    transcription     TEXT,
    confidence        REAL,
    raw_json          TEXT
);
"""

# Two columns on one page, each cut at three widths — the shape that makes
# source_regions the wrong grain for an editable document.
_COLUMNS = [
    (0, [1919.5, 0.0, 2253.5, 1012.7]),
    (1, [1551.5, 0.0, 1919.5, 1012.7]),
]
_PAGE_WIDTH = 2300.0
_PITCH = 368.0
#: name -> horizontal padding as a multiple of the pitch, as segmentation cuts it
_CONTEXTS = {"tight": 0.0, "medium": 0.35, "full": 0.75}


def _padded(bbox: list[float], pad_frac: float) -> list[float]:
    x0, y0, x1, y1 = bbox
    pad = _PITCH * pad_frac
    return [max(x0 - pad, 0.0), y0, min(x1 + pad, _PAGE_WIDTH), y1]


@pytest.fixture
def legacy(tmp_path):
    """A database and artifact tree as an earlier version of the tool left it."""
    artifacts = tmp_path / "artifacts"
    (artifacts / "pages" / DOC / "normalized").mkdir(parents=True)
    (artifacts / "pages" / DOC / "normalized" / "p0058.png").write_bytes(b"page bytes")
    for variant in ("maxrgb", "original"):
        (artifacts / "crops" / DOC / variant).mkdir(parents=True)

    conn = connect(artifacts / "foresight-ocr.db")
    conn.executescript(_OLD)
    conn.executescript(
        """
        CREATE TABLE documents (id TEXT PRIMARY KEY, title TEXT, source_path TEXT,
            checksum TEXT, page_count INTEGER, created_at TEXT);
        CREATE TABLE pages (document_id TEXT, page_index INTEGER, width INTEGER,
            height INTEGER, x_ppi REAL, y_ppi REAL, colorspace TEXT, encoding TEXT,
            PRIMARY KEY (document_id, page_index));
        """
    )
    conn.execute(
        "INSERT INTO documents VALUES (?,?,?,?,?,?)", (DOC, DOC, "p", "c", 1, "now")
    )
    conn.execute(
        "INSERT INTO pages VALUES (?,?,?,?,?,?,?,?)",
        (DOC, 58, 2300, 3025, None, None, None, None),
    )

    # init_schema fills in every table the old build shared with this one.
    init_schema(conn)
    assert migrations.current_version(conn) == migrations.HEAD

    conn.execute(
        "INSERT INTO processing_runs (id, document_id, stage, params_json, params_hash, "
        "compute_backend, pipeline_version, git_commit, started_at) "
        "VALUES (1,?,'segment','{}','h','local','0','x','now')",
        (DOC,),
    )
    layout = conn.execute(
        "INSERT INTO page_layouts (document_id, page_index, run_id) VALUES (?,58,1)",
        (DOC,),
    ).lastrowid
    band = conn.execute(
        "INSERT INTO bands (page_layout_id, band_index, label, bbox_json) "
        "VALUES (?,0,NULL,'[0,0,2300,1012.7]')",
        (layout,),
    ).lastrowid

    for entry_index, bbox in _COLUMNS:
        entry = conn.execute(
            "INSERT INTO physical_entries (band_id, entry_index, bbox_json) "
            "VALUES (?,?,?)",
            (band, entry_index, repr(bbox)),
        ).lastrowid
        for context, pad_frac in _CONTEXTS.items():
            crop_id = f"{DOC}_p0058_b0_e{entry_index:02d}_{context}"
            path = artifacts / "crops" / DOC / "maxrgb" / f"{crop_id}.png"
            path.write_bytes(b"crop bytes")
            if context == "tight":
                # A second variant exists for the width that was benchmarked.
                (artifacts / "crops" / DOC / "original" / f"{crop_id}.png").write_bytes(
                    b"crop bytes"
                )
            conn.execute(
                "INSERT INTO source_regions (entry_id, document_id, page_index, role, "
                "context, bbox_json, normalized_bbox_json, crop_id, crop_path) "
                "VALUES (?,?,58,'entry',?,'[]',?,?,?)",
                (
                    entry,
                    DOC,
                    context,
                    repr(_padded(bbox, pad_frac)),
                    crop_id,
                    str(path),
                ),
            )

    conn.execute(
        "INSERT INTO models (id, name, version, backend) "
        "VALUES ('paddleocr_vl:1.6','paddleocr_vl','1.6','paddleocr_vl')"
    )
    conn.execute(
        "INSERT INTO ocr_runs (id, run_id, model_id, input_variant, tag) "
        "VALUES (1,1,'paddleocr_vl:1.6','maxrgb','book-v3')"
    )
    for region in conn.execute(
        "SELECT id FROM source_regions WHERE context='tight' ORDER BY id"
    ).fetchall():
        conn.execute(
            "INSERT INTO ocr_candidates (source_region_id, ocr_run_id, transcription) "
            "VALUES (?,1,'庶三百三十五允二百八十六次子')",
            (region["id"],),
        )
    conn.commit()

    # Force the upgrade path: the rows above were written in the old shape.
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    init_schema(conn)
    return conn


def test_three_crop_widths_collapse_to_one_editable_region(legacy):
    assert legacy.execute("SELECT COUNT(*) FROM regions").fetchone()[0] == 2
    assert legacy.execute("SELECT COUNT(*) FROM source_regions").fetchone()[0] == 6

    shared = legacy.execute(
        "SELECT COUNT(DISTINCT region_id) FROM source_regions "
        "WHERE crop_id LIKE '%_e00_%'"
    ).fetchone()[0]
    assert shared == 1


def test_every_crop_row_finds_its_region(legacy):
    assert (
        legacy.execute(
            "SELECT COUNT(*) FROM source_regions WHERE region_id IS NULL"
        ).fetchone()[0]
        == 0
    )


def test_regions_take_the_band_label_and_the_lattice_order(legacy):
    rows = legacy.execute(
        "SELECT band_label, band_ordinal, reading_order, entry_index, state, created_by "
        "FROM regions ORDER BY reading_order"
    ).fetchall()
    assert [r["band_label"] for r in rows] == ["庶", "庶"]
    assert [r["reading_order"] for r in rows] == [0, 1]
    assert [r["entry_index"] for r in rows] == [0, 1]
    assert {r["state"] for r in rows} == {"proposed"}
    assert {r["created_by"] for r in rows} == {"machine"}


def test_each_variant_on_disk_gets_its_own_address(legacy):
    # Six maxrgb crops, plus the two tight ones that were also cut as `original`.
    assert legacy.execute("SELECT COUNT(*) FROM region_crops").fetchone()[0] == 8
    keys = [r[0] for r in legacy.execute("SELECT crop_key FROM region_crops")]
    assert len(set(keys)) == len(keys)


def test_crops_that_are_the_same_pixels_share_one_address(legacy):
    """Two contexts that clamp to the same box are one crop, not two.

    At a page edge the padded widths collapse onto the tight one. The file is
    the same file, so it gets one row — the address describes pixels, and
    pretending there are two would make the cache report work that does not
    exist.
    """
    rows = legacy.execute(
        "SELECT pixel_bbox_json, variant, COUNT(*) n FROM region_crops "
        "GROUP BY pixel_bbox_json, variant HAVING n > 1"
    ).fetchall()
    assert rows == []


def test_the_normalized_page_gets_a_checksum(legacy):
    row = legacy.execute(
        "SELECT checksum FROM page_assets WHERE role='normalized'"
    ).fetchone()
    assert row and row["checksum"]


def test_answers_are_readdressed_without_being_touched(legacy):
    rows = legacy.execute(
        "SELECT region_id, crop_key, model_key, cache_key, transcription "
        "FROM ocr_candidates"
    ).fetchall()
    assert len(rows) == 2
    for row in rows:
        assert row["region_id"] is not None
        assert row["crop_key"] and row["model_key"] and row["cache_key"]
        assert row["transcription"] == "庶三百三十五允二百八十六次子"


def test_resegmenting_a_page_no_longer_destroys_its_transcriptions(legacy):
    """The behaviour this migration exists to remove.

    This is verbatim what `segment` did to the pages it was about to redo. It
    used to take every answer for those pages with it.
    """
    before = legacy.execute("SELECT COUNT(*) FROM ocr_candidates").fetchone()[0]
    legacy.execute(
        "DELETE FROM physical_entries WHERE id IN (SELECT entry_id FROM source_regions "
        "WHERE document_id=? AND page_index=58)",
        (DOC,),
    )
    legacy.execute(
        "DELETE FROM source_regions WHERE document_id=? AND page_index=58", (DOC,)
    )
    assert legacy.execute("SELECT COUNT(*) FROM ocr_candidates").fetchone()[0] == before


def test_a_region_holding_answers_cannot_be_deleted(legacy):
    with pytest.raises(sqlite3.IntegrityError):
        legacy.execute("DELETE FROM regions WHERE document_id=?", (DOC,))


def test_migrating_twice_changes_nothing(legacy):
    def counts():
        return tuple(
            legacy.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("regions", "region_crops", "ocr_candidates", "page_assets")
        )

    before = counts()
    init_schema(legacy)
    assert counts() == before
