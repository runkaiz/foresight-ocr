"""Reconstructing people and father links from parsed entries."""

from __future__ import annotations

import pytest

from foresight_ocr.context import set_profile
from foresight_ocr.document.profile import DocumentProfile
from foresight_ocr.genealogy import build_entries, build_graph, person_key
from foresight_ocr.genealogy.graph import named_father_key

PROFILE = DocumentProfile(
    document_id="test",
    band_labels=["庶", "富", "教"],
    generation_chain=["允", "庶", "富", "教"],
    bands_per_page=3,
)
CHARTED = set(PROFILE.band_labels)
PARENT_OF = {label: PROFILE.parent_of(label) for label in PROFILE.band_labels}


@pytest.fixture(autouse=True)
def _profile():
    set_profile(PROFILE)


def _rows(*specs):
    """specs: (page, band_index, entry_index, text)"""
    return [
        {
            "region_uid": f"uid{i}",
            "page_index": p,
            "band_index": b,
            "entry_index": e,
            "text": t,
            "source": "ocr",
        }
        for i, (p, b, e, t) in enumerate(specs)
    ]


def _graph(*specs):
    entries = build_entries(_rows(*specs), PROFILE.band_map(), PARENT_OF)
    return entries, build_graph(entries, PARENT_OF, CHARTED)


def test_a_numbered_father_resolves_to_his_record():
    _, g = _graph(
        (1, 0, 0, "庶三十五 允二十 長子"),
        (1, 1, 0, "富十二 庶三十五 次子"),
    )
    assert g.people == 2
    assert g.link_status["resolved"] == 1  # 富十二 -> 庶三十五
    assert g.link_status["outside_volume"] == 1  # 庶's father 允 is not in this volume


def test_the_oldest_generation_is_not_counted_as_broken():
    # Every 庶 entry names a 允 father who lives in the previous volume. Calling
    # that unresolved reported the whole first generation as broken links.
    _, g = _graph((1, 0, 0, "庶三十五 允二十 長子"))
    assert g.link_status.get("unresolved", 0) == 0
    assert g.link_status["outside_volume"] == 1
    assert not [f for f in g.findings if f.kind == "unresolved_father"]


def test_a_father_id_nobody_carries_is_a_finding():
    _, g = _graph((1, 1, 0, "富十二 庶九百九十 長子"))
    kinds = [f.kind for f in g.findings]
    assert "unresolved_father" in kinds


def test_one_id_belongs_to_one_person():
    _, g = _graph(
        (1, 1, 0, "富十二 庶三十五 長子"),
        (2, 1, 0, "富十二 庶四十 次子"),
    )
    assert g.people == 1
    assert [f.kind for f in g.findings].count("duplicate_id") == 1


def test_generation_comes_from_the_printed_id_not_the_band_position():
    # A page carrying only two bands puts 教 at band index 1. Keying that person
    # as 富 invents a collision with a real 富 elsewhere in the book.
    entries, _ = _graph((10, 1, 0, "教五十 富三十九 三子"))
    assert entries[0].band_label == "教"


def test_sons_of_one_father_may_not_share_a_rank():
    # The real case: 五子 read as 子, which ranks first and collides with 長子.
    _, g = _graph(
        (1, 1, 0, "富十 庶三 長子"),
        (2, 2, 0, "教二十 富十 長子"),
        (3, 2, 0, "教三十 富十 子"),
    )
    conflicts = [f for f in g.findings if f.kind == "order_conflict"]
    assert len(conflicts) == 1
    assert conflicts[0].page_index == 3


def test_ranks_must_ascend_with_ids():
    _, g = _graph(
        (1, 1, 0, "富十 庶三 長子"),
        (2, 2, 0, "教二十 富十 三子"),
        (3, 2, 0, "教三十 富十 次子"),
    )
    assert [f.kind for f in g.findings].count("order_reversed") == 1


def test_a_named_father_groups_his_sons():
    # He has no record to link to, but his sons are still brothers.
    _, g = _graph(
        (1, 2, 0, "教二十 光煬 長子"),
        (2, 2, 0, "教三十 光煬 次子"),
    )
    assert g.link_status["named_only"] == 2
    assert not [f for f in g.findings if f.kind == "unresolved_father"]


def test_family_keys_cannot_collide_with_person_keys():
    assert named_father_key("富", "存省") != person_key("富", 349)
    assert ":" not in named_father_key("富", "存省").split("#")[1]
