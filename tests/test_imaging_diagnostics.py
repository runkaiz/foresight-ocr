from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from foresight_ocr.imaging.io import read_image, write_image
from foresight_ocr.imaging.overlay import (
    contact_sheet,
    draw_frame_overlay,
    draw_grid_overlay,
)
from foresight_ocr.imaging.watermark_eval import (
    PixelSets,
    build_pixel_sets,
    comparison_strip,
    score_variant,
    watermark_bbox,
)


def test_geometry_overlays_and_contact_sheet_are_real_images(tmp_path: Path) -> None:
    gray = np.full((120, 200), 230, dtype=np.uint8)
    frame = draw_frame_overlay(
        gray,
        [[10, 10], [190, 10], [190, 110], [10, 110]],
        [40, 80],
        tmp_path / "nested" / "frame.png",
        ok=False,
        caption="fit failed\nresidual=4",
        scale=0.5,
    )
    grid = draw_grid_overlay(
        cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR),
        [40, 80],
        [50, 100, 150],
        tmp_path / "grid.png",
        caption="3 bands",
        scale=0.5,
    )

    frame_image = read_image(frame)
    grid_image = read_image(grid)
    assert frame_image is not None and frame_image.shape == (60, 100, 3)
    assert grid_image is not None and grid_image.shape == (60, 100, 3)

    sheet = contact_sheet(
        [frame, tmp_path / "missing.png", grid],
        tmp_path / "sheets" / "contact.jpg",
        cols=2,
        cell=(50, 30),
    )
    sheet_image = read_image(sheet)
    assert sheet_image is not None and sheet_image.shape == (60, 100, 3)


def test_image_io_round_trips_a_unicode_path(tmp_path: Path) -> None:
    path = tmp_path / "丙辰庶富教9" / "正規化" / "p0058.png"
    path.parent.mkdir(parents=True)
    expected = np.full((9, 7, 3), [12, 34, 56], dtype=np.uint8)

    assert write_image(path, expected)
    actual = read_image(path)

    assert actual is not None
    assert np.array_equal(actual, expected)

    with pytest.raises(ValueError, match="no images"):
        contact_sheet([], tmp_path / "empty.png")


def test_pixel_sets_and_variant_score_preserve_ink_signal() -> None:
    bgr = np.full((20, 20, 3), 240, dtype=np.uint8)
    bgr[:10, :10] = (40, 40, 40)  # clean ink
    bgr[:10, 10:] = (20, 20, 160)  # dark ink under red stamp
    bgr[10:, 10:] = (120, 120, 230)  # red stamp over paper

    sets = build_pixel_sets(bgr, chroma_threshold=10, ink_percentile=30)
    counts = sets.counts()
    assert counts["watermark_only"] > 0
    assert counts["ink_under"] > 0
    assert counts["ink_clean"] > 0
    assert counts["background"] > 0

    gray = np.array([[240, 190], [30, 40]], dtype=np.uint8)
    manual_sets = PixelSets(
        watermark_only=np.array([[False, True], [False, False]]),
        ink_under=np.array([[False, False], [True, False]]),
        ink_clean=np.array([[False, False], [False, True]]),
        background=np.array([[True, False], [False, False]]),
    )
    score = score_variant(gray, manual_sets, "candidate")
    assert score.watermark_residual == 50
    assert score.ink_contrast_under == 210
    assert score.ink_contrast_clean == 200
    assert score.ink_retention == pytest.approx(1.05)
    assert score.as_row() == ["candidate", "50.0", "210.0", "200.0", "1.050"]

    empty = np.zeros((2, 2), dtype=bool)
    empty_score = score_variant(gray, PixelSets(empty, empty, empty, empty), "empty")
    assert np.isnan(empty_score.paper_level)


def test_watermark_bbox_and_comparison_strip() -> None:
    neutral = np.full((30, 40, 3), 220, dtype=np.uint8)
    assert watermark_bbox(neutral) is None

    stamped = neutral.copy()
    stamped[10:20, 15:25] = (20, 20, 220)
    bbox = watermark_bbox(stamped, pad=2)
    assert bbox is not None
    x, y, width, height = bbox
    assert x <= 15 and y <= 10
    assert x + width >= 25 and y + height >= 20

    strip = comparison_strip(
        [("gray", np.full((10, 5), 100, dtype=np.uint8)), ("bgr", stamped)],
        height=20,
    )
    assert strip.shape[0] == 46
    assert strip.shape[2] == 3

    with pytest.raises(ValueError, match="no crops"):
        comparison_strip([])
