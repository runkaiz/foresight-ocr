"""Printed rule detection.

The 雁序圖 pages are ruled: an outer printed box plus interior horizontal rules
separating the three generation bands. Those rules are the strongest geometric
signal on the page — far stronger than the text — so every layout decision is
anchored to them rather than to character positions.

Lines are fitted, not thresholded into row indices, because pages carry up to a
couple of degrees of scan skew.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from familyocr.imaging.variants import flatten_background, ink_channel


@dataclass
class Line:
    """A fitted straight rule in image coordinates.

    Horizontal lines are stored as y = slope * x + intercept.
    Vertical lines are stored as x = slope * y + intercept.
    """

    orientation: str          # "h" or "v"
    slope: float
    intercept: float
    extent: float             # length covered along the free axis, in pixels
    coverage: float           # extent / image dimension
    thickness: float
    residual: float           # RMS distance of member pixels from the fit
    start: float = 0.0        # first coordinate along the free axis
    end: float = 0.0          # last coordinate along the free axis

    def at(self, t: float) -> float:
        return self.slope * t + self.intercept

    @property
    def angle_deg(self) -> float:
        return float(np.degrees(np.arctan(self.slope)))

    def position(self, size: int) -> float:
        """Representative coordinate: the line's value at the image midpoint."""
        return self.at(size / 2.0)


def ink_mask(image: np.ndarray, block: int = 51, C: int = 12) -> np.ndarray:
    """Binary ink mask (255 = ink) from the watermark-suppressed channel."""
    gray = ink_channel(image) if image.ndim == 3 else image
    flat = flatten_background(gray)
    return cv2.adaptiveThreshold(
        flat, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, block, C
    )


def _fit_component(ys: np.ndarray, xs: np.ndarray, orientation: str) -> Line | None:
    if orientation == "h":
        free, dep = xs.astype(np.float64), ys.astype(np.float64)
        size_free = free.max() - free.min()
    else:
        free, dep = ys.astype(np.float64), xs.astype(np.float64)
        size_free = free.max() - free.min()
    if size_free < 2:
        return None
    slope, intercept = np.polyfit(free, dep, 1)
    pred = slope * free + intercept
    residual = float(np.sqrt(np.mean((dep - pred) ** 2)))
    thickness = float(len(free) / max(size_free, 1.0))
    return Line(
        orientation=orientation,
        slope=float(slope),
        intercept=float(intercept),
        extent=float(size_free),
        coverage=0.0,
        thickness=thickness,
        residual=residual,
        start=float(free.min()),
        end=float(free.max()),
    )


def detect_rules(
    image: np.ndarray,
    orientation: str,
    min_coverage: float = 0.35,
    max_thickness_ratio: float = 0.006,
) -> list[Line]:
    """Find long thin printed rules of the given orientation.

    Two filters, and both are load-bearing:

    *Length* — morphological opening with a long 1-D kernel keeps only strokes
    running continuously for a large fraction of the page.

    *Thinness* — in vertical Chinese text a column of characters is itself
    vertically continuous, so length alone cannot separate a printed rule from a
    text column. Printed rules are around 0.1% of the page in their short
    dimension; text columns are twenty times that. `max_thickness_ratio` is
    measured against the perpendicular page dimension.
    """
    mask = ink_mask(image)
    h, w = mask.shape[:2]
    span = w if orientation == "h" else h

    # Bridge first, then open. The band separators on these pages are printed as
    # broken/dotted rules; opening before closing deletes them entirely, which is
    # what a naive long-kernel open gets wrong on this corpus.
    blen = max(int(span * 0.04), 5)
    bridge = cv2.getStructuringElement(
        cv2.MORPH_RECT, (blen, 1) if orientation == "h" else (1, blen)
    )
    closed = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, bridge)

    # The opening kernel is deliberately much shorter than the length a rule
    # must ultimately have: a long straight kernel cannot follow a skewed rule
    # and erases it (a 1-degree tilt lifts a rule off a long horizontal kernel
    # entirely). Real length is enforced afterwards on the connected component,
    # which follows the skew for free.
    klen = max(int(span * min_coverage * 0.25), 15)
    ksize = (klen, 1) if orientation == "h" else (1, klen)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, ksize)
    opened = cv2.morphologyEx(closed, cv2.MORPH_OPEN, kernel)

    perpendicular = h if orientation == "h" else w
    max_thickness = perpendicular * max_thickness_ratio

    n, labels, stats, _ = cv2.connectedComponentsWithStats(opened, connectivity=8)
    lines: list[Line] = []
    for i in range(1, n):
        x, y, cw, ch, area = stats[i]
        extent = cw if orientation == "h" else ch
        # Mean thickness from area, not the bounding box: a skewed thin rule has
        # a fat bounding box but stays thin in area terms.
        thick = area / max(extent, 1)
        if extent < span * min_coverage or thick > max_thickness:
            continue
        ys, xs = np.nonzero(labels == i)
        line = _fit_component(ys, xs, orientation)
        if line is None:
            continue
        # Undo the gap-bridging dilation at the ends. Closing grows each end by
        # half the bridge kernel and the erode half cannot always take it back,
        # which otherwise reports every rule as ~blen px longer than it is. That
        # bias is invisible for positions but not for endpoints, and endpoints
        # are used to locate the page's left and right borders.
        half = blen / 2.0
        line.start += half
        line.end -= half
        line.extent = max(line.end - line.start, 0.0)
        line.coverage = float(line.extent / span)
        if line.extent < span * min_coverage:
            continue
        lines.append(line)

    size = w if orientation == "h" else h
    lines.sort(key=lambda ln: ln.position(size))
    return lines


def merge_close(lines: list[Line], size: int, tol: float = 8.0) -> list[Line]:
    """Collapse rules whose fitted positions sit within `tol` pixels.

    A thick printed rule can survive morphology as two parallel components; they
    are one physical line and must not be double-counted as band boundaries.
    """
    merged: list[Line] = []
    for line in sorted(lines, key=lambda ln: ln.position(size)):
        if merged and abs(line.position(size) - merged[-1].position(size)) <= tol:
            if line.coverage > merged[-1].coverage:
                merged[-1] = line
            continue
        merged.append(line)
    return merged
