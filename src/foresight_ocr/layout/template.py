"""Template discovery on normalized pages.

Nothing here declares a coordinate by hand. Band boundaries come from the printed
rules; entry columns come from the ink profile; both are then clustered across
the corpus so the template is an empirical summary with a stated spread, and the
spread is what tells us whether a template is usable at all.

Vertical Chinese text is what makes the column step legible: each entry is a
dense vertical stripe separated by a clear gutter, so a horizontal ink profile
across a band is close to a square wave whose period is the entry pitch.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from itertools import combinations
from typing import Any

import cv2
import numpy as np

from foresight_ocr.layout.lines import detect_rules, ink_mask, merge_close


@dataclass
class BandGeometry:
    index: int
    top: float
    bottom: float

    @property
    def height(self) -> float:
        return self.bottom - self.top


@dataclass
class PageStructure:
    page_index: int
    bands: list[BandGeometry]
    column_edges: list[float]  # canonical x of gutters, left to right
    column_pitch: float  # dominant entry spacing, px
    pitch_confidence: float  # autocorrelation peak strength, 0..1
    text_left: float  # first column of text (excludes the 版心 strip)
    text_right: float
    profile: list[float] = field(default_factory=list)
    band_repair: dict[str, Any] | None = None
    low_coverage_band_edges: list[float] = field(default_factory=list)

    def to_dict(self, with_profile: bool = False) -> dict[str, Any]:
        d = asdict(self)
        if not with_profile:
            d.pop("profile", None)
        return d


def find_bands(
    gray: np.ndarray, expected: int = 3, min_coverage: float = 0.55
) -> list[BandGeometry]:
    """Split a normalized page into generation bands using the printed rules."""
    h, w = gray.shape[:2]
    small = cv2.resize(gray, None, fx=0.25, fy=0.25, interpolation=cv2.INTER_AREA)
    sw = small.shape[1]
    rules = merge_close(detect_rules(small, "h", min_coverage=min_coverage), sw, tol=6)
    ys = sorted(ln.position(sw) * 4.0 for ln in rules)

    # In canonical space the frame edges sit at y=0 and y=h, so anything found
    # right at the margin is the border, not a separator.
    interior = [y for y in ys if 0.02 * h < y < 0.98 * h]
    edges = [0.0, *interior, float(h)]
    bands = [BandGeometry(i, edges[i], edges[i + 1]) for i in range(len(edges) - 1)]
    # Drop slivers produced by a doubled rule. Do not hard-truncate to
    # ``expected`` here: one of the detected rules may be a crease, and keeping
    # the extra candidate lets the corpus-template pass choose the right subset
    # instead of silently discarding the bottom of the page.
    _ = expected
    return [b for b in bands if b.height > 0.1 * h] if bands else []


def ink_profile(gray: np.ndarray, smooth: int = 9) -> np.ndarray:
    """Column-wise ink density, normalized to 0..1."""
    mask = ink_mask(gray)
    prof = mask.mean(axis=0).astype(np.float32)
    if smooth > 1:
        kernel = np.ones(smooth, dtype=np.float32) / smooth
        prof = np.convolve(prof, kernel, mode="same").astype(np.float32)
    rng = prof.max() - prof.min()
    return (prof - prof.min()) / rng if rng > 0 else prof


def dominant_pitch(
    profile: np.ndarray, lo: int = 60, hi: int = 400
) -> tuple[float, float]:
    """Entry pitch by autocorrelation of the ink profile.

    Returns (pitch_px, confidence). Confidence is the normalized height of the
    chosen autocorrelation peak, so a page whose columns are irregular reports a
    low number instead of a confident wrong pitch.
    """
    p = profile - profile.mean()
    if not np.any(p):
        return 0.0, 0.0
    ac = np.correlate(p, p, mode="full")[len(p) - 1 :]
    ac /= ac[0] if ac[0] else 1.0
    hi = min(hi, len(ac) - 1)
    if hi <= lo:
        return 0.0, 0.0
    window = ac[lo:hi]
    k = int(np.argmax(window))
    return float(lo + k), float(window[k])


def find_column_edges(
    profile: np.ndarray, pitch: float, ink_threshold: float = 0.25
) -> tuple[list[float], float, float]:
    """Locate gutters between entry columns.

    Gutters are runs of low ink density wide enough to be a real gap rather than
    the space inside a character. `pitch` sets that width so the test scales with
    the page instead of relying on a magic pixel count.
    """
    low = profile < ink_threshold
    min_gap = max(int(pitch * 0.12), 4)

    runs: list[tuple[int, int]] = []
    start = None
    for i, v in enumerate(low):
        if v and start is None:
            start = i
        elif not v and start is not None:
            if i - start >= min_gap:
                runs.append((start, i))
            start = None
    if start is not None and len(low) - start >= min_gap:
        runs.append((start, len(low)))

    if not runs:
        return [], 0.0, float(len(profile))

    edges = [float((a + b) / 2) for a, b in runs]
    inked = np.flatnonzero(profile >= ink_threshold)
    text_left = float(inked[0]) if inked.size else 0.0
    text_right = float(inked[-1]) if inked.size else float(len(profile))
    return edges, text_left, text_right


def analyse_page(
    gray: np.ndarray, page_index: int, expected_bands: int = 3
) -> PageStructure:
    bands = find_bands(gray, expected=expected_bands)
    low_coverage_edges: list[float] = []
    # A blank generation band can weaken both separators enough that the strict
    # pass sees no split at all. Probe once at lower coverage only for these
    # severe misses; the corpus matcher still has to validate every recovered
    # rule, so covers and prose pages are not turned into tables.
    if len(bands) < expected_bands - 1:
        fallback_bands = find_bands(gray, expected=expected_bands, min_coverage=0.45)
        low_coverage_edges = sorted(
            {
                float(edge)
                for band in fallback_bands
                for edge in (band.top, band.bottom)
                if 0.0 < float(edge) < float(gray.shape[0])
            }
        )
    # Profile the tallest band: it has the most ink and the cleanest gutters.
    if bands:
        tallest = max(bands, key=lambda b: b.height)
        strip = gray[int(tallest.top) : int(tallest.bottom), :]
    else:
        strip = gray
    profile = ink_profile(strip)
    pitch, conf = dominant_pitch(profile)
    edges, left, right = find_column_edges(profile, pitch or 150.0)
    return PageStructure(
        page_index=page_index,
        bands=bands,
        column_edges=edges,
        column_pitch=pitch,
        pitch_confidence=conf,
        text_left=left,
        text_right=right,
        profile=profile.tolist(),
        low_coverage_band_edges=low_coverage_edges,
    )


@dataclass
class DocumentTemplate:
    canonical_width: int
    canonical_height: int
    band_count: int
    band_edges: list[float]
    band_edge_mad: list[float]
    column_pitch: float
    column_pitch_mad: float
    text_left: float
    text_right: float
    pages_used: int
    layout_families: dict[str, list[int]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _mad(v: np.ndarray) -> float:
    return float(np.median(np.abs(v - np.median(v)))) if v.size else float("nan")


def build_template(
    structures: list[PageStructure], width: int, height: int
) -> DocumentTemplate:
    """Summarize page structures into one editable template."""
    band_counts = [len(s.bands) for s in structures]
    modal_bands = int(np.bincount(band_counts).argmax()) if band_counts else 0
    conforming = [s for s in structures if len(s.bands) == modal_bands]

    edge_matrix = (
        np.array([[b.top for b in s.bands] + [s.bands[-1].bottom] for s in conforming])
        if conforming
        else np.zeros((0, 0))
    )
    band_edges = np.median(edge_matrix, axis=0) if edge_matrix.size else np.array([])
    band_mad = (
        np.array([_mad(edge_matrix[:, i]) for i in range(edge_matrix.shape[1])])
        if edge_matrix.size
        else np.array([])
    )

    pitches = np.array(
        [s.column_pitch for s in conforming if s.pitch_confidence > 0.15]
    )
    lefts = np.array([s.text_left for s in conforming])
    rights = np.array([s.text_right for s in conforming])

    return DocumentTemplate(
        canonical_width=width,
        canonical_height=height,
        band_count=modal_bands,
        band_edges=[float(v) for v in band_edges],
        band_edge_mad=[float(v) for v in band_mad],
        column_pitch=float(np.median(pitches)) if pitches.size else 0.0,
        column_pitch_mad=_mad(pitches),
        text_left=float(np.median(lefts)) if lefts.size else 0.0,
        text_right=float(np.median(rights)) if rights.size else 0.0,
        pages_used=len(conforming),
    )


def repair_bands_from_template(
    structure: PageStructure,
    template: DocumentTemplate,
    *,
    tolerance_frac: float = 0.05,
) -> bool:
    """Align a page's horizontal rules with the learned document structure.

    Rule detection is deliberately local, so a faint separator can disappear on
    one scan and a crease can look like an extra separator on another.  The
    document template is the independent corpus vote that resolves those cases:

    * insert one missing separator when every detected separator matches a
      distinct template edge;
    * drop extra separators when every template edge has a nearby detected rule;
    * leave covers, endpapers, and more severe disagreements untouched.

    Matched rules keep their measured page-local coordinate.  Only an actually
    missing rule uses the corpus median, so normal scan-to-scan movement is not
    flattened away.
    """
    target_count = int(template.band_count)
    target_edges = [float(v) for v in template.band_edges[1:-1]]
    # ``find_bands`` drops narrow slivers caused by doubled rules. That can
    # leave a gap between retained bands, so a real separator may be the top of
    # one band rather than the bottom of the preceding retained band. Treat
    # both sides as rule candidates and let the corpus template select them.
    detected_edges = sorted(
        {
            float(edge)
            for band in structure.bands
            for edge in (band.top, band.bottom)
            if 0.0 < float(edge) < float(template.canonical_height)
        }
    )
    detected_count = len(structure.bands)
    if target_count <= 1 or len(template.band_edges) != target_count + 1:
        return False

    bands_are_contiguous = (
        detected_count == target_count
        and bool(structure.bands)
        and abs(structure.bands[0].top) < 1e-6
        and abs(structure.bands[-1].bottom - template.canonical_height) < 1e-6
        and all(
            abs(left.bottom - right.top) < 1e-6
            for left, right in zip(structure.bands, structure.bands[1:], strict=False)
        )
    )
    if bands_are_contiguous:
        return False

    tolerance = tolerance_frac * float(template.canonical_height)
    candidate_edges = detected_edges
    used_low_coverage = False
    if len(candidate_edges) < len(target_edges) - 1 and len(
        structure.low_coverage_band_edges
    ) >= len(target_edges):
        candidate_edges = structure.low_coverage_band_edges
        # Low-coverage rules are less precisely localized, but still require a
        # close corpus match for every expected separator.
        tolerance = max(tolerance, 0.06 * float(template.canonical_height))
        used_low_coverage = True
    final_interior: list[float] | None = None

    if len(candidate_edges) == len(target_edges) - 1:
        # One rule is missing. Match the remaining measured rules to an ordered
        # subset of the template, then fill the single unmatched template edge.
        candidates = []
        for chosen in combinations(range(len(target_edges)), len(candidate_edges)):
            errors = [
                abs(candidate_edges[i] - target_edges[j]) for i, j in enumerate(chosen)
            ]
            candidates.append((max(errors, default=0.0), sum(errors), chosen))
        max_error, _total_error, chosen = min(candidates)
        if max_error <= tolerance:
            measured_by_target = {
                target_index: candidate_edges[detected_index]
                for detected_index, target_index in enumerate(chosen)
            }
            final_interior = [
                measured_by_target.get(i, edge) for i, edge in enumerate(target_edges)
            ]
    elif len(candidate_edges) >= len(target_edges):
        # One or more extra rules were found. Keep the ordered subset that best
        # matches every expected template edge.
        candidates = []
        for chosen in combinations(range(len(candidate_edges)), len(target_edges)):
            errors = [
                abs(candidate_edges[j] - target_edges[i]) for i, j in enumerate(chosen)
            ]
            candidates.append((max(errors, default=0.0), sum(errors), chosen))
        max_error, _total_error, chosen = min(candidates)
        if max_error <= tolerance:
            final_interior = [candidate_edges[j] for j in chosen]

    if final_interior is None:
        return False

    original_edges = [0.0, *detected_edges, float(template.canonical_height)]
    final_edges = [0.0, *final_interior, float(template.canonical_height)]
    structure.bands = [
        BandGeometry(i, final_edges[i], final_edges[i + 1]) for i in range(target_count)
    ]
    structure.band_repair = {
        "detected_count": detected_count,
        "final_count": target_count,
        "detected_edges": original_edges,
        "candidate_edges": candidate_edges,
        "used_low_coverage": used_low_coverage,
        "final_edges": final_edges,
    }
    return True


def assign_layout_families(
    structures: list[PageStructure], template: DocumentTemplate, bins: int = 64
) -> dict[str, list[int]]:
    """Group pages by layout, without assuming the book has only one.

    Pages are described by their band count plus a coarse ink profile, then
    clustered. A page whose profile does not resemble any group — the cover, a
    torn page — lands in `outlier` rather than being forced into a family.
    """
    from sklearn.cluster import DBSCAN

    if not structures:
        return {}

    feats = []
    for s in structures:
        p = np.array(s.profile, dtype=np.float32)
        if p.size == 0:
            feats.append(np.zeros(bins + 1, dtype=np.float32))
            continue
        binned = np.array(
            [
                p[int(i * p.size / bins) : int((i + 1) * p.size / bins)].mean()
                for i in range(bins)
            ],
            dtype=np.float32,
        )
        feats.append(np.concatenate([[len(s.bands) * 1.0], binned]))
    X = np.vstack(feats)
    # Band count dominates the distance on purpose: a page with a different
    # number of generation bands is a different layout no matter how its ink
    # happens to be distributed.
    X[:, 0] *= 4.0

    labels = DBSCAN(eps=1.6, min_samples=5).fit_predict(X)
    families: dict[str, list[int]] = {}
    for s, lab in zip(structures, labels, strict=True):
        key = "outlier" if lab < 0 else f"layout_{chr(ord('A') + int(lab))}"
        families.setdefault(key, []).append(s.page_index)
    return families
