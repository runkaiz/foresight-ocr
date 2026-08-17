"""SQLite persistence.

Provenance-first: a transcription is never stored as a bare string. It hangs off
an ocr_candidate, which hangs off a source_region, which carries both the
normalized bbox and the transform needed to map back to original source pixels.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import migrations

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

-- The editable document. One row per thing a reviewer can see, move, split,
-- merge, reorder or verify — so one per column, not one per crop width.
--
-- `region_uid` is opaque and permanent; `geometry_hash` is the content address
-- of the box. Keeping those separate is the whole point: `crop_id` conflated
-- them, so the name of a region changed whenever a neighbour was inserted.
--
-- `band_label` is a string rather than a reference to `bands`, because a bands
-- row hangs off a run-scoped page_layouts row and a region the user verified
-- must not be reachable only through the run that first proposed it.
CREATE TABLE IF NOT EXISTS regions (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    region_uid           TEXT    NOT NULL UNIQUE,
    document_id          TEXT    NOT NULL REFERENCES documents(id),
    page_index           INTEGER NOT NULL,

    bbox_json            TEXT    NOT NULL,   -- [x0,y0,x1,y1] in canonical space
    geometry_hash        TEXT    NOT NULL,
    transform_id         TEXT    REFERENCES transforms(id),
    orig_quad_json       TEXT,               -- cached map back to source pixels

    band_label           TEXT,               -- 庶 / 富 / 教
    band_ordinal         INTEGER,            -- 0-based, top to bottom

    -- Where the text is, and in what order it reads, are different facts. The
    -- lattice index is kept only as a debugging aid and as the fallback key for
    -- corrections stored before regions had identity.
    reading_order        INTEGER NOT NULL DEFAULT 0,
    reading_order_locked INTEGER NOT NULL DEFAULT 0,
    entry_index          INTEGER,

    role                 TEXT NOT NULL DEFAULT 'entry',
    state                TEXT NOT NULL DEFAULT 'proposed',
    created_by           TEXT NOT NULL DEFAULT 'machine',
    origin_run_id        INTEGER REFERENCES processing_runs(id),
    last_run_id          INTEGER REFERENCES processing_runs(id),
    updated_by           TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    -- Soft delete. A region is never removed, because its OCR answers and any
    -- human reading of it are evidence that outlives the box.
    deleted_at           TEXT,

    CHECK (state IN ('proposed','adjusted','verified','rejected')),
    CHECK (role  IN ('entry','header','marginalia','ignore'))
);

-- Split and merge provenance. Append-only: "where did this text come from"
-- must stay answerable across an arbitrary edit history.
CREATE TABLE IF NOT EXISTS region_lineage (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    child_uid    TEXT NOT NULL,
    parent_uid   TEXT NOT NULL,
    op           TEXT NOT NULL,   -- split | merge | reconcile_replace | import
    run_id       INTEGER REFERENCES processing_runs(id),
    actor        TEXT NOT NULL,
    at           TEXT NOT NULL,
    detail_json  TEXT
);

-- One row per (pixels, variant) actually rendered. `crop_key` addresses the
-- pixels, so an unchanged region re-segmented produces a hit and no file write.
CREATE TABLE IF NOT EXISTS region_crops (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    region_id       INTEGER NOT NULL REFERENCES regions(id) ON DELETE RESTRICT,
    geometry_hash   TEXT NOT NULL,          -- the box as it stood at cut time
    context         TEXT NOT NULL,          -- tight | medium | full
    pad_frac        REAL NOT NULL,
    variant         TEXT NOT NULL,
    pixel_bbox_json TEXT NOT NULL,          -- the integer box actually sliced
    crop_key        TEXT NOT NULL,
    path            TEXT NOT NULL,
    checksum        TEXT,
    created_at      TEXT NOT NULL,
    UNIQUE (crop_key)
);

-- Only rows where the machine and the working document disagree: a proposal the
-- reviewer's geometry contradicts, or a region no proposal covers. Storing the
-- disagreement rather than a shadow copy of every region is what keeps
-- "machine proposal vs working version" from doubling the table.
-- `run_id` is nullable: a disagreement between a person's box and the
-- detector's is a fact about the page, and reconciling can be asked for by the
-- editor as readily as by a batch stage.
CREATE TABLE IF NOT EXISTS region_proposals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER REFERENCES processing_runs(id),
    document_id   TEXT NOT NULL,
    page_index    INTEGER NOT NULL,
    band_label    TEXT,
    region_id     INTEGER REFERENCES regions(id) ON DELETE CASCADE,
    bbox_json     TEXT NOT NULL,
    geometry_hash TEXT NOT NULL,
    entry_index   INTEGER,
    kind          TEXT NOT NULL,   -- divergence | orphan_proposal | orphan_region
    iou           REAL,
    status        TEXT NOT NULL DEFAULT 'open',
    created_at    TEXT NOT NULL
);

-- Where a crop was cut. Superseded as the editable unit by `regions`; kept
-- because it is what the OCR benchmark was run against, and because its
-- medium/full rows record crop widths that `regions` deliberately does not
-- model — a context is a rendering of a region, not a region.
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
    crop_path         TEXT,
    region_id         INTEGER REFERENCES regions(id)
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
--
-- `source_region_id` used to carry ON DELETE CASCADE, which made re-segmenting
-- a page destroy its OCR: `segment` deleted the crop rows and every answer went
-- with them, whether or not the geometry had actually moved. It is now a plain
-- breadcrumb, and the live link is `region_id` with ON DELETE RESTRICT — a
-- region carrying answers cannot be deleted, only retired.
--
-- `cache_key` addresses (these pixels, read by this configuration), so a second
-- run of the same thing updates one row instead of accumulating a duplicate,
-- and a new model version lands beside the old answers rather than on top.
CREATE TABLE IF NOT EXISTS ocr_candidates (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    region_id         INTEGER REFERENCES regions(id) ON DELETE RESTRICT,
    source_region_id  INTEGER,
    ocr_run_id        INTEGER NOT NULL REFERENCES ocr_runs(id),
    crop_key          TEXT,
    model_key         TEXT,
    cache_key         TEXT,
    transcription     TEXT,
    confidence        REAL,
    raw_json          TEXT,
    created_at        TEXT
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
    -- Keyed to the region, not to the crop row it was cut from. A crop row is
    -- replaced whenever the page is segmented again, so keying there meant a
    -- rerun silently emptied the genealogy for those pages even though every
    -- transcription was still on record.
    region_uid      TEXT NOT NULL REFERENCES regions(region_uid),
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
    UNIQUE (document_id, region_uid)
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

"""

