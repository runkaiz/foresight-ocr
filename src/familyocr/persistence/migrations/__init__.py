"""Numbered, forward-only schema migrations.

Two rules keep this honest:

* **`db.SCHEMA` owns table definitions.** It runs first, with
  `CREATE TABLE IF NOT EXISTS`, so a new database is complete before any
  migration is considered. A migration therefore never repeats a `CREATE TABLE`
  that the head schema already contains — it only does what that script cannot:
  add a column to an existing table, rebuild a constraint, backfill new rows
  from old ones.

* **`PRAGMA user_version` decides what has run.** The previous implementation
  read table SQL out of `sqlite_master` and matched substrings against it, which
  cannot distinguish "already applied" from "applied and then changed again",
  and which had to re-derive that answer on every connection. `init_schema` runs
  from every CLI command *and* every review-server HTTP request, so that check
  is on a hot path.

Each step runs inside `BEGIN IMMEDIATE`, because those per-request connections
mean two threads can arrive at an unmigrated database simultaneously.
"""

from __future__ import annotations

import sqlite3
from typing import Callable

from . import (
    m001_derived_tables,
    m002_regions,
    m003_ocr_cache,
    m004_proposal_without_a_run,
    m005_parsed_entries_by_region,
)

#: (version, apply) in order. A step is applied when `user_version` is below it.
MIGRATIONS: list[tuple[int, Callable[[sqlite3.Connection], None]]] = [
    (1, m001_derived_tables.apply),
    (2, m002_regions.apply),
    (3, m003_ocr_cache.apply),
    (4, m004_proposal_without_a_run.apply),
    (5, m005_parsed_entries_by_region.apply),
]

HEAD = MIGRATIONS[-1][0]


def current_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def apply(conn: sqlite3.Connection, *, fresh: bool = False) -> list[int]:
    """Run every migration the database has not seen. Returns those applied.

    A database created from the head schema is stamped at HEAD without running
    anything: the migrations exist to carry *old* rows forward, and there are
    none. An existing database that predates versioning is treated as version 0,
    which is safe because every step is written to be a no-op when its work is
    already present.
    """
    version = current_version(conn)
    if version == 0:
        if fresh:
            conn.execute(f"PRAGMA user_version = {HEAD}")
            conn.commit()
            return []
        # Pre-versioning database: replay from the start. m001 is idempotent.

    pending = [(target, step) for target, step in MIGRATIONS if target > version]
    if not pending:
        return []

    # Changing a foreign key means rebuilding the table, and dropping the old
    # one trips enforcement against tables that reference it. Enforcement is off
    # for the duration and the whole schema is checked once at the end, which is
    # stricter than what per-statement enforcement would have given us here.
    if conn.in_transaction:
        conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")

    applied: list[int] = []
    try:
        for target, step in pending:
            conn.execute("BEGIN IMMEDIATE")
            try:
                step(conn)
                # PRAGMA user_version cannot be parameterised.
                conn.execute(f"PRAGMA user_version = {target}")
            except Exception:
                conn.rollback()
                raise
            conn.commit()
            applied.append(target)

        broken = conn.execute("PRAGMA foreign_key_check").fetchall()
        if broken:
            raise RuntimeError(
                f"migration left {len(broken)} dangling references, first: {tuple(broken[0])}"
            )
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    return applied
