"""Schema v6: ignored pages and durable explicitly blank corrections."""

from __future__ import annotations

import sqlite3

import pytest

from foresight_ocr.persistence import migrations
from foresight_ocr.persistence.db import connect, init_schema
from foresight_ocr.persistence.migrations import m006_page_ignore_and_blank_corrections

DOC = "cover-test"

_V5_TABLES = """
CREATE TABLE documents (
    id          TEXT PRIMARY KEY,
    title       TEXT,
    source_path TEXT NOT NULL,
    checksum    TEXT NOT NULL,
    page_count  INTEGER NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE pages (
    document_id TEXT NOT NULL REFERENCES documents(id),
    page_index  INTEGER NOT NULL,
    width       INTEGER NOT NULL,
    height      INTEGER NOT NULL,
    x_ppi       REAL,
    y_ppi       REAL,
    colorspace  TEXT,
    encoding    TEXT,
    PRIMARY KEY (document_id, page_index)
);

CREATE TABLE human_corrections (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id   TEXT NOT NULL,
    page_index    INTEGER NOT NULL,
    band_label    TEXT,
    entry_index   INTEGER,
    role          TEXT NOT NULL,
    transcription TEXT,
    unreadable    INTEGER NOT NULL DEFAULT 0,
    corrected_by  TEXT,
    corrected_at  TEXT NOT NULL,
    note          TEXT,
    UNIQUE (document_id, page_index, band_label, entry_index, role)
);
"""


def _insert_document(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?)",
        (DOC, "Cover test", "book.pdf", "checksum", 1, "now"),
    )


def _legacy_v5(tmp_path) -> sqlite3.Connection:
    conn = connect(tmp_path / "foresight-ocr.db")
    conn.executescript(_V5_TABLES)
    _insert_document(conn)
    conn.execute(
        "INSERT INTO pages (document_id, page_index, width, height) "
        "VALUES (?, 1, 1000, 1500)",
        (DOC,),
    )
    conn.executemany(
        "INSERT INTO human_corrections "
        "(document_id, page_index, band_label, entry_index, role, transcription, "
        "unreadable, corrected_at) VALUES (?, 1, NULL, NULL, ?, ?, ?, 'now')",
        [
            (DOC, "cleared", None, 0),
            (DOC, "unreadable", None, 1),
            (DOC, "existing", "卷十", 0),
        ],
    )
    conn.execute("PRAGMA user_version = 5")
    conn.commit()
    return conn


def test_fresh_schema_constrains_ignored_to_boolean(tmp_path):
    conn = connect(tmp_path / "fresh.db")
    init_schema(conn)
    _insert_document(conn)

    column = next(
        row for row in conn.execute("PRAGMA table_info(pages)") if row[1] == "ignored"
    )
    assert column[3] == 1
    assert column[4] == "0"

    conn.execute(
        "INSERT INTO pages (document_id, page_index, width, height) "
        "VALUES (?, 1, 1000, 1500)",
        (DOC,),
    )
    assert conn.execute("SELECT ignored FROM pages").fetchone()[0] == 0
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE pages SET ignored = 2")


def test_v6_migrates_pages_and_repairs_only_readable_nulls(tmp_path):
    conn = _legacy_v5(tmp_path)

    init_schema(conn)

    assert migrations.HEAD == 6
    assert migrations.current_version(conn) == 6
    assert conn.execute("SELECT ignored FROM pages").fetchone()[0] == 0
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE pages SET ignored = -1")
    rows = {
        row["role"]: (row["transcription"], row["unreadable"])
        for row in conn.execute(
            "SELECT role, transcription, unreadable FROM human_corrections"
        )
    }
    assert rows == {
        "cleared": ("", 0),
        "unreadable": (None, 1),
        "existing": ("卷十", 0),
    }


def test_v6_migration_is_idempotent(tmp_path):
    conn = _legacy_v5(tmp_path)

    m006_page_ignore_and_blank_corrections.apply(conn)
    conn.execute("UPDATE pages SET ignored = 1")
    m006_page_ignore_and_blank_corrections.apply(conn)

    columns = [row[1] for row in conn.execute("PRAGMA table_info(pages)")]
    assert columns.count("ignored") == 1
    assert conn.execute("SELECT ignored FROM pages").fetchone()[0] == 1
    assert (
        conn.execute(
            "SELECT transcription FROM human_corrections WHERE role = 'cleared'"
        ).fetchone()[0]
        == ""
    )
    assert (
        conn.execute(
            "SELECT transcription FROM human_corrections WHERE role = 'unreadable'"
        ).fetchone()[0]
        is None
    )
