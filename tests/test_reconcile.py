"""Matching a fresh segmentation against the regions already on a page.

The behaviour being pinned down is what a rerun is allowed to do to work
somebody has done. Machine geometry may be replaced freely; edited geometry may
not be touched at all, and the disagreement has to be recorded rather than
resolved, because a detector that has genuinely improved and a detector that is
wrong look identical from inside the code.

The other half is that a rerun which changes nothing has to be recognisable as
such. Before this, segmenting a page twice produced two sets of rows and no way
to tell them apart, so every rerun cost a full re-read of the book.
"""

import json
import sqlite3

import pytest

from foresight_ocr.persistence.db import init_schema
from foresight_ocr.regions import store
from foresight_ocr.regions.model import RegionState
from foresight_ocr.regions.reconcile import (
    MATCH_IOU,
    Proposal,
    interval_iou,
    reconcile_page,
)

DOC = "doc"
PAGE = 58
BAND_TOP, BAND_BOTTOM = 0.0, 1012.0

#: Three columns on a ~380 px pitch, right to left, as the lattice cuts them.
COLUMNS = [
    (0, [1920.0, BAND_TOP, 2300.0, BAND_BOTTOM]),
    (1, [1540.0, BAND_TOP, 1920.0, BAND_BOTTOM]),
    (2, [1160.0, BAND_TOP, 1540.0, BAND_BOTTOM]),
]


@pytest.fixture
def conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    conn.execute(
        "INSERT INTO documents VALUES (?,?,?,?,?,?)", (DOC, DOC, "p", "c", 1, "now")
    )
    return conn


def _proposals(columns=COLUMNS, band=0, label="庶"):
    return [
        Proposal(band_ordinal=band, band_label=label, entry_index=i, bbox=list(box))
        for i, box in columns
    ]


def _seed(conn, columns=COLUMNS):
    """A first segmentation pass, as the machine would leave it."""
    return reconcile_page(conn, DOC, PAGE, _proposals(columns))


def _live(conn):
    return store.for_page(conn, DOC, PAGE)


def _uids(conn):
    return [r.region_uid for r in _live(conn)]


# --------------------------------------------------------------------------
# the overlap measure


def test_overlap_is_measured_across_the_column_not_down_it():
    a = [1540.0, 0.0, 1920.0, 1012.0]
    assert interval_iou(a, a) == 1.0
    # A band is a full-width strip, so two columns never overlap vertically and
    # a shorter box is the same column, not a different one.
    assert interval_iou(a, [1540.0, 0.0, 1920.0, 400.0]) == 1.0


def test_neighbouring_columns_do_not_look_like_each_other():
    assert interval_iou(COLUMNS[0][1], COLUMNS[1][1]) < MATCH_IOU
    assert interval_iou(COLUMNS[1][1], COLUMNS[2][1]) < MATCH_IOU


def test_a_boundary_that_moved_a_few_pixels_is_still_the_same_column():
    moved = [1548.0, 0.0, 1925.0, 1012.0]
    assert interval_iou(COLUMNS[1][1], moved) > MATCH_IOU


# --------------------------------------------------------------------------
# a rerun that changed nothing


def test_a_first_pass_creates_one_region_per_column(conn):
    report = _seed(conn)
    assert report.created == 3
    assert len(_live(conn)) == 3
    assert [r.reading_order for r in _live(conn)] == [0, 1, 2]
    assert {r.state for r in _live(conn)} == {RegionState.PROPOSED}


def test_running_the_same_segmentation_again_changes_nothing(conn):
    _seed(conn)
    before = _uids(conn)

    report = _seed(conn)
    assert (report.created, report.moved, report.retired, report.revived) == (
        0,
        0,
        0,
        0,
    )
    assert report.unchanged == 3
    assert _uids(conn) == before  # identity survives, so crops and answers do


def test_reading_order_follows_the_page_right_to_left(conn):
    _seed(conn)
    ordered = _live(conn)
    xs = [r.bbox[2] for r in ordered]
    assert xs == sorted(xs, reverse=True)


# --------------------------------------------------------------------------
# a rerun whose geometry moved


