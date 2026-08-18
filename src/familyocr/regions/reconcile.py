"""Matching a fresh machine proposal against the regions already on a page.

`segment` used to delete the page's regions and insert new ones. That threw
away identity — the OCR answers, and any correction keyed to them, belonged to
rows that no longer existed — and it treated a rerun that changed nothing as
indistinguishable from one that changed everything.

Reconciling instead asks, for each proposed column, *which region is this?* The
answer decides everything else:

    the same box                 → the region is unchanged; nothing is written,
                                   so its crops and answers stay current
    a nearby box, still proposed → the same region, moved; identity survives
    a nearby box, but adjusted   → the person's box wins. The disagreement is
       or verified                 recorded so it can be shown, not resolved
    no box at all                 → a new proposal
    a region nothing proposes     → withdrawn if the machine owned it; kept and
                                    reported if a person did

Matching is on the horizontal interval, not on the lattice index. Entry indices
shift by one whenever the comb's phase moves or the text extent changes, which
is the most common difference between two segmentations of this corpus — keying
on them would rename every column to the left of a single inserted one, which is
the failure the region table exists to prevent. Bands are full-width strips, so
x is the only coordinate carrying information, and a 1-D overlap is both the
cheapest and the most faithful measure available.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Sequence

from . import store
from .model import Region, RegionState, geometry_hash

#: Two boxes are the same column when they overlap this much of their union.
#:
#: For equal-width boxes this tolerates a drift of a third of their width before
#: identity is given up. Measured against 丙辰庶富教1 (201 pages, 1065 columns in
#: the first 60): an unchanged rerun matches every column exactly, a drift of
#: 2-20 px keeps every identity, and identity is only lost around half an entry
#: pitch — which is the point where a boundary genuinely stops belonging to this
#: column and starts belonging to its neighbour. Beyond that the match correctly
#: re-forms on the neighbour, because that is what the box now describes.
#:
#: So this is a cliff edge rather than a tuning knob, and the cliff sits where
#: the ambiguity actually is.
MATCH_IOU = 0.5


@dataclass
class Proposal:
    """One column a segmentation pass believes is on the page."""

    band_ordinal: int
    band_label: str | None
    entry_index: int
    bbox: list[float]
    orig_quad: list[list[float]] | None = None
    transform_id: str | None = None
    role: str = "entry"


@dataclass
class ReconcileReport:
    page_index: int
    unchanged: int = 0
    moved: int = 0
    created: int = 0
    revived: int = 0
    retired: int = 0
    refused: int = 0     # proposals answered by a column a person rejected
    divergent: list[str] = field(default_factory=list)   # region_uids
    orphaned: list[str] = field(default_factory=list)    # region_uids
    order_locked: list[str] = field(default_factory=list)  # band labels

    #: (band_ordinal, entry_index) -> regions.id, for every proposal handled.
    #: This is how a crop row finds the region it renders; deriving it later by
    #: lattice position would reintroduce exactly the positional lookup that
    #: matching on geometry exists to avoid.
    links: dict[tuple[int, int], int] = field(default_factory=dict)

    @property
    def touched(self) -> int:
        return self.moved + self.created + self.revived + self.retired

    def summary(self) -> str:
        return (
            f"p{self.page_index}: {self.unchanged} unchanged, {self.moved} moved, "
            f"{self.created} new, {self.revived} restored, {self.retired} withdrawn, "
            f"{self.refused} refused, {len(self.divergent)} divergent, "
            f"{len(self.orphaned)} orphaned"
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def interval_iou(a: Sequence[float], b: Sequence[float]) -> float:
    """Overlap of two boxes' horizontal extents, over their union."""
    a0, a1 = float(a[0]), float(a[2])
    b0, b1 = float(b[0]), float(b[2])
    lo, hi = max(a0, b0), min(a1, b1)
    overlap = max(hi - lo, 0.0)
    union = max(a1, b1) - min(a0, b0)
    return overlap / union if union > 0 else 0.0


def pair_up(
    proposals: list[Proposal],
    regions: list[Region],
    document_id: str,
    page_index: int,
) -> tuple[list[tuple[Proposal, Region, float]], list[Proposal], list[Region]]:
    """One-to-one matching: exact geometry first, then best overlap.

    Exact matches are taken before any overlap is considered, so a rerun that
    changed nothing cannot be talked out of recognising itself by a neighbour
    that happens to overlap slightly more.
    """
    matched: list[tuple[Proposal, Region, float]] = []
    free_regions = {r.region_uid: r for r in regions}

    by_hash: dict[str, list[Region]] = {}
    for region in regions:
        by_hash.setdefault(region.geometry_hash, []).append(region)

    remaining: list[Proposal] = []
    for proposal in proposals:
        digest = geometry_hash(document_id, page_index, proposal.bbox)
        candidates = [
            r for r in by_hash.get(digest, []) if r.region_uid in free_regions
        ]
        if candidates:
            region = candidates[0]
            del free_regions[region.region_uid]
            matched.append((proposal, region, 1.0))
        else:
            remaining.append(proposal)

    scored = sorted(
        (
            (interval_iou(p.bbox, r.bbox), i, r.region_uid)
            for i, p in enumerate(remaining)
            for r in free_regions.values()
        ),
        key=lambda t: -t[0],
    )
    taken_proposals: set[int] = set()
    for score, i, uid in scored:
        if score < MATCH_IOU:
            break
        if i in taken_proposals or uid not in free_regions:
            continue
        taken_proposals.add(i)
        matched.append((remaining[i], free_regions.pop(uid), score))

    unmatched_proposals = [p for i, p in enumerate(remaining) if i not in taken_proposals]
    return matched, unmatched_proposals, list(free_regions.values())


