"""The transcription workstation: fields, numerals, and the export.

The property that matters is the round trip. A reviewer edits three fields, what
gets stored is a line of the page, and reading that line back gives the same
three fields. If that breaks, editing a field silently rewrites the record into
something the parser no longer agrees with.
"""

import sqlite3

import pytest

from familyocr.context import set_profile
from familyocr.document.profile import DocumentProfile
from familyocr.ocr.fields import compose_entry, own_id_from_digits, parse_entry
from familyocr.persistence.db import init_schema
from familyocr.regions import store
from familyocr.review.data import (
    export_document,
    page_entries,
    page_summary,
    save_correction,
)

PROFILE = DocumentProfile(
    document_id="doc",
    band_labels=["庶", "富", "教"],
    generation_chain=["允", "庶", "富", "教"],
    bands_per_page=3,
)


@pytest.fixture(autouse=True)
def _profile():
    set_profile(PROFILE)


# --------------------------------------------------------------------------
# fields in, transcription out, fields back


@pytest.mark.parametrize("own,parent,order", [
    ("庶三百三十五", "允二百八十六", "次子"),
    ("教千百九十三", "富三百四十九", "長子"),
    ("庶四十", "允二十七", "子"),          # a bare 子 is an only son, and printed
    ("富十九", "武功", "三子"),            # father named rather than numbered
])
def test_editing_fields_still_stores_a_line_of_the_page(own, parent, order):
    text = compose_entry(own, parent, order)
    back = parse_entry(text, own_label=own[0])
    assert back.own_id == own
    assert (back.parent_id or back.parent_name) == parent
    assert back.order == order
    assert back.leftover == ""


def test_a_missing_field_is_simply_absent():
    assert compose_entry("庶四十", None, "長子") == "庶四十長子"
    assert compose_entry(None, None, None) == ""


def test_composing_drops_what_the_page_does_not_print():
    """The library stamp reads as text; it is not the reviewer's to keep.

    The recognizer returns `富四十四 庚子 長子 IBRARY` on stamped pages. Once the
    reviewer has put the three fields right, the stamp is simply not among them.
    """
    assert compose_entry("富四十四", "庶六十七", "次子") == "富四十四庶六十七次子"


# --------------------------------------------------------------------------
# typing digits


def test_digits_become_the_numeral_the_book_prints():
    assert own_id_from_digits("庶", "343") == "庶三百四十三"
    assert own_id_from_digits("富", "19") == "富十九"
    assert own_id_from_digits("教", "1193") == "教千百九十三"


def test_a_typed_numeral_parses_back_to_the_same_number():
    from familyocr.validation.numerals import parse_entry_id

    for n in (1, 7, 10, 19, 40, 100, 335, 999, 1193):
        text = own_id_from_digits("庶", str(n))
        assert parse_entry_id(text).value == n


def test_cjk_input_is_left_alone():
    # A reviewer typing the characters directly must not be second-guessed.
    assert own_id_from_digits("庶", "三百四十三") is None
    assert own_id_from_digits("庶", "") is None
    assert own_id_from_digits("庶", "43a") is None


# --------------------------------------------------------------------------
# what the reviewer sees, and what comes out at the end


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    conn.execute(
        "INSERT INTO documents VALUES ('doc','t','p','c',1,'now')"
    )
    layout = conn.execute(
        "INSERT INTO page_layouts (document_id, page_index) VALUES ('doc', 58)"
    ).lastrowid
    band = conn.execute(
        "INSERT INTO bands (page_layout_id, band_index, label, bbox_json) "
        "VALUES (?,0,'庶','[]')", (layout,)
    ).lastrowid
    conn.execute("INSERT INTO models VALUES ('m','b','1','b')")
    conn.execute(
        "INSERT INTO ocr_runs (id, run_id, model_id, input_variant, tag) "
        "VALUES (1,NULL,'m','maxrgb','t')"
    )
    for entry, text in enumerate(["庶三百三十五允二百八十六次子", "庶\n允四\n六\n次子"]):
        bbox = [1000.0 - 300 * entry, 0.0, 1300.0 - 300 * entry, 900.0]
        region = store.create_region(
            conn, "doc", 58, bbox,
            band_label="庶", band_ordinal=0, reading_order=entry, entry_index=entry,
        )
        conn.execute(
            "INSERT INTO region_crops (region_id, geometry_hash, context, pad_frac, "
            "variant, pixel_bbox_json, crop_key, path, created_at) "
            "VALUES (?,?, 'tight', 0.0, 'maxrgb', '[]', ?, ?, 'now')",
            (region.id, region.geometry_hash, f"c{entry}", f"/crops/c{entry}.png"),
        )
        conn.execute(
            "INSERT INTO ocr_candidates (region_id, ocr_run_id, crop_key, "
            "transcription) VALUES (?,1,?,?)", (region.id, f"c{entry}", text)
        )
    conn.commit()
    return conn


