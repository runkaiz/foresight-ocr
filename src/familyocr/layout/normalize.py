"""Two-pass geometric normalization.

Pass 1 fits a frame on every page. Pass 2 defines the canonical coordinate space
from the *median* frame across the corpus and warps each page into it.

The median matters: defining canonical space from page 1 would bake one page's
scan displacement into every downstream coordinate, and it is also how a page
whose left border was missed (so the 版心 rule got picked up as the frame edge)
would go unnoticed. Compared against the corpus median, that page stands out as
~300 px narrow and gets flagged instead of silently normalized.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from familyocr.layout.frame import FrameFit, detect_frame, homography


@dataclass
class FramePass:
    page_index: int
    fit: FrameFit
    path: Path


@dataclass
class CanonicalSpace:
    width: int
    height: int
    median_width: float
    median_height: float
    width_mad: float
    height_mad: float
    interior_positions: list[float] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "width": self.width,
            "height": self.height,
            "median_width": self.median_width,
            "median_height": self.median_height,
            "width_mad": self.width_mad,
            "height_mad": self.height_mad,
            "interior_positions": self.interior_positions,
        }


def _mad(values: np.ndarray) -> float:
    """Median absolute deviation — robust to the outlier pages we expect."""
    med = float(np.median(values))
    return float(np.median(np.abs(values - med)))


def build_canonical_space(passes: list[FramePass]) -> CanonicalSpace:
    good = [p for p in passes if p.fit.ok]
    if not good:
        raise RuntimeError("no page produced a usable frame fit")
    # Pages with an inferred edge have a width equal to the prior by
    # construction. Including them would shrink the reported spread and make the
    # measurement look better than it is, so measure on fully-detected frames
    # whenever there are enough of them.
    measured = [p for p in good if not p.fit.inferred_edges]
    if len(measured) >= 20:
        good = measured
    widths = np.array([p.fit.width for p in good])
    heights = np.array([p.fit.height for p in good])
    mw, mh = float(np.median(widths)), float(np.median(heights))
    return CanonicalSpace(
        width=int(round(mw)),
        height=int(round(mh)),
        median_width=mw,
        median_height=mh,
        width_mad=_mad(widths),
        height_mad=_mad(heights),
    )


@dataclass
class PageNormalization:
    page_index: int
    ok: bool                  # usable for downstream stages
    status: str               # clean | inferred | failed
    reason: str
    skew_deg: float
    width: float
    height: float
    width_dev: float          # deviation from corpus median, in pixels
    height_dev: float
    rect_error: float
    forward: list[list[float]]
    inverse: list[list[float]]
    interior_h: list[float]

    def to_dict(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        return d


def median_frame(passes: list[FramePass]) -> list[list[float]] | None:
    """Median corner positions across pages whose frame was really detected.

    Used as a last resort for pages the detector cannot fit at all. Scans in this
    volume are aligned closely enough (frame-size MAD around 3-5 px) that the
    corpus median frame is a far better guess than nothing — but it is a guess,
    and pages normalized this way are marked `fallback` so they are never
    counted as measured.
    """
    good = [p for p in passes if p.fit.ok and p.fit.corners
            and not p.fit.inferred_edges]
    if len(good) < 20:
        return None
    quads = np.array([p.fit.corners for p in good], dtype=np.float64)
    return np.median(quads, axis=0).tolist()


def normalize_page(
    fit: FrameFit,
    space: CanonicalSpace,
    page_index: int,
    width_tol: float = 0.03,
    skew_tol_deg: float = 2.0,
) -> PageNormalization:
    """Grade a frame fit against the corpus and produce its transform pair."""
    if not fit.ok:
        return PageNormalization(
            page_index, False, "failed", fit.reason or "frame fit failed",
            fit.skew_deg, fit.width, fit.height, float("inf"), float("inf"),
            fit.rect_error, [], [], fit.interior_h,
        )

    wdev = fit.width - space.median_width
    hdev = fit.height - space.median_height
    reasons = []
    # An inferred edge means the prior placed a border the page did not show. The
    # warp stays usable — downstream template alignment is the honest test of
    # whether the guess was right — but it is a hypothesis, not a measurement,
    # and the cover page reaches a plausible-looking frame precisely this way.
    if fit.inferred_edges:
        reasons.append(f"edge inferred from prior: {', '.join(fit.inferred_edges)}")
    hard: list[str] = []
    if abs(wdev) > width_tol * space.median_width:
        hard.append(f"width off median by {wdev:+.0f}px")
    if abs(hdev) > width_tol * space.median_height:
        hard.append(f"height off median by {hdev:+.0f}px")
    if abs(fit.skew_deg) > skew_tol_deg:
        hard.append(f"skew {fit.skew_deg:.2f}deg")
    if fit.rect_error > 0.02 * space.median_width:
        hard.append(f"non-rectangular by {fit.rect_error:.0f}px")
    reasons.extend(hard)

    if hard:
        status = "failed"
    elif fit.inferred_edges:
        status = "inferred"
    else:
        status = "clean"

    H = homography(fit, space.width, space.height)
    Hinv = np.linalg.inv(H)
    return PageNormalization(
        page_index=page_index,
        ok=not hard,
        status=status,
        reason="; ".join(reasons),
        skew_deg=fit.skew_deg,
        width=fit.width,
        height=fit.height,
        width_dev=float(wdev),
        height_dev=float(hdev),
        rect_error=fit.rect_error,
        forward=H.tolist(),
        inverse=Hinv.tolist(),
        interior_h=fit.interior_h,
    )


def warp_page(image: np.ndarray, forward: list[list[float]],
              space: CanonicalSpace) -> np.ndarray:
    H = np.array(forward, dtype=np.float64)
    return cv2.warpPerspective(
        image, H, (space.width, space.height), flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REPLICATE,
    )


def roundtrip_error(norm: PageNormalization, points: np.ndarray) -> float:
    """Max px error of original -> canonical -> original for the given points."""
    H = np.array(norm.forward, dtype=np.float64)
    Hinv = np.array(norm.inverse, dtype=np.float64)
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 1, 2)
    fwd = cv2.perspectiveTransform(pts, H)
    back = cv2.perspectiveTransform(fwd, Hinv)
    return float(np.max(np.linalg.norm(back.reshape(-1, 2) - points, axis=1)))
