"""Re-cutting a page's columns when the lattice landed on the wrong phase.

Some pages come back with every entry wrong. Not misread — miscut: the boundary
falls through the middle of each column, so every crop holds the left half of
one entry and the right half of the next, and the recognizer dutifully reports
what it can see. On 丙辰庶富教1 page 3 that is 21 entries out of 21, against a
book-wide rate of one in five.

The cause is upstream of the recognizer and upstream of any individual box. The
comb is fitted by a vote among detected gutters; page 3 detected ten where a
clean page detects thirteen, and the majority voted for a phase half a column
off. One number is wrong, and it is wrong for the whole page at once.

So the repair is one number too. A person shifts the lattice until the
boundaries sit in the gutters, and the page is re-cut and re-read in one step.
The alternative — dragging each of seven boxes by hand and re-reading each —
is seven edits and seven model calls for a single mistake.

What the shift is *not* allowed to do is lose the page's history. Columns are
matched to the regions already there by horizontal overlap, so a shifted column
is the same region moved, keeping its identity, its earlier readings and any
correction attached to it. Only a column with nothing to match becomes new.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..project import Project
from ..segmentation.entries import entry_boundaries, resolve_comb
from . import store
from .model import RegionState
from .reconcile import Proposal, pair_up


@dataclass(frozen=True)
class Band:
    ordinal: int
    label: str | None
    top: float
    bottom: float


@dataclass(frozen=True)
class CombInputs:
    """Everything the lattice for one page is built from.

    Read once and handed to both the preview and the apply, so what a person
    sees drawn on the page is what gets cut.
    """

    document_id: str
    page_index: int
    pitch: float
    text_left: float
    text_right: float
    gutters: list[float]
    page_width: int
    page_height: int
    bands: list[Band]
    pitch_confidence: float
    used_corpus_pitch: bool
    corpus_pitch: float
    transform_id: str | None = None
    inverse: list[list[float]] | None = None


@dataclass(frozen=True)
class CombPlan:
    """A lattice a person could accept, and what it would produce."""

    page_index: int
    pitch: float
    phase_offset: float
    snap: bool
    text_left: float
    text_right: float
    boundaries: list[float]          # right to left, as the comb is walked
    snapped: list[bool]
    entries_per_band: int
    proposals: list[Proposal]

    def to_dict(self) -> dict[str, Any]:
        return {
            "page": self.page_index,
            "pitch": self.pitch,
            "phase_offset": self.phase_offset,
            "snap": self.snap,
            "text_left": self.text_left,
            "text_right": self.text_right,
            "boundaries": self.boundaries,
            "snapped": self.snapped,
            "entries_per_band": self.entries_per_band,
            "entries": len(self.proposals),
        }


@dataclass
class RecutReport:
    page_index: int
    entries_per_band: int = 0
    moved: int = 0
    unchanged: int = 0
    created: int = 0
    retired: int = 0
    crops_cut: int = 0
    read: int = 0
    reused: int = 0
    findings_cleared: int = 0
    corrections_rekeyed: int = 0
    corrections_stranded: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}

    def summary(self) -> str:
        return (
            f"p{self.page_index}: {self.entries_per_band} entries per band — "
            f"{self.unchanged} unchanged, {self.moved} moved, {self.created} new, "
            f"{self.retired} withdrawn; {self.crops_cut} crops cut, "
            f"{self.read} read, {self.reused} already known"
        )


class PageNotSegmentable(RuntimeError):
    """The page has no layout to build a lattice on."""


# --------------------------------------------------------------------------
# reading the page


def comb_inputs(
    conn: sqlite3.Connection, project: Project, document_id: str, page_index: int
) -> CombInputs:
    """The measurements this page's lattice is fitted to."""
    row = conn.execute(
        """SELECT pl.id, pl.frame_json FROM page_layouts pl
           WHERE pl.document_id = ? AND pl.page_index = ?
           ORDER BY pl.run_id DESC, pl.id DESC LIMIT 1""",
        (document_id, page_index),
    ).fetchone()
    if row is None:
        raise PageNotSegmentable(
            f"{document_id} p{page_index} has no layout; run `familyocr layout` first"
        )
    geometry = json.loads(row["frame_json"])

    template_path = project.configs / f"template_{document_id}.yaml"
    if not template_path.exists():
        raise PageNotSegmentable(f"missing {template_path}; run `familyocr layout` first")
    template = yaml.safe_load(template_path.read_text(encoding="utf-8"))

    corpus_pitch = float(template["column_pitch"])
    pitch, text_left, text_right, used_corpus = resolve_comb(
        geometry, corpus_pitch,
        float(template["text_left"]), float(template["text_right"]),
    )

    # Bands come from the layout row so their vertical extent is this page's,
    # not the corpus median: a band edge that moved is a different question from
    # a column boundary that did, and mixing them would make one repair look
    # like the other.
    from ..context import BAND_LABELS

    bands = [
        Band(
            ordinal=b["band_index"],
            label=BAND_LABELS.get(b["band_index"]),
            top=float(json.loads(b["bbox_json"])[1]),
            bottom=float(json.loads(b["bbox_json"])[3]),
        )
        for b in conn.execute(
            "SELECT band_index, bbox_json FROM bands WHERE page_layout_id = ? "
            "ORDER BY band_index",
            (row["id"],),
        )
    ]

    page_path = project.pages_dir(document_id, "normalized") / f"p{page_index:04d}.png"
    width, height = _page_size(page_path)

    tf = conn.execute(
        "SELECT id, inverse_json FROM transforms WHERE document_id = ? "
        "AND page_index = ? ORDER BY rowid DESC LIMIT 1",
        (document_id, page_index),
    ).fetchone()

    return CombInputs(
        document_id=document_id,
        page_index=page_index,
        pitch=pitch,
        text_left=text_left,
        text_right=text_right,
        gutters=[float(g) for g in geometry.get("column_edges", [])],
        page_width=width,
        page_height=height,
        bands=bands,
        pitch_confidence=float(geometry.get("pitch_confidence", 0.0)),
        used_corpus_pitch=used_corpus,
        corpus_pitch=corpus_pitch,
        transform_id=tf["id"] if tf else None,
        inverse=json.loads(tf["inverse_json"]) if tf and tf["inverse_json"] else None,
    )


