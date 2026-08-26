"""Repairing a page whose lattice landed on the wrong phase.

The failure this exists for is not a misreading. On 丙辰庶富教1 page 3 the comb
was fitted by a vote among ten detected gutters where a clean page detects
thirteen, and the majority chose a phase half a column off: every crop held the
left half of one entry and the right half of the next, and all 21 entries came
back wrong. One number was wrong, once, for the whole page.

So what is tested here is that one number repairs it, and that repairing it does
not cost the page its history — a shifted column has to still be the same
column, with the readings and the correction that were made against it.
"""

import json
import sqlite3
from pathlib import Path

import cv2
import numpy as np
import pytest

from foresight_ocr.persistence.db import init_schema
from foresight_ocr.project import Project
from foresight_ocr.regions import store
from foresight_ocr.regions.crops import ensure_crop
from foresight_ocr.regions.recut import (
    Band,
    CombInputs,
    PageNotSegmentable,
    apply_comb,
    plan_comb,
)
from foresight_ocr.review.data import page_entries
from foresight_ocr.segmentation.entries import entry_boundaries

# --------------------------------------------------------------------------
# the lattice itself


PITCH = 377.0
SMALL_ADJUSTMENT = -45.0
# Page 3's own gutters. Ten of them, where a clean page of this book detects
# thirteen; three of the ten belong to the sub-columns inside an entry.
P3_GUTTERS = [46.0, 181.0, 364.0, 668.0, 1052.0, 1429.0, 1806.0, 2060.0, 2178.0, 2272.0]


def _spacing(bounds):
    edges = sorted(bounds)
    return [round(b - a) for a, b in zip(edges, edges[1:], strict=False)]


def test_the_fitted_phase_is_the_one_that_was_wrong():
    """The unadjusted lattice is reproduced, so the repair has something to fix."""
    bounds, _ = entry_boundaries(0.0, 2295.5, PITCH, P3_GUTTERS)
    assert _spacing(bounds) == [405, 304, 384, 377, 377, 372, 420]


def test_shifting_the_phase_makes_the_columns_even():
    bounds, _ = entry_boundaries(
        0.0, 2295.5, PITCH, P3_GUTTERS, phase_offset=-PITCH / 2
    )
    # The corrected gutters put the right-edge own-id fragment at x=2060 and
    # the preceding page's continuation at the left edge below x=181.
    assert bounds == pytest.approx(
        [2409.5, 2060.0, 1655.5, 1278.5, 901.5, 524.5, 181.0]
    )


def test_verified_base_phase_is_applied_before_reviewer_adjustment():
    inputs = _inputs()
    inputs = CombInputs(**{**inputs.__dict__, "base_phase_fraction": -0.5})
    plan = plan_comb(inputs)
    assert plan.phase_adjustment == 0.0
    assert plan.base_phase_offset == pytest.approx(-PITCH / 2)
    assert plan.boundaries == pytest.approx(
        [2409.5, 2060.0, 1655.5, 1278.5, 901.5, 524.5, 181.0]
    )


def test_corpus_anchor_is_part_of_the_default_recut_plan():
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
    inputs = _inputs(pitch=382.0, gutters=gutters)
    inputs = CombInputs(
        **{
            **inputs.__dict__,
            "text_right": 2298.0,
            "base_phase_fraction": -0.5,
            "phase_anchor_x": 2096.5,
        }
    )
    plan = plan_comb(inputs)
    assert plan.phase_adjustment == 0.0
    assert plan.base_phase_offset == pytest.approx(0.0)
    assert plan.boundaries[1] == pytest.approx(2101.5)


def test_the_extent_decides_how_many_columns_there_are():
    """A page whose text stops short of the corpus gains a column of margin.

    The lattice domain comes from the template, so on a page whose ink ends
    earlier the comb steps once more and produces a sliver entry at the page
    edge. On page 3 that sliver read `一` and `民國內辰重修` — the printer's
    imprint, cut and filed as two people.
    """
    wide, _ = entry_boundaries(0.0, 2295.5, PITCH, P3_GUTTERS, phase_offset=-PITCH / 2)
    trimmed, _ = entry_boundaries(
        -100.0, 2200.0, PITCH, P3_GUTTERS, phase_offset=-PITCH / 2
    )
    assert max(wide) > 2300 and len(wide) - 1 == 6
    assert wide == trimmed
    # Both edge intervals are deliberately partial: the right one belongs to
    # this page, while the left one is carried into the following scan.
    assert min(wide) == 181.0