# Applied after migrations, not with the tables: an index can name a column that
# only a migration adds, and ordering it here means the two never disagree about
# whether that column exists yet.
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_parsed_doc ON parsed_entries(document_id, band_label);
CREATE INDEX IF NOT EXISTS idx_persons_father ON persons(document_id, father_key);
CREATE INDEX IF NOT EXISTS idx_assets_page ON page_assets(document_id, page_index);
-- Historical name: this one is on source_regions, not on `regions`.
CREATE INDEX IF NOT EXISTS idx_regions_page ON source_regions(document_id, page_index);
CREATE INDEX IF NOT EXISTS idx_source_regions_region ON source_regions(region_id);
CREATE INDEX IF NOT EXISTS idx_runs_stage ON processing_runs(document_id, stage, params_hash);

CREATE INDEX IF NOT EXISTS idx_region_live ON regions(document_id, page_index)
    WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_region_order
    ON regions(document_id, page_index, band_label, reading_order);
CREATE INDEX IF NOT EXISTS idx_region_geometry ON regions(geometry_hash);
-- How a fresh machine proposal finds the region it produced last time.
CREATE INDEX IF NOT EXISTS idx_region_lattice
    ON regions(document_id, page_index, band_ordinal, entry_index);
CREATE INDEX IF NOT EXISTS idx_region_state ON regions(document_id, state)
    WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_lineage_child ON region_lineage(child_uid);
CREATE INDEX IF NOT EXISTS idx_lineage_parent ON region_lineage(parent_uid);
CREATE INDEX IF NOT EXISTS idx_region_crops_region
    ON region_crops(region_id, context, variant);
CREATE INDEX IF NOT EXISTS idx_region_proposals_page
    ON region_proposals(document_id, page_index, status);

-- One answer per (pixels, configuration). Partial, because rows predating the
-- cache have no key and must not collide with each other.
CREATE UNIQUE INDEX IF NOT EXISTS idx_candidate_cache
    ON ocr_candidates(cache_key) WHERE cache_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_candidate_region ON ocr_candidates(region_id);
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
    """Bring the database to the current schema.

    `SCHEMA` above is the head definition and is the whole story for a new
    database. `migrations` carries an existing one forward through the changes
    `CREATE TABLE IF NOT EXISTS` cannot express: added columns, rebuilt
    constraints, and backfills of new tables from old rows. Indexes come last,
    because some of them name columns a migration has just added.
    """
    fresh = not table_exists(conn, "documents")
    conn.executescript(SCHEMA)
    migrations.apply(conn, fresh=fresh)
    conn.executescript(INDEXES)
    conn.commit()


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )
