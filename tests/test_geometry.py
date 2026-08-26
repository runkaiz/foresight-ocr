"""Structural tests from the spec's test list.

These use synthetic pages so the suite stays fast; the real corpus is exercised
by the pipeline stages and their reports.
"""

import cv2
import numpy as np
import pytest

from foresight_ocr.imaging.variants import build_variant, chroma_mask, ink_channel
from foresight_ocr.layout.frame import detect_frame
from foresight_ocr.layout.lines import detect_rules
from foresight_ocr.layout.normalize import (
    FramePass,
    build_canonical_space,
    normalize_page,
    roundtrip_error,
)
from foresight_ocr.layout.template import (
    BandGeometry,
    DocumentTemplate,
    PageStructure,
    build_template,
    repair_bands_from_template,
)
from foresight_ocr.segmentation.entries import entry_boundaries, segment_page


def synthetic_page(width=2424, height=3744, skew=0.0, margin=100):
    """A ruled page: outer box plus two interior band separators."""
    img = np.full((height, width, 3), 235, dtype=np.uint8)
    x0, x1 = margin, width - margin
    y0, y1 = margin * 4, height - margin * 2
    ys = [y0, y0 + (y1 - y0) // 3, y0 + 2 * (y1 - y0) // 3, y1]
    for y in ys:
        for x in range(x0, x1):
            yy = int(y + (x - x0) * np.tan(np.radians(skew)))
            cv2.line(img, (x, yy), (x, yy), (20, 20, 20), 3)
    cv2.line(img, (x0, y0), (x0, y1), (20, 20, 20), 3)
    cv2.line(img, (x1, y0), (x1, y1), (20, 20, 20), 3)
    return img, (x0, y0, x1, y1)


def test_detect_rules_finds_the_four_horizontals():
    img, _ = synthetic_page()
    small = cv2.resize(img, None, fx=0.25, fy=0.25, interpolation=cv2.INTER_AREA)
    lines = detect_rules(small, "h", min_coverage=0.45)
    assert len(lines) == 4


def _page_structure(page: int, edges: list[float]) -> PageStructure:
    return PageStructure(
        page_index=page,
        bands=[BandGeometry(i, edges[i], edges[i + 1]) for i in range(len(edges) - 1)],
        column_edges=[],
        column_pitch=378.0,
        pitch_confidence=0.6,
        text_left=0.0,
        text_right=2300.0,
    )


def _three_band_template() -> DocumentTemplate:
    return DocumentTemplate(
        canonical_width=2300,
        canonical_height=3025,
        band_count=3,
        band_edges=[0.0, 1012.0, 1999.0, 3025.0],
        band_edge_mad=[0.0, 5.0, 5.0, 0.0],
        column_pitch=378.0,
        column_pitch_mad=2.0,
        text_left=0.0,
        text_right=2296.0,
        pages_used=198,
    )


def test_corpus_template_inserts_one_missing_band_rule():
    page = _page_structure(10, [0.0, 1994.0, 3025.0])

    assert repair_bands_from_template(page, _three_band_template())
    assert [(b.top, b.bottom) for b in page.bands] == [
        (0.0, 1012.0),
        (1012.0, 1994.0),
        (1994.0, 3025.0),
    ]
    assert page.band_repair["detected_count"] == 2
    assert page.band_repair["final_count"] == 3


def test_document_template_learns_the_modal_band_count():
    structures = [
        _page_structure(i, [0.0, 1010.0 + i, 2000.0 + i, 3025.0]) for i in range(1, 5)
    ]
    structures.append(_page_structure(5, [0.0, 2000.0, 3025.0]))

    template = build_template(structures, 2300, 3025)

    assert template.band_count == 3
    assert template.pages_used == 4
    assert template.band_edges == pytest.approx([0.0, 1012.5, 2002.5, 3025.0])


def test_corpus_template_drops_a_confidently_spurious_band_rule():
    page = _page_structure(4, [0.0, 1008.0, 2004.0, 2320.0, 3025.0])

    assert repair_bands_from_template(page, _three_band_template())
    assert [(b.top, b.bottom) for b in page.bands] == [
        (0.0, 1008.0),
        (1008.0, 2004.0),
        (2004.0, 3025.0),
    ]


def test_corpus_template_uses_both_sides_of_removed_slivers():
    page = _page_structure(23, [0.0, 335.0, 712.0, 987.0, 1302.0, 1994.0, 3005.0])
    # Mirror find_bands after narrow bands were dropped: the retained bands no
    # longer tile the page, but their tops still preserve the true rules.
    page.bands = [
        BandGeometry(0, 0.0, 335.0),
        BandGeometry(1, 335.0, 712.0),
        BandGeometry(3, 987.0, 1302.0),
        BandGeometry(4, 1302.0, 1994.0),
        BandGeometry(5, 1994.0, 3005.0),
    ]
    template = _three_band_template()
    template.canonical_height = 3005
    template.band_edges = [0.0, 973.0, 1996.0, 3005.0]

    assert repair_bands_from_template(page, template)
    assert [(b.top, b.bottom) for b in page.bands] == [
        (0.0, 987.0),
        (987.0, 1994.0),
        (1994.0, 3005.0),
    ]
    assert page.band_repair["detected_count"] == 5


def test_corpus_template_leaves_a_cover_as_an_outlier():
    page = _page_structure(1, [0.0, 3025.0])

    assert not repair_bands_from_template(page, _three_band_template())
    assert len(page.bands) == 1
    assert page.band_repair is None


def test_corpus_template_recovers_two_weak_rules_from_low_coverage_probe():
    page = _page_structure(233, [0.0, 3005.0])
    page.low_coverage_band_edges = [68.0, 897.0, 2174.0, 2481.0]
    template = _three_band_template()
    template.canonical_height = 3005
    template.band_edges = [0.0, 971.0, 1996.0, 3005.0]

    assert repair_bands_from_template(page, template)
    assert [(b.top, b.bottom) for b in page.bands] == [
        (0.0, 897.0),
        (897.0, 2174.0),
        (2174.0, 3005.0),
    ]
    assert page.band_repair["used_low_coverage"] is True


def test_corpus_template_refuses_an_unrelated_two_band_layout():
    page = _page_structure(1, [0.0, 1500.0, 3025.0])

    assert not repair_bands_from_template(page, _three_band_template())
    assert len(page.bands) == 2


def test_frame_detection_recovers_the_printed_box():
    img, (x0, y0, x1, y1) = synthetic_page()
    fit = detect_frame(img)
    assert fit.ok
    assert fit.width == pytest.approx(x1 - x0, abs=12)
    assert fit.height == pytest.approx(y1 - y0, abs=12)
    assert len(fit.interior_h) == 2


def test_skew_is_measured():
    img, _ = synthetic_page(skew=1.0)
    fit = detect_frame(img)
    assert fit.ok
    assert fit.skew_deg == pytest.approx(1.0, abs=0.15)


def test_coordinate_transform_round_trips():
    img, _ = synthetic_page()
    fit = detect_frame(img)
    space = build_canonical_space([FramePass(1, fit, None)])
    norm = normalize_page(fit, space, 1)
    probe = np.array(
        [[0, 0], [2423, 0], [2423, 3743], [0, 3743], [1212, 1872], [37, 991]],
        dtype=np.float64,
    )
    assert roundtrip_error(norm, probe) < 1.0


def test_canonical_space_ignores_inferred_frames_when_measuring():
    img, _ = synthetic_page()
    fit = detect_frame(img)
    wide = detect_frame(img)
    wide.inferred_edges = ["left"]
    wide.width = fit.width + 500
    passes = [FramePass(i, fit, None) for i in range(30)]
    passes.append(FramePass(99, wide, None))
    space = build_canonical_space(passes)
    assert space.median_width == pytest.approx(fit.width, abs=1)


def test_ink_channel_suppresses_cyan_but_keeps_neutral_ink():
    page = np.full((60, 60, 3), 240, dtype=np.uint8)
    page[10:20, :] = (30, 30, 30)  # neutral ink
    page[30:40, :] = (220, 200, 60)  # cyan-ish stamp in BGR
    ink = ink_channel(page)
    assert ink[15, 30] < 60  # ink stays dark
    assert ink[35, 30] > 200  # stamp goes bright


def test_chroma_mask_selects_only_the_coloured_region():
    page = np.full((60, 60, 3), 240, dtype=np.uint8)
    page[10:20, :] = (30, 30, 30)
    page[30:40, :] = (220, 200, 60)
    mask = chroma_mask(page, dilate=0)
    assert mask[35, 30] > 0
    assert mask[15, 30] == 0


@pytest.mark.parametrize(
    "name",
    [
        "gray",
        "red",
        "maxrgb",
        "watermark",
        "lab_l",
        "neutral",
        "inpaint",
        "contrast",
        "binary",
    ],
)
def test_every_variant_returns_a_single_channel_page(name):
    page = np.full((80, 80, 3), 200, dtype=np.uint8)
    page[20:40, 20:40] = (30, 30, 30)
    out = build_variant(page, name)
    assert out.shape == (80, 80)
    assert out.dtype == np.uint8


def test_watermark_variant_keeps_dark_ink_and_removes_gray_logo():
    page = np.full((60, 60, 3), 240, dtype=np.uint8)
    page[10:20, :] = (30, 30, 30)
    page[30:40, :] = (180, 180, 180)
    out = build_variant(page, "watermark")
    assert out[15, 30] == 0
    assert out[35, 30] == 255


def test_entry_lattice_snaps_to_gutters_and_keeps_pitch():
    gutters = [100.0, 300.0, 500.0, 700.0, 900.0]
    bounds, snapped = entry_boundaries(100.0, 900.0, 200.0, gutters)
    assert bounds == [900.0, 700.0, 500.0, 300.0, 100.0]
    assert all(snapped)


def test_entry_lattice_survives_a_missing_gutter():
    gutters = [100.0, 300.0, 700.0, 900.0]  # the 500 gutter was not detected
    bounds, _ = entry_boundaries(100.0, 900.0, 200.0, gutters)
    assert bounds == pytest.approx([900.0, 700.0, 500.0, 300.0, 100.0])


def test_segment_page_orders_entries_right_to_left_and_stays_in_bounds():
    regions = segment_page(
        page_index=7,
        bands=[(0, 0.0, 1000.0), (1, 1000.0, 2000.0)],
        column_edges=[100.0, 300.0, 500.0, 700.0, 900.0],
        pitch=200.0,
        text_left=100.0,
        text_right=900.0,
        page_width=1000,
    )
    tight = [r for r in regions if r.context == "tight" and r.band_index == 0]
    assert [r.entry_index for r in tight] == [0, 1, 2, 3]
    # Entry 0 is the rightmost column.
    assert tight[0].x1 > tight[-1].x1
    assert all(0 <= r.x0 < r.x1 <= 1000 for r in regions)
    assert all(r.y0 < r.y1 for r in regions)


def test_wide_context_crops_are_clamped_to_the_page():
    regions = segment_page(
        page_index=7,
        bands=[(0, 0.0, 1000.0)],
        column_edges=[100.0, 300.0, 500.0],
        pitch=200.0,
        text_left=100.0,
        text_right=500.0,
        page_width=600,
    )
    assert all(0 <= r.x0 and r.x1 <= 600 for r in regions)


def test_comb_fit_ignores_spurious_gutters():
    # Real entry boundaries every 379 px, plus four gutters from gaps *inside*
    # annotation columns. The comb must be voted for by the real majority.
    real = [70.0, 411.0, 862.0, 1156.0, 1547.0, 1839.0, 2256.0]
    spurious = [204.0, 579.0, 1352.0, 2104.0]
    bounds, _ = entry_boundaries(0.0, 2299.0, 379.0, sorted(real + spurious))
    widths = [bounds[i] - bounds[i + 1] for i in range(len(bounds) - 1)]
    # Every entry should come out close to one pitch, not half or one and a half.
    assert all(0.8 * 379 <= w <= 1.2 * 379 for w in widths), widths
    assert not any(round(b) in (204, 579, 1352, 2104) for b in bounds)


def test_comb_fit_reports_how_many_gutters_it_explains():
    from foresight_ocr.segmentation.entries import fit_comb

    clean = [100.0 + 200.0 * k for k in range(6)]
    _, hits = fit_comb(0.0, 1100.0, 200.0, clean)
    assert hits == len(clean)


def test_one_bad_gutter_does_not_shift_the_rest_of_the_page():
    good = [100.0 + 200.0 * k for k in range(6)]
    bounds_clean, _ = entry_boundaries(100.0, 1100.0, 200.0, good)
    bounds_noisy, _ = entry_boundaries(100.0, 1100.0, 200.0, sorted(good + [640.0]))
    assert [round(b) for b in bounds_clean] == [round(b) for b in bounds_noisy]


def test_comb_covers_the_right_edge_when_the_phase_lands_short():
    # The gutters only reach 1940, so the fitted phase lands there. The comb
    # must still be extended rightwards to cover the requested domain instead of
    # dropping the band's last entry.
    gutters = [68.0, 414.0, 792.0, 1166.0, 1558.0, 1940.0]
    bounds, _ = entry_boundaries(0.0, 2296.0, 379.0, gutters)
    assert max(bounds) > 2296.0 - 379.0
    assert len(bounds) - 1 == 6


def test_entry_count_survives_a_short_measured_ink_edge():
    # A faint rightmost column pulls the measured text edge inward by less than
    # one pitch. The band must still yield the same number of entries — an entry
    # lost this way leaves nothing in the geometry to signal the loss.
    gutters = [68.0, 414.0, 792.0, 1166.0, 1558.0, 1940.0]
    counts = {
        len(entry_boundaries(0.0, right, 379.0, gutters)[0]) - 1
        for right in (2230.0, 2260.0, 2296.0, 2299.0)
    }
    assert counts == {6}, counts


def test_lattice_entry_count_is_stable_across_pages_with_different_ink_extents():
    # Same frame, same pitch; only the measured ink edge differs. Entry count
    # must not depend on how far the ink happens to reach.
    counts = set()
    for right in (2299.0, 2260.0, 2230.0, 2221.0):
        bounds, _ = entry_boundaries(0.0, max(right, 2296.0), 378.0, [])
        counts.add(len(bounds) - 1)
    assert len(counts) == 1, counts


def test_corpus_anchor_selects_the_other_half_phase_on_a_damaged_page():
    from foresight_ocr.segmentation.entries import anchored_phase_offset

    # Page 199 exposes both alternating gutter families.  Damage made the
    # 1912 px family win locally, but the volume's first interior boundary is
    # around x=2096, so the 2101 px family is the structurally valid one.
    gutters = [
        53.0,
        186.0,
        385.5,
        555.5,
        760.5,
        936.0,
        1143.5,
        1321.0,
        1528.5,
        1708.0,
        1912.5,
        2101.5,
    ]
    pitch = 382.0
    selected = anchored_phase_offset(0.0, 2298.0, pitch, gutters, -pitch / 2, 2096.5)
    bounds, _ = entry_boundaries(0.0, 2298.0, pitch, gutters, phase_offset=selected)
    assert selected == pytest.approx(0.0)
    assert bounds[1] == pytest.approx(2101.5)


def test_corpus_anchor_keeps_a_legitimate_page_offset():
    from foresight_ocr.segmentation.entries import anchored_phase_offset

    # Page 100 is shifted farther right than the median, but its opposite phase
    # is farther away still.  A corpus prior must disambiguate, not flatten all
    # local geometry onto one hard-coded coordinate.
    gutters = [
        90.5,
        221.0,
        434.5,
        606.0,
        816.5,
        980.0,
        1192.0,
        1366.5,
        1646.5,
        1952.5,
        2132.5,
        2275.5,
    ]
    pitch = 378.0
    selected = anchored_phase_offset(0.0, 2295.5, pitch, gutters, -pitch / 2, 2096.5)
    bounds, _ = entry_boundaries(0.0, 2295.5, pitch, gutters, phase_offset=selected)
    assert selected == pytest.approx(-pitch / 2)
    assert bounds[1] == pytest.approx(2180.5)


def test_phase_anchor_is_the_corpus_median_not_an_outlier_vote():
    from foresight_ocr.segmentation.entries import infer_entry_phase_anchor

    def geometry(edges):
        return {
            "column_edges": edges,
            "column_pitch": 200.0,
            "pitch_confidence": 1.0,
            "text_left": 0.0,
            "text_right": 1000.0,
        }

    regular = [100.0, 300.0, 500.0, 700.0, 900.0]
    flipped = [0.0, 200.0, 400.0, 600.0, 800.0, 1000.0]
    anchor = infer_entry_phase_anchor(
        [geometry(regular), geometry(regular), geometry(regular), geometry(flipped)],
        200.0,
        0.0,
        1000.0,
        0.0,
    )
    assert anchor == pytest.approx(900.0)