def _page_size(path: Path) -> tuple[int, int]:
    if not path.exists():
        return 0, 0
    from PIL import Image

    with Image.open(path) as im:
        return im.size


# --------------------------------------------------------------------------
# proposing


def plan_comb(
    inputs: CombInputs,
    *,
    phase_offset: float = 0.0,
    pitch: float | None = None,
    snap: bool = True,
    text_left: float | None = None,
    text_right: float | None = None,
) -> CombPlan:
    """What the lattice would be at this phase, without changing anything.

    Separate from applying it because the whole value of a phase control is
    seeing where the boundaries land before paying for a re-cut and a re-read.

    Three numbers describe a lattice and each fails differently. `phase_offset`
    slides it: wrong, and every column is cut through its own text. `pitch`
    scales it: wrong, and the error accumulates across the page. `text_left` and
    `text_right` say where it stops: wrong, and the page gains a column made of
    its own margin, or loses a real one at the edge. The extent is not
    cosmetic — it comes from the corpus, so a page whose text stops short of the
    others gets an extra boundary and a sliver entry that reads as decoration.
    """
    use_pitch = float(pitch) if pitch else inputs.pitch
    left = inputs.text_left if text_left is None else float(text_left)
    right = inputs.text_right if text_right is None else float(text_right)
    bounds, snapped = entry_boundaries(
        left, right, use_pitch, inputs.gutters,
        phase_offset=phase_offset, snap=snap,
    )

    proposals: list[Proposal] = []
    if len(bounds) >= 2:
        from ..segmentation.entries import to_original_quad

        for band in inputs.bands:
            for i in range(len(bounds) - 1):
                x1, x0 = bounds[i], bounds[i + 1]
                bbox = [
                    max(x0, 0.0), band.top,
                    min(x1, float(inputs.page_width) or x1), band.bottom,
                ]
                proposals.append(
                    Proposal(
                        band_ordinal=band.ordinal,
                        band_label=band.label,
                        entry_index=i,
                        bbox=bbox,
                        orig_quad=(
                            to_original_quad(bbox, inputs.inverse)
                            if inputs.inverse else None
                        ),
                        transform_id=inputs.transform_id,
                    )
                )

    return CombPlan(
        page_index=inputs.page_index,
        pitch=use_pitch,
        phase_offset=phase_offset,
        snap=snap,
        text_left=left,
        text_right=right,
        boundaries=bounds,
        snapped=snapped,
        entries_per_band=max(len(bounds) - 1, 0),
        proposals=proposals,
    )


