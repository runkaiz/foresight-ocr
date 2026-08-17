"""Reading and changing regions.

The only writer. Two things are centralised here rather than left to callers:

* **State follows the edit.** Moving a box makes the region `adjusted`, which is
  what stops a later automatic pass from quietly moving it back. A caller that
  forgot would produce a region the machine believes it still owns.
* **The geometry address is recomputed with the geometry.** They are written in
  one statement so no row can exist whose hash describes a box it no longer has
  — every cache decision downstream trusts that pairing.

Every mutation returns what it replaced. Nothing consumes that yet; undo will,
and by then the server will have forgotten the pre-image unless it was handed
back at the time.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

from .model import Region, RegionState, geometry_hash, new_region_uid


@dataclass
class GeometryEdit:
    """A completed geometry change, and the inverse that would undo it."""

    region: Region
    previous_bbox: list[float]
    previous_state: str
    previous_geometry_hash: str

    @property
    def undo(self) -> dict[str, Any]:
        return {
            "op": "update_region",
            "region_uid": self.region.region_uid,
            "bbox": self.previous_bbox,
            "state": self.previous_state,
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _region(row: sqlite3.Row) -> Region:
    return Region(
        id=row["id"],
        region_uid=row["region_uid"],
        document_id=row["document_id"],
        page_index=row["page_index"],
        bbox=json.loads(row["bbox_json"]),
        geometry_hash=row["geometry_hash"],
        band_label=row["band_label"],
        band_ordinal=row["band_ordinal"],
        reading_order=row["reading_order"],
        reading_order_locked=bool(row["reading_order_locked"]),
        entry_index=row["entry_index"],
        role=row["role"],
        state=row["state"],
        created_by=row["created_by"],
        transform_id=row["transform_id"],
        orig_quad=json.loads(row["orig_quad_json"]) if row["orig_quad_json"] else None,
        deleted_at=row["deleted_at"],
    )


_SELECT = """
SELECT id, region_uid, document_id, page_index, bbox_json, geometry_hash,
       transform_id, orig_quad_json, band_label, band_ordinal, reading_order,
       reading_order_locked, entry_index, role, state, created_by, deleted_at
  FROM regions
