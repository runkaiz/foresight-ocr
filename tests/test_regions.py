"""Region identity and the addresses derived from it.

The distinction these tests exist to protect: `region_uid` names the thing and
never changes, `geometry_hash` names its current box and changes whenever the
box does. Conflating the two is what made `crop_id` unusable as a key — the name
of a region changed when a neighbour was inserted, so corrections and OCR
answers followed positions rather than entries.
"""

from foresight_ocr.regions.model import (
    Region,
    RegionRole,
    RegionState,
    geometry_hash,
    new_region_uid,
)


def _region(**kw):
    bbox = kw.pop("bbox", [100.0, 0.0, 400.0, 1000.0])
    return Region(
        region_uid=new_region_uid(),
        document_id="doc",
        page_index=58,
        bbox=bbox,
        geometry_hash=geometry_hash("doc", 58, bbox),
        **kw,
    )


def test_uid_is_unique_and_says_nothing_about_the_region():
    a, b = new_region_uid(), new_region_uid()
    assert a != b
    assert len(a) == 32 and int(a, 16) >= 0


def test_geometry_hash_is_stable_for_the_same_box():
    box = [1919.5, 0.0, 2253.5, 1012.710458336632]
    assert geometry_hash("doc", 58, box) == geometry_hash("doc", 58, list(box))


def test_geometry_hash_ignores_noise_below_a_hundredth_of_a_pixel():
    # A float that has been through JSON and back must not read as a moved box.
    assert geometry_hash("doc", 58, [100.0, 0.0, 400.0, 1000.0]) == geometry_hash(
        "doc", 58, [100.0000001, 0.0, 400.0, 1000.0]
    )


def test_geometry_hash_changes_when_the_box_actually_moves():
    a = geometry_hash("doc", 58, [100.0, 0.0, 400.0, 1000.0])
    assert a != geometry_hash("doc", 58, [102.0, 0.0, 400.0, 1000.0])
    assert a != geometry_hash("doc", 59, [100.0, 0.0, 400.0, 1000.0])
    assert a != geometry_hash("other", 58, [100.0, 0.0, 400.0, 1000.0])


def test_moving_a_region_keeps_its_identity_and_renames_its_geometry():
    before = _region()
    after = before.with_bbox([120.0, 0.0, 420.0, 1000.0])
    assert after.region_uid == before.region_uid
    assert after.geometry_hash != before.geometry_hash


def test_pinned_states_are_the_ones_a_rerun_must_not_overwrite():
    assert _region(state=RegionState.PROPOSED).pinned is False
    assert _region(state=RegionState.ADJUSTED).pinned is True
    assert _region(state=RegionState.VERIFIED).pinned is True


def test_a_rejected_or_deleted_region_is_not_live():
    assert _region().live is True
    assert _region(state=RegionState.REJECTED).live is False
    assert _region(deleted_at="2026-08-17T00:00:00+00:00").live is False


def test_default_region_is_an_unreviewed_entry():
    r = _region()
    assert r.role == RegionRole.ENTRY
    assert r.state == RegionState.PROPOSED
    assert r.created_by == "machine"