def test_snapping_can_be_switched_off():
    """When the gutters are the reason the page is wrong, they can be ignored."""
    snapped, flags = entry_boundaries(0.0, 2295.5, PITCH, P3_GUTTERS)
    plain, none = entry_boundaries(0.0, 2295.5, PITCH, P3_GUTTERS, snap=False)
    assert any(flags) and not any(none)
    assert len(set(_spacing(plain))) == 1  # a perfectly regular comb


def test_a_phase_of_zero_changes_nothing():
    a, _ = entry_boundaries(0.0, 2295.5, PITCH, P3_GUTTERS)
    b, _ = entry_boundaries(0.0, 2295.5, PITCH, P3_GUTTERS, phase_offset=0.0)
    assert a == b


def test_one_boundary_can_be_overridden_without_moving_the_rest():
    inputs = _inputs()
    base = plan_comb(inputs, snap=False)
    index = 2
    moved_x = base.boundaries[index] - 21.0

    adjusted = plan_comb(inputs, snap=False, boundary_overrides={index: moved_x})

    assert adjusted.boundaries[index] == moved_x
    assert adjusted.manual[index] is True
    assert adjusted.snapped[index] is False
    assert adjusted.boundaries[:index] == base.boundaries[:index]
    assert adjusted.boundaries[index + 1 :] == base.boundaries[index + 1 :]
    assert adjusted.entries_per_band == base.entries_per_band
    # One division is shared by the two columns on either side of it.
    assert adjusted.proposals[index - 1].bbox[0] == moved_x
    assert adjusted.proposals[index].bbox[2] == moved_x


def test_manual_boundaries_cannot_cross_each_other():
    inputs = _inputs()
    base = plan_comb(inputs, snap=False)

    with pytest.raises(PageNotSegmentable, match="right-to-left order"):
        plan_comb(
            inputs,
            snap=False,
            boundary_overrides={1: base.boundaries[2] - 1.0},
        )


# --------------------------------------------------------------------------
# applying it


def _inputs(pitch=PITCH, gutters=None):
    return CombInputs(
        document_id="doc",
        page_index=3,
        pitch=pitch,
        text_left=0.0,
        text_right=2295.5,
        gutters=list(P3_GUTTERS if gutters is None else gutters),
        page_width=2300,
        page_height=3025,
        bands=[Band(0, "庶", 0.0, 1000.0)],
        pitch_confidence=0.47,
        used_corpus_pitch=False,
        corpus_pitch=378.0,
    )


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    conn.execute("INSERT INTO documents VALUES ('doc','t','p','c',1,'now')")
    return conn


def _seed(conn, plan):
    """Put the page's regions where the given plan says, as segment would."""
    out = []
    for i, proposal in enumerate(plan.proposals):
        out.append(
            store.create_region(
                conn,
                "doc",
                3,
                proposal.bbox,
                band_label=proposal.band_label,
                band_ordinal=proposal.band_ordinal,
                reading_order=i,
                entry_index=i,
            )
        )
    conn.commit()
    return out


def test_a_shift_moves_the_columns_and_keeps_their_identity():
    inputs = _inputs()
    conn = _db()
    before = {r.region_uid: list(r.bbox) for r in _seed(conn, plan_comb(inputs))}

    # A smaller reviewer adjustment exercises identity preservation; the
    # document's verified half-pitch base is tested separately above.
    report = apply_comb(
        conn,
        None,
        plan_comb(inputs, phase_offset=SMALL_ADJUSTMENT),
        inputs,
        reocr=False,
    )
    after = {r.region_uid: r for r in store.for_page(conn, "doc", 3)}

    # The shift is smaller than half a column, so every column that survives is
    # still itself. The lattice loses one at the left edge, and losing it is the
    # point — it was the page margin cut as an entry.
    assert set(after) < set(before)
    assert (report.created, report.retired) == (0, 1)
    assert report.moved
    for uid, region in after.items():
        if region.bbox != before[uid]:
            # A person said where this column is; a later automatic pass has to
            # ask before moving it back.
            assert region.state == "adjusted"
    assert [r.reading_order for r in store.for_page(conn, "doc", 3)] == list(
        range(len(after))
    )


