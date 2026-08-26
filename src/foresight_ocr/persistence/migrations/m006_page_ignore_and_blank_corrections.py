"""006 — pages can be excluded, and an explicit blank stays blank.

Cover pages and other non-content scans remain part of the document, but can be
marked ignored so downstream review and export code can exclude them without
deleting their evidence.

Older review writes stored a deliberately cleared transcription as ``NULL``.
That is also the sentinel for "no human transcription", so reading the page
again fell back to the machine candidate and resurrected the deleted text.
Rows marked unreadable intentionally keep their ``NULL`` transcription; only a
readable correction is repaired to the explicit empty string.
"""

from __future__ import annotations

import sqlite3


def apply(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(pages)")}
    if "ignored" not in columns:
        conn.execute(
            "ALTER TABLE pages ADD COLUMN ignored INTEGER NOT NULL DEFAULT 0 "
            "CHECK (ignored IN (0, 1))"
        )

    conn.execute(
        "UPDATE human_corrections SET transcription = '' "
        "WHERE transcription IS NULL AND unreadable = 0"
    )