"""


def get(conn: sqlite3.Connection, region_uid: str) -> Region | None:
    row = conn.execute(_SELECT + " WHERE region_uid = ?", (region_uid,)).fetchone()
    return _region(row) if row else None


def get_many(conn: sqlite3.Connection, region_uids: Sequence[str]) -> list[Region]:
    if not region_uids:
        return []
    marks = ",".join("?" * len(region_uids))
    rows = conn.execute(
        _SELECT + f" WHERE region_uid IN ({marks})", tuple(region_uids)
    ).fetchall()
    by_uid = {r["region_uid"]: _region(r) for r in rows}
    return [by_uid[u] for u in region_uids if u in by_uid]


def for_page(
    conn: sqlite3.Connection,
    document_id: str,
    page_index: int,
    *,
    include_deleted: bool = False,
) -> list[Region]:
    """Regions of a page, in reading order within each band."""
    clause = "" if include_deleted else " AND deleted_at IS NULL"
    rows = conn.execute(
        _SELECT + f" WHERE document_id = ? AND page_index = ?{clause}"
        " ORDER BY band_ordinal, reading_order",
        (document_id, page_index),
    ).fetchall()
    return [_region(r) for r in rows]


def set_geometry(
    conn: sqlite3.Connection,
    region_uid: str,
    bbox: Sequence[float],
    *,
    actor: str = "local",
    state: str = RegionState.ADJUSTED,
) -> GeometryEdit:
    """Move or resize a region, and record that a person did it.

    Reading order is deliberately untouched. Where a box sits and where it comes
    in the sequence are different facts about it, and the lattice conflated them
    — dragging an entry left of its neighbour used to be indistinguishable from
    reordering the two.
    """
    before = get(conn, region_uid)
    if before is None:
        raise KeyError(f"no region {region_uid}")

    box = [float(v) for v in bbox]
    digest = geometry_hash(before.document_id, before.page_index, box)
    conn.execute(
        "UPDATE regions SET bbox_json = ?, geometry_hash = ?, state = ?, "
        "updated_by = ?, updated_at = ? WHERE region_uid = ?",
        (json.dumps(box), digest, state, actor, _now(), region_uid),
    )
    after = get(conn, region_uid)
    assert after is not None
    return GeometryEdit(
        region=after,
        previous_bbox=before.bbox,
        previous_state=before.state,
        previous_geometry_hash=before.geometry_hash,
    )


# --------------------------------------------------------------------------
# What an automatic pass is allowed to do.
#
# Separate entry points from `set_geometry` on purpose. A machine pass leaves
# `state` alone, so a region it proposes stays proposed and a region a person
# adjusted is never reached by these at all — the caller decides that, and the
# decision is easier to audit when the two kinds of write do not share a
# function with a `state` argument that could be passed by mistake.


def create_proposed(
    conn: sqlite3.Connection,
    document_id: str,
    page_index: int,
    bbox: Sequence[float],
    *,
    band_label: str | None,
    band_ordinal: int | None,
    reading_order: int,
    entry_index: int | None,
    role: str = "entry",
    transform_id: str | None = None,
    orig_quad: list[list[float]] | None = None,
    run_id: int | None = None,
) -> Region:
    box = [float(v) for v in bbox]
    uid = new_region_uid()
    now = _now()
    conn.execute(
        "INSERT INTO regions (region_uid, document_id, page_index, bbox_json, "
        "geometry_hash, transform_id, orig_quad_json, band_label, band_ordinal, "
        "reading_order, entry_index, role, state, created_by, origin_run_id, "
        "last_run_id, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'proposed','machine',?,?,?,?)",
        (
            uid, document_id, page_index, json.dumps(box),
            geometry_hash(document_id, page_index, box), transform_id,
            json.dumps(orig_quad) if orig_quad is not None else None,
            band_label, band_ordinal, reading_order, entry_index, role,
            run_id, run_id, now, now,
        ),
    )
    region = get(conn, uid)
    assert region is not None
    return region


def reposition(
    conn: sqlite3.Connection,
    region: Region,
    bbox: Sequence[float],
    *,
    entry_index: int | None = None,
    band_ordinal: int | None = None,
    orig_quad: list[list[float]] | None = None,
    transform_id: str | None = None,
    run_id: int | None = None,
) -> Region:
    """Move a region the machine still owns, keeping its identity and state."""
    box = [float(v) for v in bbox]
    conn.execute(
        "UPDATE regions SET bbox_json = ?, geometry_hash = ?, entry_index = ?, "
        "band_ordinal = COALESCE(?, band_ordinal), "
        "orig_quad_json = COALESCE(?, orig_quad_json), "
        "transform_id = COALESCE(?, transform_id), "
        "last_run_id = ?, updated_at = ?, deleted_at = NULL "
        "WHERE region_uid = ?",
        (
            json.dumps(box),
            geometry_hash(region.document_id, region.page_index, box),
            entry_index if entry_index is not None else region.entry_index,
            band_ordinal,
            json.dumps(orig_quad) if orig_quad is not None else None,
            transform_id, run_id, _now(), region.region_uid,
        ),
    )
    updated = get(conn, region.region_uid)
    assert updated is not None
    return updated


def retire(conn: sqlite3.Connection, region: Region, *, run_id: int | None = None) -> None:
    """Withdraw a proposal nothing supports any more.

    A soft delete, because the answers read from it and any human reading of it
    are evidence about pixels that existed. If a later pass proposes the same
    box again the row comes back with all of it still attached.
    """
    conn.execute(
        "UPDATE regions SET deleted_at = ?, last_run_id = ?, updated_at = ? "
        "WHERE region_uid = ?",
        (_now(), run_id, _now(), region.region_uid),
    )


def renumber_band(
    conn: sqlite3.Connection, document_id: str, page_index: int, band_label: str | None
) -> bool:
    """Densely renumber one band right-to-left. Returns False if it is locked.

    Reading order is rewritten from geometry only while nobody has said
    otherwise. Once a person has ordered a band, a later automatic pass must not
    quietly restore the order they rejected — it appends and leaves a finding.
    """
    clause = "band_label IS NULL" if band_label is None else "band_label = ?"
    params: list[Any] = [document_id, page_index]
    if band_label is not None:
        params.append(band_label)

    rows = conn.execute(
        f"SELECT region_uid, reading_order_locked, bbox_json FROM regions "
        f"WHERE document_id = ? AND page_index = ? AND {clause} "
        f"AND deleted_at IS NULL",
        params,
    ).fetchall()
    if not rows:
        return True
    if any(r["reading_order_locked"] for r in rows):
        return False

    ordered = sorted(rows, key=lambda r: -json.loads(r["bbox_json"])[2])
    conn.executemany(
        "UPDATE regions SET reading_order = ? WHERE region_uid = ?",
        [(i, r["region_uid"]) for i, r in enumerate(ordered)],
    )
    return True
