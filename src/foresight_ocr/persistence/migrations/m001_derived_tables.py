"""001 — the pre-versioning migrations, replayed idempotently.

These two ran on every connection before `user_version` existed. They are
reproduced here unchanged in effect so that a database created by an older
build still arrives at the same place; on a database that has already seen them
both checks fail and nothing happens.

The table definitions below are deliberately a *snapshot* rather than a
reference to `db.SCHEMA`. A migration describes the shape the schema had at this
point in its history; if `parsed_entries` changes again, a later migration says
so. Pointing at the head schema would make this step silently mean something
different every time the head moved.
"""

from __future__ import annotations

import sqlite3

_PARSED_ENTRIES = """
CREATE TABLE parsed_entries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id     TEXT NOT NULL,
    source_region_id INTEGER NOT NULL REFERENCES source_regions(id) ON DELETE CASCADE,
    page_index      INTEGER NOT NULL,
    band_label      TEXT NOT NULL,
    entry_index     INTEGER NOT NULL,
    source          TEXT NOT NULL,
    text            TEXT,
    own_id          TEXT,
    own_value       INTEGER,
    parent_id       TEXT,
    parent_value    INTEGER,
    parent_name     TEXT,
    birth_order     TEXT,
    order_rank      INTEGER,
    leftover        TEXT,
    flags_json      TEXT,
    UNIQUE (document_id, source_region_id)
)
"""

_PERSONS = """
CREATE TABLE persons (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id     TEXT NOT NULL,
    person_key      TEXT NOT NULL,
    generation      TEXT NOT NULL,
    own_id          TEXT NOT NULL,
    own_value       INTEGER NOT NULL,
    parsed_entry_id INTEGER REFERENCES parsed_entries(id) ON DELETE SET NULL,
    father_person_id INTEGER REFERENCES persons(id) ON DELETE SET NULL,
    father_key      TEXT,
    father_name     TEXT,
    birth_order     TEXT,
    order_rank      INTEGER,
    link_status     TEXT NOT NULL,
    UNIQUE (document_id, person_key)
)
"""


def apply(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(ocr_runs)")}
    if "tag" not in columns:
        conn.execute("ALTER TABLE ocr_runs ADD COLUMN tag TEXT NOT NULL DEFAULT ''")

    # parsed_entries was first keyed on (page, band_label, entry_index), which
    # collides whenever two entries on a page are read as the same generation.
    # Both tables are derived and rebuilt wholesale by `graph`, so dropping is
    # cheaper and safer than rewriting them in place.
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='parsed_entries'"
    ).fetchone()
    if sql and "UNIQUE (document_id, page_index" in sql[0]:
        conn.execute("DROP TABLE IF EXISTS persons")
        conn.execute("DROP TABLE IF EXISTS parsed_entries")
        conn.execute(_PARSED_ENTRIES)
        conn.execute(_PERSONS)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_parsed_doc "
            "ON parsed_entries(document_id, band_label)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_persons_father "
            "ON persons(document_id, father_key)"
        )