def test_the_reviewer_gets_the_fields_already_split():
    conn = _db()
    first, second = page_entries(conn, "doc", 58)
    assert first.own_id == "庶三百三十五"
    assert first.parent == "允二百八十六"
    assert first.birth_order == "次子"
    assert first.own_label == "庶" and first.parent_label == "允"


def test_an_entry_the_parser_cannot_place_still_shows_its_reading():
    """The hard pages read every glyph and scramble the order across lines.

    Nothing can be pre-filled, so what the reviewer needs is the reading itself
    — the characters are all there, and retyping an entry whose pieces are on
    screen is the waste this tool exists to remove.
    """
    conn = _db()
    second = page_entries(conn, "doc", 58)[1]
    assert second.own_id is None          # 庶 and 六 are on different lines
    assert second.machine == "庶\n允四\n六\n次子"
    assert "庶" in (second.leftover or "")


def test_a_correction_replaces_the_reading_for_that_entry_only():
    conn = _db()
    save_correction(conn, "doc", 58, "庶", 1,
                    transcription=compose_entry("庶六", "允四", "次子"))
    first, second = page_entries(conn, "doc", 58)
    assert second.human == "庶六允四次子"
    assert second.own_id == "庶六" and second.parent == "允四"
    assert second.machine == "庶\n允四\n六\n次子"   # untouched
    assert first.human is None


def test_the_page_announces_how_much_is_disputed(tmp_path):
    conn = _db()
    conn.execute(
        "INSERT INTO validation_findings (document_id, band_label, kind, page_index, "
        "entry_index, expected) VALUES ('doc','庶','gap',58,1,'三百三十六')"
    )
    conn.commit()
    assert page_summary(conn, "doc") == [
        {"page": 58, "entries": 2, "flagged": 1, "reviewed": 0}
    ]
    entry = page_entries(conn, "doc", 58)[1]
    assert entry.flagged is True
    # The label goes back on, because the checker works inside one band and the
    # reviewer needs an id they can accept as it stands.
    assert entry.expected_own_id == "庶三百三十六"


def test_the_export_is_the_whole_document_not_only_the_corrections(tmp_path):
    conn = _db()
    save_correction(conn, "doc", 58, "庶", 1,
                    transcription=compose_entry("庶六", "允四", "次子"))
    out = tmp_path / "doc.tsv"
    counts = export_document(conn, "doc", out)

    assert counts["entries"] == 2
    assert counts["human"] == 1 and counts["machine"] == 1
    body = [l for l in out.read_text(encoding="utf-8").splitlines()
            if not l.startswith("#")]
    assert len(body) == 2
    assert body[0].split("\t")[-1] == "machine"
    assert body[1].split("\t")[-1] == "human"
    # Fields are columns of their own, so the output is usable without reparsing.
    assert body[1].split("\t")[3:6] == ["庶六", "允四", "次子"]


def test_an_unreadable_entry_is_exported_as_unreadable_not_omitted(tmp_path):
    """A column that defeated a careful reader is a finding, not a gap."""
    conn = _db()
    save_correction(conn, "doc", 58, "庶", 0, transcription=None, unreadable=True)
    out = tmp_path / "doc.tsv"
    counts = export_document(conn, "doc", out)
    assert counts["entries"] == 2 and counts["unreadable"] == 1
    body = [l for l in out.read_text(encoding="utf-8").splitlines()
            if not l.startswith("#")]
    assert len(body) == 2
    assert body[0].split("\t")[-1] == "unreadable"