# --------------------------------------------------------------------------
# applying


def apply_comb(
    conn: sqlite3.Connection,
    project: Project,
    plan: CombPlan,
    inputs: CombInputs,
    *,
    actor: str = "local",
    reocr: bool = True,
    backend: str = "paddleocr_vl",
    variant: str = "maxrgb",
) -> RecutReport:
    """Move the page's regions onto this lattice, then cut and read what changed.

    The regions become `adjusted`, which is what stops a later `segment` from
    quietly putting them back where the detector wanted them. That is the whole
    point: a person looked at the page and said where the columns are.
    """
    document_id = inputs.document_id
    page_index = inputs.page_index
    report = RecutReport(page_index=page_index, entries_per_band=plan.entries_per_band)
    if not plan.proposals:
        report.errors.append("the lattice produced no columns; check the pitch")
        return report

    existing = [
        r for r in store.for_page(conn, document_id, page_index)
        if r.state != RegionState.REJECTED
    ]
    # A correction is keyed by where its entry sits, and this is about to move
    # entries. Remembering which region each one belonged to before is what lets
    # it be re-aimed afterwards instead of silently describing its neighbour.
    corrections = _corrections_by_region(conn, document_id, page_index, existing)

    touched: list[str] = []
    for band in inputs.bands:
        matched, unmatched_proposals, unmatched_regions = pair_up(
            [p for p in plan.proposals if p.band_ordinal == band.ordinal],
            [r for r in existing if r.band_ordinal == band.ordinal],
            document_id, page_index,
        )
        for proposal, region, _score in matched:
            # A shift moves some columns and leaves others exactly where they
            # were. The ones that did not move keep the answers they have:
            # re-reading unchanged pixels costs a model call to arrive back at
            # the same sentence.
            #
            # They are still marked, though. The person chose a lattice for the
            # whole page, not for the columns that happened to shift under it —
            # leaving the rest `proposed` would let a later automatic pass move
            # two thirds of the page back onto the comb they rejected and leave
            # the other third, which is worse than either lattice on its own.
            if region.bbox == [float(v) for v in proposal.bbox]:
                report.unchanged += 1
            else:
                touched.append(region.region_uid)
                report.moved += 1
            store.set_geometry(
                conn, region.region_uid, proposal.bbox,
                actor=actor, state=RegionState.ADJUSTED,
            )

        for proposal in unmatched_proposals:
            created = store.create_region(
                conn, document_id, page_index, proposal.bbox,
                band_label=proposal.band_label,
                band_ordinal=proposal.band_ordinal,
                reading_order=proposal.entry_index,
                entry_index=proposal.entry_index,
                role=proposal.role,
                transform_id=proposal.transform_id,
                orig_quad=proposal.orig_quad,
                state=RegionState.ADJUSTED,
                created_by=actor,
            )
            touched.append(created.region_uid)
            report.created += 1

        for region in unmatched_regions:
            # The person has said this column is not on the page. Rejected, not
            # withdrawn: a withdrawn proposal comes back when the detector makes
            # it again, and the detector will make this one again on every run.
            # The readings stay attached either way — they describe pixels that
            # were really there.
            store.reject(conn, region, actor=actor)
            report.retired += 1

    for label in {b.label for b in inputs.bands}:
        store.renumber_band(conn, document_id, page_index, label)

    report.corrections_rekeyed, report.corrections_stranded = _rekey_corrections(
        conn, document_id, page_index, corrections
    )
    # Findings describe entry indices that no longer name the same columns.
    # Leaving them would send the reviewer to the wrong entry with a confident
    # explanation, which is worse than saying nothing until `validate` runs
    # again. Only when something actually moved: a person who opens the control,
    # looks, and accepts what is already there should not lose the page's
    # findings for having looked.
    if report.moved or report.created or report.retired:
        report.findings_cleared = conn.execute(
            "DELETE FROM validation_findings WHERE document_id = ? AND page_index = ?",
            (document_id, page_index),
        ).rowcount
    conn.commit()

    if reocr:
        _read_page(conn, project, document_id, touched, report,
                   backend=backend, variant=variant)
    return report


