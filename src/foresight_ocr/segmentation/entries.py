"""Entry segmentation on normalized pages.

Entries are laid out on a near-constant pitch, so the reliable construction is a
pitch-spaced lattice seeded at the text edge and then *snapped* to whichever
detected gutters are nearby. Pure gutter-splitting breaks wherever two entries
touch or an entry is blank; pure pitch-stepping drifts across a page. Snapping
keeps the regularity of the lattice and the local accuracy of the gutters.

Reading order is right to left, so entry 0 is the rightmost column.

Three crop widths are emitted per entry because the spec calls for benchmarking
context size against OCR quality, and a crop tight enough to cut a neighbouring
glyph can remove exactly the context a recognizer needs to resolve it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import numpy as np

# name -> horizontal padding as a multiple of the entry pitch
CONTEXTS: dict[str, float] = {
    "tight": 0.0,
    "medium": 0.35,
    "full": 0.75,
}


@dataclass
class EntryRegion:
    page_index: int
    band_index: int
    entry_index: int  # 0 = rightmost, following reading order
    context: str
    x0: float
    y0: float
    x1: float
    y1: float
    snapped: bool  # whether a detected gutter anchored this edge
    role: str = "entry"  # entry | header; headers are never people

    @property
    def bbox(self) -> list[float]:
        return [self.x0, self.y0, self.x1, self.y1]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _snap(value: float, edges: list[float], tol: float) -> tuple[float, bool]:
    """Move `value` onto the nearest detected gutter within `tol`."""
    if not edges:
        return value, False
    arr = np.asarray(edges, dtype=np.float64)
    i = int(np.argmin(np.abs(arr - value)))
    if abs(arr[i] - value) <= tol:
        return float(arr[i]), True
    return value, False


def fit_comb(
    text_left: float,
    text_right: float,
    pitch: float,
    gutters: list[float],
    tol_frac: float = 0.12,
) -> tuple[float, int]:
    """Best phase for a comb of period `pitch`, and how many gutters it explains.

    Chosen over stepping left gutter-by-gutter because a single wrong snap used
    to shift every boundary after it. The ink profile yields more gutters than
    there are entries — gaps inside an annotation column look the same as gaps
    between entries — and a greedy walk cannot tell which is which. Fitting one
    global phase lets the *majority* of gutters outvote the spurious ones.
    """
    if pitch <= 0:
        return text_right, 0
    tol = pitch * tol_frac
    arr = np.asarray(gutters, dtype=np.float64)
    best_phase, best_hits = text_right, -1
    # Phase is only meaningful modulo the pitch; half-pixel steps are plenty.
    steps = max(int(pitch * 2), 1)
    for i in range(steps):
        phase = text_right - i * (pitch / steps)
        if arr.size:
            offsets = np.abs((arr - phase) % pitch)
            offsets = np.minimum(offsets, pitch - offsets)
            hits = int((offsets <= tol).sum())
        else:
            hits = 0
        if hits > best_hits:
            best_phase, best_hits = phase, hits
    return best_phase, best_hits


def entry_boundaries(
    text_left: float,
    text_right: float,
    pitch: float,
    gutters: list[float],
    snap_tol_frac: float = 0.12,
    *,
    phase_offset: float = 0.0,
    snap: bool = True,
) -> tuple[list[float], list[bool]]:
    """Right-to-left lattice of entry boundaries on a fitted comb.

    `phase_offset` shifts every boundary together, in pixels. The fit is a vote
    among detected gutters, and on a page where too few were detected the
    majority can be wrong — every column then gets cut through its own id
    instead of between two. One number moves the whole lattice, which is the
    repair that matches the damage; dragging each box separately would be seven
    edits for one mistake.

    `snap=False` keeps the lattice perfectly regular. Snapping buys local
    accuracy from the gutters and is right when they are trustworthy; when they
    are the reason the page is wrong, it is the thing to switch off.
    """
    if pitch <= 0:
        return [], []
    tol = pitch * snap_tol_frac
    phase, _ = fit_comb(text_left, text_right, pitch, gutters, snap_tol_frac)
    phase += phase_offset

    # The fitted phase can land short of the right-hand text edge, so extend the
    # comb rightwards first. Walking only leftwards from the phase silently drops
    # the rightmost entry of the band — and a missing entry is worse than a
    # spurious one, because the sequence check reports it as a gap in the record
    # rather than as something to look at.
    start = phase
    # Step until the topmost boundary reaches the right edge. Requiring a whole
    # further pitch to *fit* below text_right stops one boundary short, which
    # costs the band its last entry; the final entry is allowed to run past the
    # text edge and is clamped to the page when the crop is cut.
    while start < text_right - tol:
        start += pitch

    positions: list[float] = []
    x = start
    while x > text_left - tol:
        positions.append(x)
        x -= pitch

    bounds: list[float] = []
    snapped: list[bool] = []
    # Walk the comb, not the gutters. Each boundary may nudge onto a nearby
    # gutter for local accuracy, but the nudge never moves the comb itself, so
    # one bad gutter can no longer drag the rest of the page with it.
    for pos in positions:
        snapped_x, ok = _snap(pos, gutters, tol) if snap else (pos, False)
        bounds.append(snapped_x)
        snapped.append(ok)
    return bounds, snapped


def anchored_phase_offset(
    text_left: float,
    text_right: float,
    pitch: float,
    gutters: list[float],
    phase_offset: float,
    phase_anchor_x: float | None,
    snap_tol_frac: float = 0.12,
    *,
    snap: bool = True,
) -> float:
    """Choose the entry phase that agrees with the rest of the volume.

    A genealogy entry contains two printed sub-columns, so the ink profile
    normally exposes two convincing families of gutters half an entry apart.
    Damage can make the minority family win on one page even though both have
    equally plausible spacing.  ``phase_anchor_x`` is the corpus median of the
    first interior boundary at the right edge.  Comparing the fitted phase with
    its half-pitch opposite lets the other scans outvote one torn page without
    discarding that page's useful local gutter positions.

    The supplied offset remains the first choice on an exact tie.  This keeps
    documents without a decisive corpus signal byte-for-byte compatible with
    the older fit.
    """
    if phase_anchor_x is None or pitch <= 0:
        return phase_offset

    choices = (phase_offset, phase_offset + pitch / 2.0)
    scored: list[tuple[float, int, float]] = []
    for order, candidate in enumerate(choices):
        bounds, _ = entry_boundaries(
            text_left,
            text_right,
            pitch,
            gutters,
            snap_tol_frac,
            phase_offset=candidate,
            snap=snap,
        )
        # Boundaries are emitted from right to left.  The first is the outer
        # edge (usually beyond the page and later clamped); the second is the
        # stable, visible boundary between the edge fragment and first complete
        # entry, which is the comparable landmark across scans.
        distance = abs(bounds[1] - phase_anchor_x) if len(bounds) > 1 else float("inf")
        scored.append((distance, order, candidate))
    return min(scored)[2]


def infer_entry_phase_anchor(
    geometries: list[dict[str, Any]],
    corpus_pitch: float,
    corpus_text_left: float,
    corpus_text_right: float,
    phase_fraction: float,
) -> float | None:
    """Learn the volume's right-edge entry boundary from its regular pages.

    The verified phase fraction establishes which of the two typographic
    orientations is correct for the book.  The median then turns that decision
    into an absolute canonical-space landmark; a handful of pages whose gutter
    vote flipped cannot drag it to the opposite family.
    """
    anchors: list[float] = []
    for geometry in geometries:
        try:
            pitch, left, right, _ = resolve_comb(
                geometry,
                corpus_pitch,
                corpus_text_left,
                corpus_text_right,
            )
            bounds, _ = entry_boundaries(
                left,
                right,
                pitch,
                [float(v) for v in geometry.get("column_edges", [])],
                phase_offset=pitch * phase_fraction,
            )
        except (KeyError, TypeError, ValueError):
            continue
        if len(bounds) > 1:
            anchors.append(float(bounds[1]))
    return float(np.median(anchors)) if anchors else None


def resolve_comb(
    geometry: dict[str, Any],
    corpus_pitch: float,
    corpus_text_left: float,
    corpus_text_right: float,
) -> tuple[float, float, float, bool]:
    """Which pitch and text extent a page's lattice is built on.

    Shared by the batch pass and the editor so a re-cut with no adjustment
    reproduces the boxes already on record. Two priors are applied:

    * Autocorrelation can lock onto half the true period on a noisy page, which
      would cut every entry in two, so a weak or wildly different per-page
      estimate defers to the corpus.
    * The lattice domain comes from the template rather than this page's ink
      extent. Pages are normalized to a common frame, so the grid is a property
      of the frame; a faint rightmost column pulls the measured edge inward by
      less than one pitch and the band loses its last entry entirely.
    """
    page_pitch = float(geometry["column_pitch"])
    used_corpus = (
        float(geometry.get("pitch_confidence", 0.0)) < 0.3
        or abs(page_pitch - corpus_pitch) > 0.1 * corpus_pitch
    )
    if used_corpus:
        page_pitch = corpus_pitch
    return (
        page_pitch,
        min(float(geometry["text_left"]), corpus_text_left),
        max(float(geometry["text_right"]), corpus_text_right),
        used_corpus,
    )


def segment_page(
    page_index: int,
    bands: list[tuple[int, float, float]],  # (band_index, top, bottom)
    column_edges: list[float],
    pitch: float,
    text_left: float,
    text_right: float,
    page_width: int,
    contexts: dict[str, float] | None = None,
    *,
    phase_offset: float = 0.0,
    phase_anchor_x: float | None = None,
    snap: bool = True,
) -> list[EntryRegion]:
    """Produce entry regions for every band on one normalized page."""
    contexts = contexts or CONTEXTS
    phase_offset = anchored_phase_offset(
        text_left,
        text_right,
        pitch,
        column_edges,
        phase_offset,
        phase_anchor_x,
        snap=snap,
    )
    bounds, snapped = entry_boundaries(
        text_left,
        text_right,
        pitch,
        column_edges,
        phase_offset=phase_offset,
        snap=snap,
    )
    if len(bounds) < 2:
        return []

    regions: list[EntryRegion] = []
    for band_index, top, bottom in bands:
        for i in range(len(bounds) - 1):
            right, left = bounds[i], bounds[i + 1]
            was_snapped = snapped[i] and snapped[i + 1]
            for name, pad_frac in contexts.items():
                pad = pitch * pad_frac
                regions.append(
                    EntryRegion(
                        page_index=page_index,
                        band_index=band_index,
                        entry_index=i,
                        context=name,
                        x0=max(left - pad, 0.0),
                        y0=top,
                        x1=min(right + pad, float(page_width)),
                        y1=bottom,
                        snapped=was_snapped,
                    )
                )
    return regions


def segment_structured_page(
    page_index: int,
    bands: list[tuple[int, float, float]],
    override: dict[str, Any],
    page_width: int,
    contexts: dict[str, float] | None = None,
) -> list[EntryRegion]:
    """Segment a ruled title page whose columns do not share one pitch.

    The first chart page can carry four different structures side by side: a
    page-wide book title, one generation heading per horizontal band, one
    ``X字第`` section title per band, and the people themselves.  A periodic
    comb necessarily merges each pair of narrow headings with its neighbour.

    ``override`` is explicit document geometry, normally copied from the
    detected ruled lines into ``template_<document>.yaml``.  It contains:

    ``entry_boundaries``
        Right-to-left x coordinates enclosing only people.
    ``band_headers``
        Header columns repeated in every band, each with ``x0``/``x1``.
    ``page_headers``
        Page-level header boxes with explicit x/y coordinates.

    Header crops never receive contextual padding.  Padding a narrow title is
    exactly how ``庶字第`` acquired ``允一 長子`` in the same OCR answer.
    """
    contexts = contexts or CONTEXTS
    bounds = [float(x) for x in override.get("entry_boundaries", [])]
    if len(bounds) < 2:
        return []
    if any(a <= b for a, b in zip(bounds, bounds[1:], strict=False)):
        raise ValueError("structured page entry_boundaries must run right to left")

    regions: list[EntryRegion] = []

    def add(
        band_index: int,
        entry_index: int,
        bbox: tuple[float, float, float, float],
        *,
        role: str,
        pad: bool,
    ) -> None:
        x0, y0, x1, y1 = bbox
        for name, pad_frac in contexts.items():
            amount = (bounds[0] - bounds[1]) * pad_frac if pad else 0.0
            regions.append(
                EntryRegion(
                    page_index=page_index,
                    band_index=band_index,
                    entry_index=entry_index,
                    context=name,
                    x0=max(x0 - amount, 0.0),
                    y0=y0,
                    x1=min(x1 + amount, float(page_width)),
                    y1=y1,
                    snapped=True,
                    role=role,
                )
            )

    for band_index, top, bottom in bands:
        for i, (right, left) in enumerate(zip(bounds, bounds[1:], strict=False)):
            add(
                band_index,
                i,
                (left, top, right, bottom),
                role="entry",
                pad=True,
            )

        # Negative indices keep structural headings outside the person's
        # 0-based reading order while remaining stable correction keys.
        for i, header in enumerate(override.get("band_headers", []), 1):
            add(
                band_index,
                -i,
                (float(header["x0"]), top, float(header["x1"]), bottom),
                role="header",
                pad=False,
            )

    for i, header in enumerate(override.get("page_headers", []), 1):
        add(
            int(header.get("band_index", bands[0][0] if bands else 0)),
            -100 - i,
            (
                float(header["x0"]),
                float(header.get("y0", 0.0)),
                float(header["x1"]),
                float(header["y1"]),
            ),
            role="header",
            pad=False,
        )
    return regions


def to_original_quad(
    bbox: list[float], inverse: list[list[float]]
) -> list[list[float]]:
    """Map a canonical bbox back to its quadrilateral in original pixels."""
    import cv2

    x0, y0, x1, y1 = bbox
    pts = np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], dtype=np.float64).reshape(
        -1, 1, 2
    )
    out = cv2.perspectiveTransform(pts, np.array(inverse, dtype=np.float64))
    return [[float(p[0]), float(p[1])] for p in out.reshape(-1, 2)]