def test_a_shifted_lattice_moves_regions_instead_of_replacing_them(conn):
    """The most common difference between two segmentations of this corpus.

    A change in the comb's phase shifts every boundary a little. Keyed on the
    lattice index this is invisible; keyed on geometry it is three moves and no
    new regions — which is what keeps the answers attached.
    """
    _seed(conn)
    before = _uids(conn)

    shifted = [(i, [x0 + 9, y0, x1 + 9, y1]) for i, (x0, y0, x1, y1) in COLUMNS]
    report = reconcile_page(conn, DOC, PAGE, _proposals(shifted))

    assert (report.moved, report.created, report.retired) == (3, 0, 0)
    assert _uids(conn) == before


def test_an_inserted_column_does_not_rename_its_neighbours(conn):
    """The failure the region table exists to prevent.

    Under positional identity every column left of a new one shifts by one, so
    every answer and every correction beyond the insertion point silently moves
    to the wrong entry.
    """
    _seed(conn)
    before = set(_uids(conn))

    extra = [*COLUMNS, (3, [780.0, BAND_TOP, 1160.0, BAND_BOTTOM])]
    report = reconcile_page(conn, DOC, PAGE, _proposals(extra))

    assert (report.created, report.moved, report.retired) == (1, 0, 0)
    assert before < set(_uids(conn))  # the originals are all still themselves


def test_a_column_the_detector_stops_seeing_is_withdrawn_not_erased(conn):
    _seed(conn)
    dropped = _uids(conn)[-1]

    report = reconcile_page(conn, DOC, PAGE, _proposals(COLUMNS[:2]))
    assert report.retired == 1
    assert dropped not in _uids(conn)

    row = conn.execute(
        "SELECT deleted_at FROM regions WHERE region_uid = ?", (dropped,)
    ).fetchone()
    assert row["deleted_at"] is not None  # kept, because answers were read from it


def test_a_column_that_comes_back_comes_back_as_itself(conn):
    """A page re-segmented under a better pitch should not lose its history."""
    _seed(conn)
    dropped = _uids(conn)[-1]
    reconcile_page(conn, DOC, PAGE, _proposals(COLUMNS[:2]))

    report = _seed(conn)
    assert report.revived == 1
    assert dropped in _uids(conn)


def test_a_live_adjusted_replacement_wins_over_an_exact_tombstone(conn):
    """A rerun must not restore a historical duplicate beside a human edit.

    A half-phase repair can replace a machine region rather than move it when
    overlap falls below the identity threshold.  The new adjusted row and the
    retired machine row then have identical geometry.  The live edit owns that
    proposal; insertion order must not resurrect the older tombstone.
    """
    _seed(conn)
    old = _live(conn)[1]
    store.retire(conn, old)
    replacement = store.create_region(
        conn,
        DOC,
        PAGE,
        old.bbox,
        band_label=old.band_label,
        band_ordinal=old.band_ordinal,
        reading_order=old.reading_order,
        entry_index=old.entry_index,
        state=RegionState.ADJUSTED,
        created_by="reviewer",
    )

    report = _seed(conn)

    assert report.revived == 0
    assert replacement.region_uid in _uids(conn)
    assert old.region_uid not in _uids(conn)
    assert len(_live(conn)) == len(COLUMNS)
    assert report.links[(0, 1)] == replacement.id


# --------------------------------------------------------------------------
# a rerun over work somebody has done


def _edit(conn, uid, bbox, state=RegionState.ADJUSTED):
    store.set_geometry(conn, uid, bbox, actor="reviewer", state=state)


def test_an_edited_box_is_not_moved_by_a_rerun(conn):
    _seed(conn)
    uid = _uids(conn)[1]
    _edit(conn, uid, [1500.0, BAND_TOP, 1900.0, BAND_BOTTOM])
    edited = store.get(conn, uid).bbox

    report = _seed(conn)
    assert store.get(conn, uid).bbox == edited
    assert store.get(conn, uid).state == RegionState.ADJUSTED
    assert uid in report.divergent
    assert report.moved == 0


def test_a_verified_box_is_not_moved_by_a_rerun(conn):
    _seed(conn)
    uid = _uids(conn)[1]
    _edit(conn, uid, [1500.0, BAND_TOP, 1900.0, BAND_BOTTOM], RegionState.VERIFIED)

    _seed(conn)
    assert store.get(conn, uid).state == RegionState.VERIFIED
    assert store.get(conn, uid).bbox == [1500.0, BAND_TOP, 1900.0, BAND_BOTTOM]


