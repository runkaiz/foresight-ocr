"""Layout verification.

Segmentation is the foundation everything else rests on: if entries are cut
wrongly, OCR accuracy and the sequence checksum are both measuring the wrong
thing, and no amount of downstream care recovers it. These checks exist so that
"we never verified the layout" cannot be true again.

The lattice emits a fixed number of entries per band by construction, which
means a uniform count is *not* evidence that the count is right — it is exactly
what a wrong lattice would also produce. So the checks are chosen to be
falsifiable independently of the lattice:

- **phantom entries** — a box the lattice invented contains no ink
- **edge concentration** — a lattice that pads pages fails at index 0 or the
  last index far more than in the middle
- **id completeness** — the corpus numbers its own entries, so the observed id
  range says how many entries should exist, independently of how many were cut
- **header contamination** — a section header column read as if it were a person
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

HEADER_RE = re.compile("字第|第[一二三四五六七八九十百]+世|雁序圖|宗譜")


@dataclass
class LayoutVerification:
    pages_expected: int
    pages_segmented: int
    pages_missing: list[int]
    entries: int
    entries_per_page: dict[int, int]
    phantom_crops: list[str]
    ink_percentiles: dict[str, float]
    unparsed_by_position: dict[int, int]
    edge_bias: float
    header_entries: list[dict[str, Any]]
    id_ranges: dict[str, dict[str, Any]]
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def check_phantoms(crop_paths: list[Path], threshold: float = 0.02
                   ) -> tuple[list[str], dict[str, float]]:
    """Crops with essentially no ink — boxes the lattice invented."""
    import cv2
    import numpy as np

    coverage: list[tuple[float, str]] = []
    for path in crop_paths:
        gray = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if gray is None:
            continue
        # Threshold relative to the crop's own tone: paper brightness drifts
        # across the volume and a fixed cutoff would mis-score faded pages.
        ink = float((gray < np.percentile(gray, 50) - 25).mean())
        coverage.append((ink, str(path)))
    if not coverage:
        return [], {}
    arr = np.array([c[0] for c in coverage])
    pct = {
        "p1": float(np.percentile(arr, 1)),
        "p5": float(np.percentile(arr, 5)),
        "median": float(np.median(arr)),
        "p95": float(np.percentile(arr, 95)),
    }
    return [p for ink, p in coverage if ink < threshold], pct


def edge_bias(unparsed_by_position: dict[int, int]) -> float:
    """How much more often the outermost entries fail than the inner ones.

    A lattice padding pages with entries that are not there fails hardest at the
    edges. 1.0 means no bias; values well above 1 point at padding.
    """
    if not unparsed_by_position:
        return 1.0
    positions = sorted(unparsed_by_position)
    if len(positions) < 3:
        return 1.0
    first, last = positions[0], positions[-1]
    edge = unparsed_by_position.get(first, 0) + unparsed_by_position.get(last, 0)
    inner = [unparsed_by_position.get(p, 0) for p in positions[1:-1]]
    if not inner or sum(inner) == 0:
        return float("inf") if edge else 1.0
    return (edge / 2) / (sum(inner) / len(inner))
