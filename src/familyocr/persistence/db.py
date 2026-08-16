"""SQLite persistence.

Provenance-first: a transcription is never stored as a bare string. It hangs off
an ocr_candidate, which hangs off a source_region, which carries both the
normalized bbox and the transform needed to map back to original source pixels.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS documents (
    id              TEXT PRIMARY KEY,
    title           TEXT,
    source_path     TEXT NOT NULL,
    checksum        TEXT NOT NULL,
    page_count      INTEGER NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS processing_runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id       TEXT NOT NULL REFERENCES documents(id),
    stage             TEXT NOT NULL,
    params_json       TEXT NOT NULL,
    params_hash       TEXT NOT NULL,
    input_checksum    TEXT,
    compute_backend   TEXT NOT NULL,
    pipeline_version  TEXT NOT NULL,
    git_commit        TEXT NOT NULL,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    status            TEXT NOT NULL DEFAULT 'running'
);

CREATE TABLE IF NOT EXISTS pages (
    document_id     TEXT NOT NULL REFERENCES documents(id),
    page_index      INTEGER NOT NULL,
    width           INTEGER NOT NULL,
    height          INTEGER NOT NULL,
    x_ppi           REAL,
    y_ppi           REAL,
    colorspace      TEXT,
    encoding        TEXT,
    PRIMARY KEY (document_id, page_index)
);

-- Original scans and every derivative are separate assets. The 'original' role
-- is written exactly once and never modified.
CREATE TABLE IF NOT EXISTS page_assets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id   TEXT NOT NULL,
    page_index    INTEGER NOT NULL,
    role          TEXT NOT NULL,        -- original | decoded | normalized | wm_suppressed | ...
    path          TEXT NOT NULL,
    checksum      TEXT NOT NULL,
    run_id        INTEGER REFERENCES processing_runs(id),
    created_at    TEXT NOT NULL,
    FOREIGN KEY (document_id, page_index) REFERENCES pages(document_id, page_index),
    UNIQUE (document_id, page_index, role)
);

-- Forward and inverse homography so any normalized coordinate maps back exactly.
CREATE TABLE IF NOT EXISTS transforms (
    id            TEXT PRIMARY KEY,
    document_id   TEXT NOT NULL,
    page_index    INTEGER NOT NULL,
    kind          TEXT NOT NULL,
    forward_json  TEXT NOT NULL,
    inverse_json  TEXT NOT NULL,
    residual      REAL,
    status        TEXT NOT NULL DEFAULT 'automatic',
    run_id        INTEGER REFERENCES processing_runs(id)
);

CREATE TABLE IF NOT EXISTS page_layouts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id    TEXT NOT NULL,
    page_index     INTEGER NOT NULL,
    layout_family  TEXT,
    frame_json     TEXT,
    template_fit   REAL,
    is_outlier     INTEGER NOT NULL DEFAULT 0,
    run_id         INTEGER REFERENCES processing_runs(id),
    UNIQUE (document_id, page_index, run_id)
);

CREATE TABLE IF NOT EXISTS bands (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    page_layout_id INTEGER NOT NULL REFERENCES page_layouts(id) ON DELETE CASCADE,
    band_index     INTEGER NOT NULL,
    label          TEXT,               -- 庶 / 富 / 教
    bbox_json      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS physical_entries (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    band_id        INTEGER NOT NULL REFERENCES bands(id) ON DELETE CASCADE,
    entry_index    INTEGER NOT NULL,   -- 0 = rightmost column (reading order)
    bbox_json      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_regions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id          INTEGER REFERENCES physical_entries(id) ON DELETE CASCADE,
    document_id       TEXT NOT NULL,
    page_index        INTEGER NOT NULL,
    role              TEXT NOT NULL,   -- id_glyphs | child_order | name_annotation
    context           TEXT NOT NULL,   -- tight | medium | full
    bbox_json         TEXT NOT NULL,   -- original image coordinates
    normalized_bbox_json TEXT,
    transform_id      TEXT REFERENCES transforms(id),
    crop_id           TEXT,
    crop_path         TEXT
);

CREATE TABLE IF NOT EXISTS models (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    version       TEXT NOT NULL,
    backend       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ocr_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER REFERENCES processing_runs(id),
    model_id      TEXT REFERENCES models(id),
    input_variant TEXT NOT NULL,
    -- Distinguishes configurations of the same model on the same variant
    -- (batching, image scale, prompt). Without it a second configuration would
    -- silently overwrite the first and the comparison would be lost.
    tag           TEXT NOT NULL DEFAULT ''
);

-- Every model's raw answer is kept. Nothing is overwritten by a later verifier.
CREATE TABLE IF NOT EXISTS ocr_candidates (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    source_region_id  INTEGER NOT NULL REFERENCES source_regions(id) ON DELETE CASCADE,
    ocr_run_id        INTEGER NOT NULL REFERENCES ocr_runs(id),
    transcription     TEXT,
    confidence        REAL,
    raw_json          TEXT
);

CREATE TABLE IF NOT EXISTS ocr_characters (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id      INTEGER NOT NULL REFERENCES ocr_candidates(id) ON DELETE CASCADE,
    char_index        INTEGER NOT NULL,
    character         TEXT NOT NULL,
    confidence        REAL,
    bbox_json         TEXT
);

-- Human corrections live beside machine output and survive reprocessing:
-- they key on the stable (document, page, band, entry, role) tuple, not on a
-- candidate row that a rerun would replace.
CREATE TABLE IF NOT EXISTS human_corrections (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id     TEXT NOT NULL,
    page_index      INTEGER NOT NULL,
    band_label      TEXT,
    entry_index     INTEGER,
    role            TEXT NOT NULL,
    transcription   TEXT,
    unreadable      INTEGER NOT NULL DEFAULT 0,
    corrected_by    TEXT,
    corrected_at    TEXT NOT NULL,
    note            TEXT,
    UNIQUE (document_id, page_index, band_label, entry_index, role)
);

CREATE TABLE IF NOT EXISTS validation_findings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER REFERENCES processing_runs(id),
    document_id     TEXT NOT NULL,
    band_label      TEXT NOT NULL,
    kind            TEXT NOT NULL,     -- gap | non_monotonic | duplicate | unparsed
    page_index      INTEGER,
    entry_index     INTEGER,
    expected        TEXT,
    observed        TEXT,
    status          TEXT NOT NULL DEFAULT 'needs_review'
);

-- The genealogy itself. Everything above describes the document; these two
-- tables describe the people printed in it, which is what the project is for.
--
-- Rebuilt wholesale by `graph` from whatever transcription is currently best
-- for each entry — a human correction where one exists, the recognizer
-- otherwise — so it is a derived view, never a place to edit. Corrections go
-- to human_corrections and the graph is rebuilt.
CREATE TABLE IF NOT EXISTS parsed_entries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id     TEXT NOT NULL,
    source_region_id INTEGER NOT NULL REFERENCES source_regions(id) ON DELETE CASCADE,
    page_index      INTEGER NOT NULL,
    band_label      TEXT NOT NULL,
    entry_index     INTEGER NOT NULL,
    source          TEXT NOT NULL,     -- human | ocr
    text            TEXT,
    -- Identity is the crop this came from, not (page, band, entry): band_label
    -- here is the generation the *text* claims, which two entries on one page
    -- can share when one of them was misread.
    own_id          TEXT,
    own_value       INTEGER,
    parent_id       TEXT,
    parent_value    INTEGER,
    parent_name     TEXT,
    birth_order     TEXT,
    order_rank      INTEGER,
    leftover        TEXT,
    flags_json      TEXT,              -- label_from_geometry, numeral repairs, …
    UNIQUE (document_id, source_region_id)
);

-- One row per person the chart names. `father_person_id` is the reconstructed
-- link; `link_status` records why it is null when it is, because "no father
-- found" is a finding, not a blank.
CREATE TABLE IF NOT EXISTS persons (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id     TEXT NOT NULL,
    person_key      TEXT NOT NULL,     -- e.g. 庶:335 — stable within a document
    generation      TEXT NOT NULL,     -- band label: 庶 / 富 / 教 …
    own_id          TEXT NOT NULL,
    own_value       INTEGER NOT NULL,
    parsed_entry_id INTEGER REFERENCES parsed_entries(id) ON DELETE SET NULL,
    father_person_id INTEGER REFERENCES persons(id) ON DELETE SET NULL,
    father_key      TEXT,
    father_name     TEXT,
    birth_order     TEXT,
    order_rank      INTEGER,
    link_status     TEXT NOT NULL,     -- resolved | named_only | unresolved | root
    UNIQUE (document_id, person_key)
);

CREATE INDEX IF NOT EXISTS idx_parsed_doc ON parsed_entries(document_id, band_label);
CREATE INDEX IF NOT EXISTS idx_persons_father ON persons(document_id, father_key);
CREATE INDEX IF NOT EXISTS idx_assets_page ON page_assets(document_id, page_index);
CREATE INDEX IF NOT EXISTS idx_regions_page ON source_regions(document_id, page_index);
CREATE INDEX IF NOT EXISTS idx_runs_stage ON processing_runs(document_id, stage, params_hash);
"""


