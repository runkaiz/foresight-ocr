"""004 — a disagreement does not need a batch run to have happened.

`region_proposals.run_id` was NOT NULL, which assumed every proposal came from a
pipeline stage. Reconciling is also what the editor does when it re-proposes a
single page, and there is no `processing_runs` row behind that. Recording the
disagreement is the point; which run noticed it is provenance, and provenance
that is sometimes absent should be nullable rather than invented.

The table has never held a row outside a test, so this is a rebuild of an empty
table rather than a data migration.
"""

from __future__ import annotations

import sqlite3

_REBUILT = """
CREATE TABLE region_proposals_new (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER REFERENCES processing_runs(id),
    document_id   TEXT NOT NULL,
    page_index    INTEGER NOT NULL,
    band_label    TEXT,
    region_id     INTEGER REFERENCES regions(id) ON DELETE CASCADE,
    bbox_json     TEXT NOT NULL,
    geometry_hash TEXT NOT NULL,
    entry_index   INTEGER,
    kind          TEXT NOT NULL,
    iou           REAL,
    status        TEXT NOT NULL DEFAULT 'open',
    created_at    TEXT NOT NULL
)
"""


def apply(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='region_proposals'"
    ).fetchone()
    if not row or "run_id        INTEGER NOT NULL" not in row[0]:
        return

    conn.execute(_REBUILT)
    conn.execute(
        "INSERT INTO region_proposals_new SELECT id, run_id, document_id, page_index, "
        "band_label, region_id, bbox_json, geometry_hash, entry_index, kind, iou, "
        "status, created_at FROM region_proposals"
    )
    conn.execute("DROP TABLE region_proposals")
    conn.execute("ALTER TABLE region_proposals_new RENAME TO region_proposals")