def test_the_disagreement_is_recorded_so_it_can_be_offered(conn):
    _seed(conn)
    uid = _uids(conn)[1]
    _edit(conn, uid, [1500.0, BAND_TOP, 1900.0, BAND_BOTTOM])
    _seed(conn)

    row = conn.execute(
        "SELECT kind, bbox_json, iou FROM region_proposals WHERE kind='divergence'"
    ).fetchone()
    assert row is not None
    assert json.loads(row["bbox_json"]) == COLUMNS[1][1]  # what the machine wanted
    assert row["iou"] > MATCH_IOU


def test_an_edited_region_nothing_proposes_is_kept_and_reported(conn):
    _seed(conn)
    uid = _uids(conn)[2]
    _edit(conn, uid, [1160.0, BAND_TOP, 1540.0, BAND_BOTTOM], RegionState.VERIFIED)

    report = reconcile_page(conn, DOC, PAGE, _proposals(COLUMNS[:2]))
    assert report.retired == 0
    assert uid in report.orphaned
    assert uid in _uids(conn)
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM region_proposals WHERE kind='orphan_region'"
        ).fetchone()[0]
        == 1
    )


def test_an_edit_that_agrees_with_the_machine_raises_nothing(conn):
    """Approving a proposal as-is must not then read as a disagreement."""
    _seed(conn)
    uid = _uids(conn)[1]
    store.set_geometry(conn, uid, COLUMNS[1][1], state=RegionState.VERIFIED)

    report = _seed(conn)
    assert report.divergent == [uid]  # reported as pinned…
    assert (
        conn.execute("SELECT COUNT(*) FROM region_proposals").fetchone()[0] == 0
    )  # …but there is nothing to disagree about


# --------------------------------------------------------------------------
# reading order


def test_a_rerun_renumbers_reading_order_from_the_page(conn):
    _seed(conn)
    extra = [(3, [780.0, BAND_TOP, 1160.0, BAND_BOTTOM]), *COLUMNS]
    reconcile_page(conn, DOC, PAGE, _proposals(extra))
    assert [r.reading_order for r in _live(conn)] == [0, 1, 2, 3]


def test_a_rerun_does_not_overrule_an_order_somebody_set(conn):
    _seed(conn)
    conn.execute(
        "UPDATE regions SET reading_order_locked = 1, reading_order = 9 "
        "WHERE reading_order = 0"
    )
    report = _seed(conn)
    assert "庶" in report.order_locked
    assert 9 in [r.reading_order for r in _live(conn)]


# --------------------------------------------------------------------------
# looking without touching


def test_a_dry_run_reports_the_same_thing_and_writes_nothing(conn):
    _seed(conn)
    before = [(r.region_uid, r.bbox) for r in _live(conn)]

    shifted = [(i, [x0 + 9, y0, x1 + 9, y1]) for i, (x0, y0, x1, y1) in COLUMNS]
    dry = reconcile_page(conn, DOC, PAGE, _proposals(shifted), dry_run=True)
    assert dry.moved == 3
    assert [(r.region_uid, r.bbox) for r in _live(conn)] == before

    wet = reconcile_page(conn, DOC, PAGE, _proposals(shifted))
    assert (wet.moved, wet.created, wet.retired) == (
        dry.moved,
        dry.created,
        dry.retired,
    )


# --------------------------------------------------------------------------
# bands


def test_columns_are_matched_within_their_own_band(conn):
    """Two bands print columns at the same x, and they are different people."""
    both = [*_proposals(), *_proposals(band=1, label="富")]
    reconcile_page(conn, DOC, PAGE, both)
    assert len(_live(conn)) == 6
    assert sorted(r.band_label for r in _live(conn)) == ["富"] * 3 + ["庶"] * 3

    report = reconcile_page(conn, DOC, PAGE, both)
    assert report.unchanged == 6 and report.created == 0


