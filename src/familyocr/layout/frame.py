"""Outer page-frame detection and the transform to canonical coordinates.

Frame detection runs on a downscaled copy for speed, then rescales the fitted
lines back to full resolution — the fit is a straight line, so scaling is exact
and no accuracy is lost.

Border selection happens in two stages. The first pass takes the outermost
detected rules, which is right whenever all four borders survive the scan. On
this corpus a border is often faded or cropped away, and the outermost-rule rule
then latches onto an interior rule and silently produces a frame ~300 px narrow.
The second pass (`refit_with_prior`) therefore re-selects borders using the
corpus median frame size as a prior, and marks an edge as inferred when it truly
is not on the page.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict, field
from typing import Any

import cv2
import numpy as np

from familyocr.layout.lines import Line, detect_rules, merge_close


@dataclass
class FrameFit:
    corners: list[list[float]]      # TL, TR, BR, BL in original image coordinates
    skew_deg: float                 # rotation implied by the horizontal rules
    width: float                    # mean of top/bottom edge lengths
    height: float                   # mean of left/right edge lengths
    rect_error: float               # px deviation of the quad from a rectangle
    line_residual: float            # RMS of member pixels about the fitted rules
    interior_h: list[float]         # interior horizontal rule positions (page y)
    detected_edges: int             # how many of the 4 borders were really found
    ok: bool
    reason: str = ""
    inferred_edges: list[str] = field(default_factory=list)
    h_candidates: list[Line] = field(default_factory=list)
    v_candidates: list[Line] = field(default_factory=list)

    def to_dict(self, with_candidates: bool = False) -> dict[str, Any]:
        d = asdict(self)
        if not with_candidates:
            d.pop("h_candidates", None)
            d.pop("v_candidates", None)
        return d


def _intersect(h: Line, v: Line) -> tuple[float, float]:
    """Intersection of y = a*x + b with x = c*y + d."""
    a, b = h.slope, h.intercept
    c, d = v.slope, v.intercept
    denom = 1.0 - a * c
    if abs(denom) < 1e-9:
        raise ValueError("degenerate frame: rules are parallel")
    y = (a * d + b) / denom
    x = c * y + d
    return float(x), float(y)


def _scale_line(line: Line, factor: float) -> Line:
    """Rescale a fitted line from the downscaled image back to full resolution.

    Slope is scale-invariant under uniform scaling; only the intercept moves.
    """
    return Line(
        orientation=line.orientation,
        slope=line.slope,
        intercept=line.intercept * factor,
        extent=line.extent * factor,
        coverage=line.coverage,
        thickness=line.thickness * factor,
        residual=line.residual * factor,
        start=line.start * factor,
        end=line.end * factor,
    )


def _offset_line(line: Line, delta: float) -> Line:
    """A parallel copy of `line` shifted by `delta` along its dependent axis."""
    return Line(
        orientation=line.orientation,
        slope=line.slope,
        intercept=line.intercept + delta,
        extent=line.extent,
        coverage=0.0,
        thickness=line.thickness,
        residual=line.residual,
        start=line.start,
        end=line.end,
    )


def _borders_from_rule_extents(
    h_lines: list[Line], min_coverage: float = 0.85
) -> tuple[Line, Line] | None:
    """Derive the left and right borders from where the horizontal rules stop.

    On these pages the top rule, the two band separators and the bottom rule all
    run border to border, so their endpoints *are* the frame's vertical edges.
    That makes the horizontal rules — which survive on almost every page — a far
    better source for the left/right borders than vertical-rule detection, which
    has to compete with columns of vertical text and frequently loses.

    Needs at least two full-width rules at different heights, so the fitted
    border carries the page's skew rather than being forced perfectly upright.
    """
    full = [ln for ln in h_lines if ln.coverage >= min_coverage]
    if len(full) < 2:
        return None

    ys = np.array([ln.intercept + ln.slope * (ln.start + ln.end) / 2 for ln in full])
    starts = np.array([ln.start for ln in full])
    ends = np.array([ln.end for ln in full])

    def _fit(xs: np.ndarray) -> Line:
        # x = c*y + d, so the border tilts with the page instead of assuming 90°.
        c, d = np.polyfit(ys, xs, 1)
        resid = float(np.sqrt(np.mean((xs - (c * ys + d)) ** 2)))
        return Line(
            orientation="v", slope=float(c), intercept=float(d),
            extent=float(ys.max() - ys.min()), coverage=1.0,
            thickness=1.0, residual=resid,
            start=float(ys.min()), end=float(ys.max()),
        )

    return _fit(starts), _fit(ends)


def find_candidates(
    image: np.ndarray,
    downscale: int = 4,
    min_coverage: float = 0.45,
) -> tuple[list[Line], list[Line]]:
    """All plausible printed rules, in full-resolution coordinates."""
    small = cv2.resize(
        image, None, fx=1.0 / downscale, fy=1.0 / downscale, interpolation=cv2.INTER_AREA
    )
    sh, sw = small.shape[:2]
    h_lines = merge_close(detect_rules(small, "h", min_coverage=min_coverage), sw, tol=6)
    v_lines = merge_close(detect_rules(small, "v", min_coverage=min_coverage), sh, tol=6)
    f = float(downscale)
    return [_scale_line(ln, f) for ln in h_lines], [_scale_line(ln, f) for ln in v_lines]


def _assemble(
    top: Line, bottom: Line, left: Line, right: Line,
    h_lines: list[Line], image_shape: tuple[int, ...],
    inferred: list[str],
) -> FrameFit:
    ih, iw = image_shape[:2]
    try:
        tl = _intersect(top, left)
        tr = _intersect(top, right)
        br = _intersect(bottom, right)
        bl = _intersect(bottom, left)
    except ValueError as exc:
        return FrameFit([], 0.0, 0.0, 0.0, float("inf"), float("inf"), [], 4,
                        False, str(exc))

    quad = np.array([tl, tr, br, bl], dtype=np.float64)
    width = float(
        (np.linalg.norm(quad[1] - quad[0]) + np.linalg.norm(quad[2] - quad[3])) / 2
    )
    height = float(
        (np.linalg.norm(quad[3] - quad[0]) + np.linalg.norm(quad[2] - quad[1])) / 2
    )
    # Unequal diagonals mean the quad is not a rectangle under any rigid motion.
    rect_error = float(
        abs(np.linalg.norm(quad[2] - quad[0]) - np.linalg.norm(quad[3] - quad[1]))
    )
    skew = float(np.degrees(np.arctan((top.slope + bottom.slope) / 2.0)))
    residual = float(
        np.mean([top.residual, bottom.residual, left.residual, right.residual])
    )

    top_y, bottom_y = top.position(iw), bottom.position(iw)
    interior = [
        ln.position(iw) for ln in h_lines
        if top_y + 20 < ln.position(iw) < bottom_y - 20
    ]

    plausible = (
        0.5 * iw < width < 1.02 * iw
        and 0.5 * ih < height < 1.02 * ih
        and rect_error < 0.05 * max(width, height)
    )
    return FrameFit(
        corners=[list(map(float, p)) for p in (tl, tr, br, bl)],
        skew_deg=skew,
        width=width,
        height=height,
        rect_error=rect_error,
        line_residual=residual,
        interior_h=interior,
        detected_edges=4 - len(inferred),
        ok=bool(plausible),
        reason="" if plausible else "frame geometry implausible for a full page",
        inferred_edges=inferred,
    )


def detect_frame(
    image: np.ndarray,
    downscale: int = 4,
    min_coverage: float = 0.45,
) -> FrameFit:
    """First-pass fit: outermost detected rules. Returns ok=False, never raises."""
    h_lines, v_lines = find_candidates(image, downscale, min_coverage)
    if len(h_lines) < 2 or len(v_lines) < 2:
        fit = FrameFit(
            [], 0.0, 0.0, 0.0, float("inf"), float("inf"), [],
            len(h_lines) + len(v_lines), False,
            f"only {len(h_lines)} horizontal / {len(v_lines)} vertical rules",
        )
    else:
        fit = _assemble(h_lines[0], h_lines[-1], v_lines[0], v_lines[-1],
                        h_lines, image.shape, [])
    fit.h_candidates = h_lines
    fit.v_candidates = v_lines
    return fit


def _best_pair(
    lines: list[Line], target: float, size: int, tol: float
) -> tuple[Line, Line] | None:
    """Pick the pair of rules whose separation best matches `target`.

    Ties on separation are broken by total coverage, so a well-inked border beats
    a barely-detected fragment that happens to sit at the same distance.
    """
    best: tuple[float, tuple[Line, Line]] | None = None
    for i in range(len(lines)):
        for j in range(i + 1, len(lines)):
            a, b = lines[i], lines[j]
            sep = abs(b.position(size) - a.position(size))
            err = abs(sep - target)
            if err > tol:
                continue
            score = err - 2.0 * (a.coverage + b.coverage)
            if best is None or score < best[0]:
                best = (score, (a, b))
    return best[1] if best else None


def _infer_pair(
    lines: list[Line],
    target: float,
    size: int,
    span: int,
    sides: tuple[str, str],
) -> tuple[tuple[Line, Line] | None, str]:
    """Place a missing border `target` away from a surviving one.

    Only the outermost rules are considered as anchors: an interior rule can
    easily be the longest thing on a damaged page, and anchoring on it throws the
    inferred border clean off the scan. Whichever option keeps both borders on
    the page wins; ties go to the better-inked anchor.
    """
    if not lines:
        return None, ""
    ordered = sorted(lines, key=lambda ln: ln.position(size))
    margin = 0.03 * span
    options: list[tuple[float, tuple[Line, Line], str]] = []

    first, last = ordered[0], ordered[-1]
    # Treat the outermost rule as the near border and project the far one.
    for anchor, delta, side in (
        (first, target, sides[1]),
        (last, -target, sides[0]),
    ):
        partner = _offset_line(anchor, delta)
        lo = min(anchor.position(size), partner.position(size))
        hi = max(anchor.position(size), partner.position(size))
        if lo < -margin or hi > span + margin:
            continue
        options.append((-anchor.coverage, (anchor, partner), side))

    if not options:
        return None, ""
    options.sort(key=lambda o: o[0])
    _, pair, side = options[0]
    return pair, side


def refit_with_prior(
    image_shape: tuple[int, ...],
    fit: FrameFit,
    target_w: float,
    target_h: float,
    tol_frac: float = 0.02,
) -> FrameFit:
    """Re-select borders using the corpus median frame size as a prior.

    Falls back to inferring a missing edge from its opposite number plus the
    target size. An inferred edge is recorded in `inferred_edges` so the review
    stage can treat those pages as lower confidence rather than as clean fits.
    """
    ih, iw = image_shape[:2]
    h_lines, v_lines = fit.h_candidates, fit.v_candidates
    inferred: list[str] = []

    vpair = _best_pair(v_lines, target_w, ih, tol_frac * target_w)
    if vpair is None:
        # Rule endpoints before geometric guessing: they are measured from the
        # page, not projected from a corpus statistic.
        extent_pair = _borders_from_rule_extents(h_lines)
        if extent_pair is not None:
            sep = abs(extent_pair[1].position(ih) - extent_pair[0].position(ih))
            if abs(sep - target_w) <= 2.5 * tol_frac * target_w:
                vpair = extent_pair
    if vpair is None:
        vpair, side = _infer_pair(v_lines, target_w, ih, iw, ("left", "right"))
        if vpair is None:
            return FrameFit([], 0.0, 0.0, 0.0, float("inf"), float("inf"), [],
                            0, False, "no vertical rule can anchor the frame")
        inferred.append(side)
    left, right = sorted(vpair, key=lambda ln: ln.position(ih))

    hpair = _best_pair(h_lines, target_h, iw, tol_frac * target_h)
    if hpair is None:
        hpair, side = _infer_pair(h_lines, target_h, iw, ih, ("top", "bottom"))
        if hpair is None:
            return FrameFit([], 0.0, 0.0, 0.0, float("inf"), float("inf"), [],
                            0, False, "no horizontal rule can anchor the frame")
        inferred.append(side)
    top, bottom = sorted(hpair, key=lambda ln: ln.position(iw))

    refit = _assemble(top, bottom, left, right, h_lines, image_shape, inferred)
    refit.h_candidates = h_lines
    refit.v_candidates = v_lines
    return refit


def homography(fit: FrameFit, canonical_w: int, canonical_h: int) -> np.ndarray:
    """Forward transform: original pixels -> canonical page space."""
    src = np.array(fit.corners, dtype=np.float32)
    dst = np.array(
        [[0, 0], [canonical_w, 0], [canonical_w, canonical_h], [0, canonical_h]],
        dtype=np.float32,
    )
    return cv2.getPerspectiveTransform(src, dst)


def apply_points(H: np.ndarray, pts: np.ndarray) -> np.ndarray:
    """Map Nx2 points through a homography."""
    pts = np.asarray(pts, dtype=np.float64).reshape(-1, 1, 2)
    out = cv2.perspectiveTransform(pts, H.astype(np.float64))
    return out.reshape(-1, 2)