# Long enough to outlast a segmentation page's write burst, short enough that a
# genuine deadlock still surfaces as an error rather than a hang.
_BUSY_TIMEOUT_S = 60.0


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=_BUSY_TIMEOUT_S)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Stages are long and write in bursts: OCR of one volume can run while
    # another volume is being segmented, and the review server reads while both
    # write. Under the default rollback journal a second writer fails instantly
    # with "database is locked" and takes an hour of work with it. WAL lets
    # readers run against writers, and the busy timeout makes a writer wait its
    # turn instead of dying.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(f"PRAGMA busy_timeout = {int(_BUSY_TIMEOUT_S * 1000)}")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive migrations for databases created by an earlier version."""
    have = {row[1] for row in conn.execute("PRAGMA table_info(ocr_runs)")}
    if "tag" not in have:
        conn.execute("ALTER TABLE ocr_runs ADD COLUMN tag TEXT NOT NULL DEFAULT ''")

    # parsed_entries was first keyed on (page, band_label, entry_index), which
    # collides whenever two entries on a page are read as the same generation.
    # Both tables are derived and rebuilt by `graph`, so dropping is cheaper and
    # safer than rewriting them in place.
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='parsed_entries'"
    ).fetchone()
    if sql and "UNIQUE (document_id, page_index" in sql[0]:
        conn.execute("DROP TABLE IF EXISTS persons")
        conn.execute("DROP TABLE IF EXISTS parsed_entries")
        conn.executescript(SCHEMA)