def test_proposals_are_the_whole_page(conn):
    """A band left out of the proposal is a band the detector says is empty.

    Worth pinning down because the alternative reading — "only reconcile what I
    passed" — would make a caller that filters by band silently withdraw
    everything else, and the two are indistinguishable from the call site.
    """
    both = [*_proposals(), *_proposals(band=1, label="富")]
    reconcile_page(conn, DOC, PAGE, both)

    report = reconcile_page(conn, DOC, PAGE, _proposals(band=1, label="富"))
    assert report.retired == 3
    assert {r.band_label for r in _live(conn)} == {"富"}


def test_each_proposal_reports_the_region_it_became(conn):
    """Crop rows link to a region by this map rather than by lattice position."""
    report = _seed(conn)
    assert sorted(report.links) == [(0, 0), (0, 1), (0, 2)]
    ids = {r.id for r in _live(conn)}
    assert set(report.links.values()) == ids


# --------------------------------------------------------------------------
# what the readers see afterwards


def _answer(conn, region_id, source_region_id, text):
    conn.execute(
        "INSERT INTO models (id, name, version, backend) VALUES ('m','b','1','b') "
        "ON CONFLICT(id) DO NOTHING"
    )
    conn.execute(
        "INSERT INTO ocr_runs (id, run_id, model_id, input_variant, tag) "
        "VALUES (1, NULL, 'm', 'maxrgb', 't') ON CONFLICT(id) DO NOTHING"
    )
    conn.execute(
        "INSERT INTO ocr_candidates (region_id, source_region_id, ocr_run_id, "
        "crop_key, transcription) VALUES (?,?,1,?,?)",
        (region_id, source_region_id, f"key{region_id}", text),
    )


def _crop_rows(conn, region_ids, start_id):
    """Stand in for a segmentation pass writing this page's crop rows."""
    conn.execute("DELETE FROM source_regions WHERE document_id = ?", (DOC,))
    for offset, region_id in enumerate(region_ids):
        conn.execute(
            "INSERT INTO source_regions (id, document_id, page_index, role, context, "
            "bbox_json, crop_id, region_id) VALUES (?,?,?,'entry','tight','[]',?,?)",
            (start_id + offset, DOC, PAGE, f"crop{offset}", region_id),
        )
        conn.execute(
            "INSERT INTO region_crops (region_id, geometry_hash, context, pad_frac, "
            "variant, pixel_bbox_json, crop_key, path, created_at) "
            "VALUES (?,'h','tight',0.0,'maxrgb','[]',?,?,'now') "
            "ON CONFLICT(crop_key) DO NOTHING",
            (region_id, f"key{region_id}", f"/crops/{region_id}.png"),
        )


def test_an_answer_is_still_readable_after_the_page_is_segmented_again(conn):
    """The bug that only showed on real data.

    Answers hang off regions and survive a rerun, but every report read them
    through the crop row they were cut from — and those rows are replaced. The
    transcriptions stayed in the database and vanished from the genealogy, which
    is worse than losing them, because nothing said so.
    """
    _seed(conn)
    regions = [r.id for r in _live(conn)]
    _crop_rows(conn, regions, start_id=100)
    for i, region_id in enumerate(regions):
        _answer(conn, region_id, 100 + i, f"text{i}")

    # Segment again: same geometry, but the crop rows are new rows.
    _seed(conn)
    _crop_rows(conn, regions, start_id=200)

    found = conn.execute(
        "SELECT COUNT(*) FROM regions r JOIN ocr_candidates oc ON oc.region_id = r.id "
        "WHERE r.document_id = ? AND r.deleted_at IS NULL",
        (DOC,),
    ).fetchone()[0]
    assert found == 3


def test_the_breadcrumb_is_re_aimed_at_the_new_crop_rows(conn):
    """Readers that still go through the crop row keep working."""
    from foresight_ocr.cli.main import _repoint_breadcrumbs

    _seed(conn)
    regions = [r.id for r in _live(conn)]
    _crop_rows(conn, regions, start_id=100)
    for i, region_id in enumerate(regions):
        _answer(conn, region_id, 100 + i, f"text{i}")

    _crop_rows(conn, regions, start_id=200)
    assert _repoint_breadcrumbs(conn, DOC, [PAGE]) == 3

    stale = conn.execute(
        "SELECT COUNT(*) FROM ocr_candidates c WHERE c.source_region_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM source_regions sr WHERE sr.id = c.source_region_id)"
    ).fetchone()[0]
    assert stale == 0
