from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from foresight_ocr.layout.verify import LayoutVerification, check_phantoms, edge_bias


def test_check_phantoms_ignores_unreadable_files_and_reports_distribution(
    tmp_path: Path,
) -> None:
    blank = np.full((40, 30), 240, dtype=np.uint8)
    inked = blank.copy()
    inked[:, 10:20] = 20
    blank_path = tmp_path / "blank.png"
    inked_path = tmp_path / "inked.png"
    assert cv2.imwrite(str(blank_path), blank)
    assert cv2.imwrite(str(inked_path), inked)

    phantoms, percentiles = check_phantoms(
        [blank_path, tmp_path / "missing.png", inked_path], threshold=0.02
    )
    assert phantoms == [str(blank_path)]
    assert percentiles["p1"] <= percentiles["median"] <= percentiles["p95"]
    assert check_phantoms([tmp_path / "missing.png"]) == ([], {})


def test_edge_bias_handles_empty_short_clean_and_biased_positions() -> None:
    assert edge_bias({}) == 1.0
    assert edge_bias({0: 4, 1: 2}) == 1.0
    assert edge_bias({0: 0, 1: 0, 2: 0}) == 1.0
    assert edge_bias({0: 3, 1: 0, 2: 2}) == float("inf")
    assert edge_bias({0: 4, 1: 1, 2: 1, 3: 2}) == 3.0


def test_layout_verification_serializes_and_derives_ok() -> None:
    verification = LayoutVerification(
        pages_expected=2,
        pages_segmented=1,
        pages_missing=[2],
        entries=3,
        entries_per_page={1: 3},
        phantom_crops=["blank.png"],
        ink_percentiles={"median": 0.1},
        unparsed_by_position={0: 1},
        edge_bias=1.0,
        header_entries=[],
        id_ranges={},
    )
    assert verification.ok
    assert verification.to_dict()["pages_missing"] == [2]
    verification.problems.append("page missing")
    assert not verification.ok
