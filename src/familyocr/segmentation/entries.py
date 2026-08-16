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

from dataclasses import dataclass, asdict
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
    entry_index: int          # 0 = rightmost, following reading order
    context: str
    x0: float
    y0: float
    x1: float
    y1: float
    snapped: bool             # whether a detected gutter anchored this edge

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
) -> tuple[list[float], list[bool]]:
    """Right-to-left lattice of entry boundaries on a fitted comb."""
    if pitch <= 0:
        return [], []
    tol = pitch * snap_tol_frac
    phase, _ = fit_comb(text_left, text_right, pitch, gutters, snap_tol_frac)

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
        snapped_x, ok = _snap(pos, gutters, tol)
        bounds.append(snapped_x)
        snapped.append(ok)
    return bounds, snapped


def segment_page(
    page_index: int,
    bands: list[tuple[int, float, float]],   # (band_index, top, bottom)
    column_edges: list[float],
    pitch: float,
    text_left: float,
    text_right: float,
    page_width: int,
    contexts: dict[str, float] | None = None,
) -> list[EntryRegion]:
    """Produce entry regions for every band on one normalized page."""
    contexts = contexts or CONTEXTS
    bounds, snapped = entry_boundaries(text_left, text_right, pitch, column_edges)
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


def to_original_quad(bbox: list[float], inverse: list[list[float]]) -> list[list[float]]:
    """Map a canonical bbox back to its quadrilateral in original pixels."""
    import cv2

    x0, y0, x1, y1 = bbox
    pts = np.array(
        [[x0, y0], [x1, y0], [x1, y1], [x0, y1]], dtype=np.float64
    ).reshape(-1, 1, 2)
    out = cv2.perspectiveTransform(pts, np.array(inverse, dtype=np.float64))
    return [[float(p[0]), float(p[1])] for p in out.reshape(-1, 2)]
