"""Regions: the unit of the document a person can edit.

A `source_regions` row records where a crop was cut. That is not the same thing
as what the user owns, and the difference is why this table exists:

  * Three `source_regions` rows describe one column, because `context` is a crop
    width (tight/medium/full) rather than a different region. Storing state on
    them means keeping three rows consistent through every edit, and that bug is
    already live — `verify-layout --mark-headers` marked the tight rows only.
  * Their identity is an autoincrement id plus a positional `crop_id`
    (`<doc>_p0001_b0_e02_tight`). Inserting one column renumbers every column to
    its left, so the name of a thing changes when its neighbour changes.
  * They hang off `physical_entries -> bands -> page_layouts`, and page_layouts
    is scoped to a processing run. A region the user has verified must not be
    reachable only through the run that first proposed it.

So identity and content-address are separated, deliberately:

    region_uid      opaque, random, permanent          — this is the thing
    geometry_hash   changes with the box               — content address
    crop_key        changes with the pixels            — see ocr/cache.py
    cache_key       changes with pixels or model       — see ocr/cache.py

Nothing derived from a region's own content can serve as its identity, because
identity has to survive the user changing that content.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import Sequence


class RegionState:
    """How much human judgement a region carries.

    The ordering matters: automatic processing may rewrite PROPOSED geometry
    freely, must ask before touching ADJUSTED, and must never silently replace
    VERIFIED. REJECTED is a region a human said is not there — re-proposing it
    every run would be the machine arguing with the reviewer.
    """

    PROPOSED = "proposed"
    ADJUSTED = "adjusted"
    VERIFIED = "verified"
    REJECTED = "rejected"

    ALL = (PROPOSED, ADJUSTED, VERIFIED, REJECTED)
    #: States automatic processing must not overwrite without consent.
    PINNED = (ADJUSTED, VERIFIED)


class RegionRole:
    """What kind of thing is printed here.

    `header` exists because the 庶字第 / 富字第 band headings are cut as entries
    by the lattice and are not people; `ignore` lets a reviewer silence a region
    without deleting the evidence that the detector proposed it.
    """

    ENTRY = "entry"
    HEADER = "header"
    MARGINALIA = "marginalia"
    IGNORE = "ignore"

    ALL = (ENTRY, HEADER, MARGINALIA, IGNORE)


def new_region_uid() -> str:
    """A fresh opaque identity.

    Random rather than derived. Every deterministic scheme available here —
    hashing the geometry, or composing (page, band, index) — hashes something
    the user is about to edit, which is exactly how `crop_id` became unusable as
    a key.
    """
    return uuid.uuid4().hex


def geometry_hash(document_id: str, page_index: int, bbox: Sequence[float]) -> str:
    """Content address of a region's box, in canonical page coordinates.

    Excludes the transform id on purpose. A transform affects *pixels*, and
    pixels are addressed at crop level (`ocr/cache.py::crop_key`). Folding it in
    here would mean an idempotent `normalize` re-run churned every region row in
    the document — there have already been fourteen normalize runs on one
    volume, and none of them moved a box.

    Rounded to 0.01 px so that a float that survived a JSON round-trip with a
    different last bit does not read as a moved region.
    """
    x0, y0, x1, y1 = (float(v) for v in bbox)
    raw = f"{document_id}|{page_index}|{x0:.2f}|{y0:.2f}|{x1:.2f}|{y1:.2f}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass
class Region:
    """One editable region.

    `entry_index` is retained but advisory: it is the lattice index the machine
    last assigned, useful for debugging and for re-attaching a correction stored
    under the old positional key. Reading order is `reading_order`, which the
    user can change without moving anything.
    """

    region_uid: str
    document_id: str
    page_index: int
    bbox: list[float]                 # [x0, y0, x1, y1] in canonical space
    geometry_hash: str
    band_label: str | None = None
    band_ordinal: int | None = None
    reading_order: int = 0
    reading_order_locked: bool = False
    entry_index: int | None = None
    role: str = RegionRole.ENTRY
    state: str = RegionState.PROPOSED
    created_by: str = "machine"
    transform_id: str | None = None
    orig_quad: list[list[float]] | None = None
    id: int | None = None
    deleted_at: str | None = None

    @property
    def live(self) -> bool:
        return self.deleted_at is None and self.state != RegionState.REJECTED

    @property
    def pinned(self) -> bool:
        """True when automatic processing may not rewrite this geometry."""
        return self.state in RegionState.PINNED

    def with_bbox(self, bbox: Sequence[float]) -> "Region":
        """A copy moved to `bbox`, with the content address recomputed."""
        box = [float(v) for v in bbox]
        return Region(
            **{
                **self.__dict__,
                "bbox": box,
                "geometry_hash": geometry_hash(self.document_id, self.page_index, box),
            }
        )