def _read_page(conn, project, document_id, region_uids, report, *, backend, variant):
    """Cut and read the columns this re-cut touched.

    Every region goes through the ordinary on-demand path, so a column the shift
    happened to leave where it was costs nothing: its pixels address a crop that
    already exists and an answer already on record.
    """
    from ..ocr.ondemand import recognize_regions

    before = conn.execute("SELECT COUNT(*) n FROM region_crops").fetchone()["n"]
    answers = recognize_regions(
        conn, project, document_id, region_uids, backend=backend, variant=variant,
    )
    conn.commit()
    report.crops_cut = conn.execute(
        "SELECT COUNT(*) n FROM region_crops"
    ).fetchone()["n"] - before
    for answer in answers:
        if answer.error:
            report.errors.append(answer.error)
        elif answer.reused:
            report.reused += 1
        else:
            report.read += 1


def _corrections_by_region(
    conn: sqlite3.Connection, document_id: str, page_index: int, regions
) -> dict[str, tuple[str, int]]:
    """Which region each of this page's corrections currently describes."""
    by_key = {
        (r.band_label, r.reading_order): r.region_uid
        for r in regions
    }
    out: dict[str, tuple[str, int]] = {}
    for row in conn.execute(
        "SELECT id, band_label, entry_index FROM human_corrections "
        "WHERE document_id = ? AND page_index = ?",
        (document_id, page_index),
    ):
        uid = by_key.get((row["band_label"], row["entry_index"]))
        if uid is not None:
            out[uid] = (row["band_label"], row["id"])
    return out


def _rekey_corrections(
    conn: sqlite3.Connection,
    document_id: str,
    page_index: int,
    before: dict[str, tuple[str, int]],
) -> tuple[int, int]:
    """Point each correction back at the region it was made against.

    Returns how many were re-aimed and how many could not be: a correction whose
    column the re-cut withdrew has nowhere to go, and it is left where it is and
    reported rather than deleted or quietly reattached to a neighbour.
    """
    moved = stranded = 0
    for uid, (_band_label, correction_id) in before.items():
        region = store.get(conn, uid)
        if region is None or region.deleted_at is not None:
            stranded += 1
            continue
        cur = conn.execute(
            "UPDATE human_corrections SET band_label = ?, entry_index = ? "
            "WHERE id = ? AND (band_label IS NOT ? OR entry_index IS NOT ?)",
            (region.band_label, region.reading_order, correction_id,
             region.band_label, region.reading_order),
        )
        moved += cur.rowcount
    return moved, stranded
