"""Splitting an entry into its printed fields."""

from __future__ import annotations

import pytest

from familyocr.context import set_profile
from familyocr.document.profile import DocumentProfile
from familyocr.ocr.fields import parse_entry

PROFILE = DocumentProfile(
    document_id="test",
    band_labels=["庶", "富", "教"],
    generation_chain=["允", "庶", "富", "教"],
    bands_per_page=3,
)


@pytest.fixture(autouse=True)
def _profile():
    set_profile(PROFILE)


def test_a_broken_id_is_reassembled_when_it_otherwise_would_not_parse():
    # 丙辰庶富教2 often breaks inside the id: the label on one line, its numeral
    # on the next. Half that volume failed to parse for this alone.
    e = parse_entry("武功\n三子\n庶\n干\n二百", own_label="庶")
    assert e.own_id == "庶千二百"
    assert e.parent_name == "武功"
    assert e.order == "三子"
    assert e.text == "武功\n三子\n庶\n干\n二百"     # transcription untouched


def test_joining_is_a_fallback_and_never_reinterprets_a_working_entry():
    # Joined, this reads as father 富十三 and order 子 — both wrong. The spaced
    # form parses, so the fallback must not run.
    e = parse_entry("教二十 富十 三子", own_label="教")
    assert e.parent_id == "富十"
    assert e.order == "三子"


def test_field_order_does_not_matter():
    # 丙辰庶富教1 prints id, father, order; 丙辰庶富教2 prints father, order, id.
    # Fields are found by their labels, so both read the same.
    first = parse_entry("庶千二百一\n武顯\n次子", own_label="庶")
    second = parse_entry("武顯\n次子\n庶千二百一", own_label="庶")
    assert first.own_id == second.own_id == "庶千二百一"
    assert first.parent_name == second.parent_name == "武顯"
    assert first.order == second.order == "次子"