def test_shift_without_reocr_materializes_crops_for_the_applied_geometry(tmp_path):
    inputs = CombInputs(
        document_id="doc",
        page_index=3,
        pitch=40.0,
        text_left=0.0,
        text_right=220.0,
        gutters=[],
        page_width=240,
        page_height=120,
        bands=[Band(0, "庶", 0.0, 100.0)],
        pitch_confidence=1.0,
        used_corpus_pitch=False,
        corpus_pitch=40.0,
    )
    project = Project(tmp_path)
    normalized = project.pages_dir("doc", "normalized")
    normalized.mkdir(parents=True)
    image = np.full((120, 240, 3), 240, dtype=np.uint8)
    image[:, 20:225] = 30
    assert cv2.imwrite(str(normalized / "p0003.png"), image)

    conn = _db()
    conn.execute(
        "INSERT INTO pages (document_id, page_index, width, height) "
        "VALUES ('doc',3,240,120)"
    )
    conn.commit()
    seeded = _seed(conn, plan_comb(inputs, snap=False))
    for region in seeded:
        ensure_crop(conn, project, region, variant="original")
    conn.commit()
    stale_count = conn.execute("SELECT COUNT(*) n FROM region_crops").fetchone()["n"]
    assert stale_count == len(seeded)

    report = apply_comb(
        conn,
        project,
        plan_comb(inputs, phase_offset=5.0, snap=False),
        inputs,
        reocr=False,
        variant="original",
    )

    live = store.for_page(conn, "doc", 3)
    assert report.errors == []
    assert report.moved == len(live) > 0
    assert report.crops_cut == len(live)
    assert conn.execute("SELECT COUNT(*) n FROM ocr_candidates").fetchone()["n"] == 0

    # Old crop evidence remains, but every moved region now has a crop whose
    # geometry hash and displayed pixel box match the accepted lattice.
    assert conn.execute("SELECT COUNT(*) n FROM region_crops").fetchone()[
        "n"
    ] == stale_count + len(live)
    displayed = {
        entry.entry_index: entry.crop_bbox for entry in page_entries(conn, "doc", 3)
    }
    for region in live:
        row = conn.execute(
            "SELECT path, pixel_bbox_json FROM region_crops "
            "WHERE region_id = ? AND geometry_hash = ? AND context = 'tight'",
            (region.id, region.geometry_hash),
        ).fetchone()
        assert row is not None
        assert displayed[region.reading_order] == [int(v) for v in region.bbox]
        assert json.loads(row["pixel_bbox_json"]) == displayed[region.reading_order]
        assert Path(row["path"]).exists()


def test_re_cutting_at_the_same_phase_is_a_no_op():
    """A person who opens the control and changes nothing costs nothing.

    Not a nicety: the whole cache — crops and readings alike — is addressed by
    geometry, so a lattice that produces the same boxes must produce the same
    rows, or every visit to the control would re-read the page.
    """
    inputs = _inputs()
    conn = _db()
    _seed(conn, plan_comb(inputs))
    conn.execute(
        "INSERT INTO validation_findings (document_id, band_label, kind, "
        "page_index, entry_index) VALUES ('doc','庶','gap',3,4)"
    )
    conn.commit()

    report = apply_comb(conn, None, plan_comb(inputs), inputs, reocr=False)
    assert (report.moved, report.created, report.retired) == (0, 0, 0)
    assert report.unchanged == len(plan_comb(inputs).proposals)
    assert report.crops_cut == 0
    # Looking is not editing: a reviewer who opens the control and accepts what
    # is already there keeps the page's findings.
    assert report.findings_cleared == 0
    assert (
        conn.execute("SELECT COUNT(*) n FROM validation_findings").fetchone()["n"] == 1
    )


def test_the_whole_page_takes_the_lattice_the_person_chose():
    """Including the columns that happened not to move.

    A shift leaves some boxes exactly where they were. If those stayed
    `proposed`, the next automatic pass would put two thirds of the page back on
    the comb the reviewer rejected and leave the other third — a page cut two
    ways at once, which is worse than either.
    """
    inputs = _inputs()
    conn = _db()
    _seed(conn, plan_comb(inputs))
    report = apply_comb(
        conn,
        None,
        plan_comb(inputs, phase_offset=SMALL_ADJUSTMENT),
        inputs,
        reocr=False,
    )
    assert report.unchanged and report.moved  # both kinds are present
    assert {r.state for r in store.for_page(conn, "doc", 3)} == {"adjusted"}