def reconcile_page(
    conn: sqlite3.Connection,
    document_id: str,
    page_index: int,
    proposals: Iterable[Proposal],
    *,
    run_id: int | None = None,
    dry_run: bool = False,
) -> ReconcileReport:
    """Bring a page's regions into line with what segmentation now proposes.

    `proposals` is the whole page. A band with no proposal is a band the
    detector says has no columns, and its regions are withdrawn accordingly —
    passing a subset would quietly retire everything else on the page.
    """
    report = ReconcileReport(page_index=page_index)
    proposals = list(proposals)

    # Tombstones take part in matching: a column that reappears should come back
    # as itself, with its answers, rather than as a stranger with the same box.
    #
    # Rejected regions take part too, and this is not the same thing. They are
    # never revived — a person said the column is not there — but they still have
    # to absorb the proposal that names their box, or the detector would insert a
    # fresh region beside the rejection on every run and the reviewer would be
    # deleting the same column for the rest of the volume.
    existing = store.for_page(conn, document_id, page_index, include_deleted=True)

    bands = {p.band_ordinal for p in proposals} | {
        r.band_ordinal for r in existing if r.band_ordinal is not None
    }
    for band in sorted(b for b in bands if b is not None):
        _reconcile_band(
            conn, document_id, page_index,
            [p for p in proposals if p.band_ordinal == band],
            [r for r in existing if r.band_ordinal == band],
            report, run_id, dry_run,
        )

    if not dry_run:
        for label in {r.band_label for r in existing} | {p.band_label for p in proposals}:
            if not store.renumber_band(conn, document_id, page_index, label):
                report.order_locked.append(label or "—")
    return report


def _reconcile_band(
    conn, document_id, page_index, proposals, regions, report, run_id, dry_run
) -> None:
    matched, unmatched_proposals, unmatched_regions = pair_up(
        proposals, regions, document_id, page_index
    )

    for proposal, region, score in matched:
        if region.state == RegionState.REJECTED:
            # The proposal is answered, not applied: the box is accounted for, so
            # nothing new is inserted, and nothing comes back.
            report.refused += 1
            continue
        if region.id is not None:
            report.links[(proposal.band_ordinal, proposal.entry_index)] = region.id
        if region.pinned:
            # The person's geometry stands. What the machine now believes is
            # recorded beside it so the editor can offer the change rather than
            # make it — and so that a detector which has genuinely improved is
            # visible instead of silently ignored.
            report.divergent.append(region.region_uid)
            if not dry_run and region.geometry_hash != geometry_hash(
                document_id, page_index, proposal.bbox
            ):
                _record_divergence(
                    conn, document_id, page_index, region, proposal, score, run_id
                )
            continue

        digest = geometry_hash(document_id, page_index, proposal.bbox)
        if region.geometry_hash == digest and region.deleted_at is None:
            report.unchanged += 1
            continue

        if region.deleted_at is not None:
            report.revived += 1
        else:
            report.moved += 1
        if not dry_run:
            store.reposition(
                conn, region, proposal.bbox,
                entry_index=proposal.entry_index,
                band_ordinal=proposal.band_ordinal,
                orig_quad=proposal.orig_quad,
                transform_id=proposal.transform_id,
                run_id=run_id,
            )

    for proposal in unmatched_proposals:
        report.created += 1
        if not dry_run:
            created = store.create_proposed(
                conn, document_id, page_index, proposal.bbox,
                band_label=proposal.band_label,
                band_ordinal=proposal.band_ordinal,
                reading_order=proposal.entry_index,
                entry_index=proposal.entry_index,
                role=proposal.role,
                transform_id=proposal.transform_id,
                orig_quad=proposal.orig_quad,
                run_id=run_id,
            )
            if created.id is not None:
                report.links[(proposal.band_ordinal, proposal.entry_index)] = created.id

    for region in unmatched_regions:
        if region.deleted_at is not None or region.state == RegionState.REJECTED:
            continue                      # already withdrawn; leave it that way
        if region.pinned:
            # Nothing proposed it, but a person said it is there. They are the
            # reason this row exists; the absence is the finding.
            report.orphaned.append(region.region_uid)
            if not dry_run:
                _record_orphan(conn, document_id, page_index, region, run_id)
            continue
        report.retired += 1
        if not dry_run:
            store.retire(conn, region, run_id=run_id)


def _record_divergence(conn, document_id, page_index, region, proposal, iou, run_id):
    conn.execute(
        "INSERT INTO region_proposals (run_id, document_id, page_index, band_label, "
        "region_id, bbox_json, geometry_hash, entry_index, kind, iou, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,'divergence',?,?)",
        (
            run_id, document_id, page_index, proposal.band_label, region.id,
            json.dumps(proposal.bbox),
            geometry_hash(document_id, page_index, proposal.bbox),
            proposal.entry_index, iou, _now(),
        ),
    )


def _record_orphan(conn, document_id, page_index, region, run_id):
    conn.execute(
        "INSERT INTO region_proposals (run_id, document_id, page_index, band_label, "
        "region_id, bbox_json, geometry_hash, entry_index, kind, iou, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,'orphan_region',NULL,?)",
        (
            run_id, document_id, page_index, region.band_label, region.id,
            json.dumps(region.bbox), region.geometry_hash, region.entry_index, _now(),
        ),
    )
