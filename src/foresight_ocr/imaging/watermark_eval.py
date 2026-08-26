"""Quantifying watermark suppression without ground truth.

There is no clean copy of these pages to compare against, so the evaluation is
built from pixel sets defined on the *original* colour page:

    W  watermark-only  — strong chroma, not dark: stamp over bare paper
    I  ink-under-stamp — strong chroma and dark: the strokes we must not lose
    C  ink-clean       — near-neutral and dark: ink nowhere near the stamp
    B  background      — near-neutral and bright: bare paper

A good variant drives W up to paper level (the stamp disappears) while keeping
I nearly as dark as C (the ink underneath survives). Those two goals pull in
opposite directions, which is exactly why this needs measuring rather than
eyeballing: erasing the stamp's bounding box scores perfectly on the first and
catastrophically on the second.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from foresight_ocr.imaging.variants import chroma_mask, ink_channel


@dataclass
class PixelSets:
    watermark_only: np.ndarray
    ink_under: np.ndarray
    ink_clean: np.ndarray
    background: np.ndarray

    def counts(self) -> dict[str, int]:
        return {
            "watermark_only": int(self.watermark_only.sum()),
            "ink_under": int(self.ink_under.sum()),
            "ink_clean": int(self.ink_clean.sum()),
            "background": int(self.background.sum()),
        }


def build_pixel_sets(
    bgr: np.ndarray,
    chroma_threshold: int = 18,
    ink_percentile: float = 12.0,
) -> PixelSets:
    """Partition a page into the four evaluation sets described above."""
    chroma = chroma_mask(bgr, threshold=chroma_threshold, dilate=0) > 0
    ink_gray = ink_channel(bgr)
    # Ink threshold from the page's own distribution: absolute grey levels drift
    # with paper tone across the book.
    dark_level = float(np.percentile(ink_gray, ink_percentile))
    bright_level = float(np.percentile(ink_gray, 75))
    dark = ink_gray <= dark_level
    bright = ink_gray >= bright_level

    return PixelSets(
        watermark_only=chroma & ~dark,
        ink_under=chroma & dark,
        ink_clean=~chroma & dark,
        background=~chroma & bright,
    )


@dataclass
class VariantScore:
    variant: str
    paper_level: float  # mean value over B
    watermark_level: float  # mean value over W
    watermark_residual: float  # paper_level - watermark_level; 0 is perfect
    ink_under_level: float  # mean value over I
    ink_clean_level: float  # mean value over C
    ink_contrast_under: float  # paper_level - ink_under_level; higher is better
    ink_contrast_clean: float  # paper_level - ink_clean_level; the reference
    ink_retention: float  # contrast_under / contrast_clean; 1.0 is perfect

    def as_row(self) -> list[str]:
        return [
            self.variant,
            f"{self.watermark_residual:.1f}",
            f"{self.ink_contrast_under:.1f}",
            f"{self.ink_contrast_clean:.1f}",
            f"{self.ink_retention:.3f}",
        ]


def score_variant(variant_gray: np.ndarray, sets: PixelSets, name: str) -> VariantScore:
    def mean(mask: np.ndarray) -> float:
        if not mask.any():
            return float("nan")
        return float(variant_gray[mask].mean())

    paper = mean(sets.background)
    wm = mean(sets.watermark_only)
    under = mean(sets.ink_under)
    clean = mean(sets.ink_clean)
    contrast_under = paper - under
    contrast_clean = paper - clean
    return VariantScore(
        variant=name,
        paper_level=paper,
        watermark_level=wm,
        watermark_residual=paper - wm,
        ink_under_level=under,
        ink_clean_level=clean,
        ink_contrast_under=contrast_under,
        ink_contrast_clean=contrast_clean,
        ink_retention=(
            float(contrast_under / contrast_clean) if contrast_clean else float("nan")
        ),
    )


def watermark_bbox(bgr: np.ndarray, pad: int = 40) -> tuple[int, int, int, int] | None:
    """Bounding box of the largest chroma blob — used to pick sample crops."""
    mask = chroma_mask(bgr, dilate=9)
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if n < 2:
        return None
    idx = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    x, y, w, h, _ = stats[idx]
    H, W = mask.shape[:2]
    x0, y0 = max(x - pad, 0), max(y - pad, 0)
    x1, y1 = min(x + w + pad, W), min(y + h + pad, H)
    return x0, y0, x1 - x0, y1 - y0


def comparison_strip(
    crops: list[tuple[str, np.ndarray]], height: int = 420
) -> np.ndarray:
    """Label and tile variant crops side by side for visual comparison."""
    if not crops:
        raise ValueError("no crops to compare")
    tiles = []
    for name, img in crops:
        if img.ndim == 2:
            img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
        scale = height / img.shape[0]
        img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        strip = np.full((img.shape[0] + 26, img.shape[1], 3), 250, dtype=np.uint8)
        strip[26:] = img
        cv2.putText(
            strip,
            name,
            (6, 18),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            (20, 20, 20),
            1,
            cv2.LINE_AA,
        )
        tiles.append(strip)
    width = sum(t.shape[1] for t in tiles)
    out = np.full((tiles[0].shape[0], width, 3), 250, dtype=np.uint8)
    x = 0
    for t in tiles:
        out[:, x : x + t.shape[1]] = t
        x += t.shape[1]
    return out