def test_a_correction_follows_its_column_to_the_new_position():
    """The reviewer's answer is about a column, not about a slot on the page.

    Corrections are keyed positionally, and a re-cut renumbers the band. Left
    alone, the answer for what was entry 3 would describe whatever entry 3
    becomes — a wrong reading stated with confidence, which is worse than none.
    """
    inputs = _inputs()
    conn = _db()
    seeded = _seed(conn, plan_comb(inputs))
    marked = seeded[2]
    conn.execute(
        "INSERT INTO human_corrections (document_id, page_index, band_label, "
        "entry_index, role, transcription, unreadable, corrected_by, corrected_at) "
        "VALUES ('doc',3,'庶',?, 'entry','庶七允五長子',0,'me','now')",
        (marked.reading_order,),
    )
    conn.commit()

    apply_comb(
        conn,
        None,
        plan_comb(
            inputs, phase_offset=SMALL_ADJUSTMENT, text_left=-100.0, text_right=2200.0
        ),
        inputs,
        reocr=False,
    )

    moved = store.get(conn, marked.region_uid)
    row = conn.execute(
        "SELECT band_label, entry_index, transcription FROM human_corrections"
    ).fetchone()
    assert row["transcription"] == "庶七允五長子"
    assert (row["band_label"], row["entry_index"]) == (
        moved.band_label,
        moved.reading_order,
    )


def test_the_pages_findings_go_with_the_positions_they_described():
    inputs = _inputs()
    conn = _db()
    _seed(conn, plan_comb(inputs))
    conn.execute(
        "INSERT INTO validation_findings (document_id, band_label, kind, "
        "page_index, entry_index) VALUES ('doc','庶','gap',3,4)"
    )
    conn.execute(
        "INSERT INTO validation_findings (document_id, band_label, kind, "
        "page_index, entry_index) VALUES ('doc','庶','gap',4,1)"
    )
    conn.commit()
    report = apply_comb(
        conn,
        None,
        plan_comb(inputs, phase_offset=SMALL_ADJUSTMENT),
        inputs,
        reocr=False,
    )
    assert report.findings_cleared == 1
    left = conn.execute("SELECT page_index FROM validation_findings").fetchall()
    assert [r["page_index"] for r in left] == [4]  # the next page is untouched


def test_a_withdrawn_column_keeps_what_was_read_from_it():
    """Retiring is a soft delete, so the evidence survives the repair.

    The sliver at the page edge really was cut and really was read; that reading
    describes pixels that existed. If the same box is ever proposed again the row
    comes back with it still attached.
    """
    inputs = _inputs()
    conn = _db()
    seeded = _seed(conn, plan_comb(inputs))
    conn.execute("INSERT INTO models VALUES ('m','b','1','b')")
    conn.execute(
        "INSERT INTO ocr_runs (id, run_id, model_id, input_variant, tag) "
        "VALUES (1,NULL,'m','maxrgb','t')"
    )
    conn.execute(
        "INSERT INTO ocr_candidates (region_id, ocr_run_id, transcription) "
        "VALUES (?,1,'一')",
        (seeded[0].id,),
    )
    conn.commit()

    apply_comb(
        conn,
        None,
        plan_comb(
            inputs, phase_offset=SMALL_ADJUSTMENT, text_left=-100.0, text_right=2200.0
        ),
        inputs,
        reocr=False,
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) n FROM ocr_candidates WHERE region_id = ?", (seeded[0].id,)
        ).fetchone()["n"]
        == 1
    )


def test_the_detector_does_not_re_insert_a_column_the_reviewer_removed():
    """The sliver comes back on every run unless the removal is recorded as one.

    Withdrawing a proposal and rejecting a column look the same on the page and
    are opposites in the record. The detector will keep proposing the sliver at
    the page edge — nothing about the page has changed — so a mere withdrawal is
    undone by the next `segment`, and the reviewer deletes the same column once
    per run for the rest of the volume.
    """
    from foresight_ocr.regions.reconcile import reconcile_page

    inputs = _inputs()
    conn = _db()
    _seed(conn, plan_comb(inputs))
    apply_comb(
        conn,
        None,
        plan_comb(
            inputs, phase_offset=SMALL_ADJUSTMENT, text_left=-100.0, text_right=2200.0
        ),
        inputs,
        reocr=False,
    )
    live = len(store.for_page(conn, "doc", 3))

    # The pipeline runs again and proposes exactly what it proposed before.
    report = reconcile_page(conn, "doc", 3, plan_comb(inputs).proposals)

    assert report.refused == 1 and report.created == 0 and report.revived == 0
    assert len(store.for_page(conn, "doc", 3)) == live
    # And the reviewer's boxes are kept, with the disagreement recorded.
    assert len(report.divergent) == live
