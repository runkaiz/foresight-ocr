"""003 — break the cascade, and give every answer a cache address.

Two changes to `ocr_candidates`, and the first one is the reason this whole
migration exists.

`source_region_id` referenced `source_regions(id) ON DELETE CASCADE`. `segment`
deletes and reinserts crop rows for the pages it processes, so re-running it
deleted every recognizer answer for those pages — including the pages where
nothing had moved, and including any page a reviewer had already worked on. The
column stays as a breadcrumb but loses its foreign key; the live link becomes
`region_id ... ON DELETE RESTRICT`, so a region holding answers cannot be
deleted at all, only retired. Destroying transcriptions stops being something
the code has to remember not to do.

The second change is `cache_key`: the address of (these pixels, read by this
configuration). It is what lets an unchanged page be recognised as already done,
a moved region be the only thing re-read, and a new model's answers be stored
beside the old ones rather than over them.

SQLite cannot alter a foreign key, so the table is rebuilt. `ocr_characters`
references candidate ids, which are copied through unchanged.
"""

from __future__ import annotations

import json
import sqlite3

_NEW_TABLE = """
CREATE TABLE ocr_candidates_new (
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
)
"""


def apply(conn: sqlite3.Connection) -> None:
    if not _has_table(conn, "ocr_candidates"):
        return

    _add_columns(conn)
    _backfill_region_and_crop(conn)
    _backfill_model_and_cache_keys(conn)
    _drop_duplicate_answers(conn)
    _rebuild_without_cascade(conn)


def _add_columns(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(ocr_candidates)")}
    for name, decl in (
        ("region_id", "INTEGER"),
        ("crop_key", "TEXT"),
        ("model_key", "TEXT"),
        ("cache_key", "TEXT"),
        ("created_at", "TEXT"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE ocr_candidates ADD COLUMN {name} {decl}")


def _backfill_region_and_crop(conn: sqlite3.Connection) -> None:
    """Attach each answer to the region it read, and to the pixels it read.

    The crop is matched on variant as well as region and context: a crop row
    exists per variant, and the recognizers were benchmarked across several, so
    the answer's own `input_variant` is what says which pixels it saw. Answers
    from runs whose crops are no longer on disk keep a NULL `crop_key` — they
    are historical benchmark data, and inventing an address for pixels nobody
    can produce would make the cache lie.
    """
    conn.execute(
        """
        UPDATE ocr_candidates SET region_id = (
            SELECT sr.region_id FROM source_regions sr
             WHERE sr.id = ocr_candidates.source_region_id)
         WHERE region_id IS NULL
        """
    )
    conn.execute(
        """
        UPDATE ocr_candidates SET crop_key = (
            SELECT rc.crop_key
              FROM source_regions sr
              JOIN ocr_runs orun ON orun.id = ocr_candidates.ocr_run_id
              JOIN region_crops rc
                ON rc.region_id = sr.region_id
               AND rc.context   = sr.context
               AND rc.variant   = orun.input_variant
             WHERE sr.id = ocr_candidates.source_region_id
             LIMIT 1)
         WHERE crop_key IS NULL
        """
    )


def _backfill_model_and_cache_keys(conn: sqlite3.Connection) -> None:
    """Recover each run's recognizer configuration from the run that made it.

    The options that distinguish two runs of the same model — prompt, image
    scale, batching — were recorded in `processing_runs.params_json` and
    otherwise only in a free-text tag. Folding them into `model_key` makes that
    distinction structural, so re-running under one tag can no longer be
    confused with re-running under different settings.
    """
    from ...ocr.cache import cache_key, model_key

    keys: dict[int, str] = {}
    for row in conn.execute(
        """
        SELECT orun.id, orun.tag, m.backend, m.version, pr.params_json
          FROM ocr_runs orun
          JOIN models m ON m.id = orun.model_id
          LEFT JOIN processing_runs pr ON pr.id = orun.run_id
        """
    ):
        params = json.loads(row["params_json"] or "{}")
        options = (params.get("backend_options") or {}).get(row["backend"]) or {}
        keys[row["id"]] = model_key(row["backend"], row["version"], options)

    if keys:
        conn.executemany(
            "UPDATE ocr_candidates SET model_key = ? WHERE ocr_run_id = ? "
            "AND model_key IS NULL",
            [(key, run_id) for run_id, key in keys.items()],
        )

    updates = [
        (cache_key(row["crop_key"], row["model_key"], row["tag"] or ""), row["id"])
        for row in conn.execute(
            "SELECT c.id, c.crop_key, c.model_key, orun.tag "
            "FROM ocr_candidates c JOIN ocr_runs orun ON orun.id = c.ocr_run_id "
            "WHERE c.cache_key IS NULL AND c.crop_key IS NOT NULL "
            "AND c.model_key IS NOT NULL"
        )
    ]
    conn.executemany("UPDATE ocr_candidates SET cache_key = ? WHERE id = ?", updates)


def _drop_duplicate_answers(conn: sqlite3.Connection) -> None:
    """Collapse repeats of the same read onto the newest.

    Two runs of one configuration over one crop are the same fact recorded
    twice, and `load_outcomes` already resolved them by taking the last. Making
    that a uniqueness constraint means the count of answers finally equals the
    count of things read — it did not, and every rate computed over them was
    reading some entries twice.
    """
    conn.execute(
        """
        DELETE FROM ocr_candidates
         WHERE cache_key IS NOT NULL
           AND id NOT IN (SELECT MAX(id) FROM ocr_candidates
                           WHERE cache_key IS NOT NULL GROUP BY cache_key)
        """
    )


def _rebuild_without_cascade(conn: sqlite3.Connection) -> None:
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='ocr_candidates'"
    ).fetchone()
    if not sql or "ON DELETE CASCADE" not in sql[0]:
        return

    # Inside the migration's own transaction; foreign_keys cannot be toggled
    # there, so the rebuild relies on ocr_characters referencing candidate ids
    # that are copied through unchanged.
    conn.execute(_NEW_TABLE)
    conn.execute(
        """
        INSERT INTO ocr_candidates_new
            (id, region_id, source_region_id, ocr_run_id, crop_key, model_key,
             cache_key, transcription, confidence, raw_json, created_at)
        SELECT id, region_id, source_region_id, ocr_run_id, crop_key, model_key,
               cache_key, transcription, confidence, raw_json, created_at
          FROM ocr_candidates
        """
    )
    conn.execute("DROP TABLE ocr_candidates")
    conn.execute("ALTER TABLE ocr_candidates_new RENAME TO ocr_candidates")


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )
