"""Splitting an entry into its printed fields."""

from __future__ import annotations

import pytest

from foresight_ocr.context import set_profile
from foresight_ocr.document.profile import DocumentProfile
from foresight_ocr.ocr.fields import parse_entry

PROFILE = DocumentProfile(
    document_id="test",
    band_labels=["庶", "富", "教"],
    generation_chain=["允", "庶", "富", "教"],
    bands_per_page=3,
)


@pytest.fixture(autouse=True)
def _profile():
    set_profile(PROFILE)
    yield
    set_profile(PROFILE)


def test_a_broken_id_is_reassembled_when_it_otherwise_would_not_parse():
    # 丙辰庶富教2 often breaks inside the id: the label on one line, its numeral
    # on the next. Half that volume failed to parse for this alone.
    e = parse_entry("武功\n三子\n庶\n干\n二百", own_label="庶")
    assert e.own_id == "庶千二百"
    assert e.parent_name == "武功"
    assert e.order == "三子"
    assert e.text == "武功\n三子\n庶\n干\n二百"  # transcription untouched


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


def test_title_page_subcolumns_rejoin_a_split_own_id_without_guessing():
    e = parse_entry("庶\n允六\n二\n長子", own_label="庶", trust_band=True)
    assert e.own_id == "庶二"
    assert e.parent_id == "允六"
    assert e.order == "長子"
    assert e.leftover == ""


def test_split_own_id_requires_an_explicit_numeral_line():
    e = parse_entry("允一\n長子", own_label="庶", trust_band=True)
    assert e.own_id is None
    assert e.parent_id == "允一"
    assert e.order == "長子"


def test_birthday_hour_is_additional_information_not_birth_order():
    e = parse_entry(
        "教二千百六十七\n生於民國丙辰年\n七月十九日子時\n富千八百七十七",
        own_label="教",
    )
    assert e.own_id == "教二千百六十七"
    assert e.parent_id == "富千八百七十七"
    assert e.order is None
    assert "七月十九日子時" in e.leftover


def test_stepson_marker_is_birth_order_not_additional_information():
    e = parse_entry("富廿八庶四十一繼子", own_label="富")

    assert e.own_id == "富廿八"
    assert e.parent_id == "庶四十一"
    assert e.order == "繼子"
    assert e.order_rank is None
    assert e.leftover == ""


def test_parent_line_is_not_reused_as_own_id_when_own_label_is_blurred():
    e = parse_entry("允五十八\n庚五十五\n次子", own_label="庶", trust_band=True)

    assert e.own_id == "庶五十五"
    assert e.parent_id == "允五十八"
    assert e.label_from_geometry is True
    assert e.observed_label == "庚"


def test_known_parent_anchors_a_split_blurred_own_id():
    e = parse_entry("允六\n族\n十\n次子", own_label="庶", trust_band=True)

    assert e.own_id == "庶十"
    assert e.parent_id == "允六"
    assert e.observed_label == "族"
    assert e.leftover == ""


def test_blurred_parent_label_is_recovered_only_with_structure_enabled():
    set_profile(
        DocumentProfile(
            document_id="learned",
            band_labels=["庶", "富", "教"],
            generation_chain=["允", "庶", "富", "教"],
            bands_per_page=3,
            label_confusions={"紫": "庶"},
        )
    )
    raw = "紫四十七\n富廿五\n長子"
    strict = parse_entry(raw, own_label="富", trust_band=False)
    structured = parse_entry(raw, own_label="富", trust_band=True)

    assert strict.parent_id is None
    assert structured.parent_id == "庶四十七"
    assert structured.parent_label_from_structure is True
    assert structured.observed_parent_label == "紫"
    assert structured.text == raw


def test_visible_split_parent_id_is_reassembled_without_sequence_guessing():
    e = parse_entry("庶\n八\n富\n十\n五\n長子", own_label="富", trust_band=True)

    assert e.own_id == "富十五"
    assert e.parent_id == "庶八"
    assert e.order == "長子"


def test_document_numeral_confusions_apply_only_inside_id_tokens():
    set_profile(
        DocumentProfile(
            document_id="learned",
            band_labels=["庶", "富", "教"],
            generation_chain=["允", "庶", "富", "教"],
            bands_per_page=3,
            numeral_confusions={"大": "六", "甘": "廿"},
        )
    )

    e = parse_entry("富十大\n教十七\n大公生於甲午", own_label="教", trust_band=True)

    assert e.parent_id == "富十六"
    assert e.numeral_repairs == {"大": "六"}
    assert "大公生於甲午" in e.leftover
    assert e.text == "富十大\n教十七\n大公生於甲午"
