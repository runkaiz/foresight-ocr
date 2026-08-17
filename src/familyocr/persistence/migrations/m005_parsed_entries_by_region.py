"""005 — the genealogy hangs off regions, not off crop rows.

`parsed_entries.source_region_id` referenced `source_regions(id)`, which is the
row describing a crop that a segmentation pass cut. Those rows are replaced
every time a page is segmented again, so the reference went stale on a rerun and
`ON DELETE CASCADE` removed the parsed entry outright. The effect was quiet and
bad: re-segmenting three pages of 丙辰庶富教1 dropped fifty-one people out of the
graph while every transcription involved was still on record.

Keying on `regions.region_uid` fixes it at the root — a region is the thing that
persists across segmentations, which is why it exists.

Both tables are derived and rebuilt wholesale by `graph`, so they are recreated
empty rather than migrated. The instruction is `familyocr graph <document>`.
"""

from __future__ import annotations

import sqlite3

_PARSED_ENTRIES = """
CREATE TABLE parsed_entries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id     TEXT NOT NULL,
    region_uid      TEXT NOT NULL REFERENCES regions(region_uid),
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
    UNIQUE (document_id, region_uid)
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
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='parsed_entries'"
    ).fetchone()
    if not row or "region_uid" in row[0]:
        return

    conn.execute("DROP TABLE IF EXISTS persons")
    conn.execute("DROP TABLE IF EXISTS parsed_entries")
    conn.execute(_PARSED_ENTRIES)
    conn.execute(_PERSONS)
