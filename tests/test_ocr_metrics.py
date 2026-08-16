import math

from familyocr.ocr.fields import parse_entry
from familyocr.ocr.metrics import Pair, align, rare_characters, score_pairs


def test_parse_entry_splits_the_three_printed_fields():
    e = parse_entry("庶三百三十五允二百八十六次子")
    assert e.own_id == "庶三百三十五"
    assert e.parent_id == "允二百八十六"
    assert e.order == "次子"
    assert e.leftover == ""


def test_a_named_father_replaces_a_missing_id():
    # The page names the father when his generation id is unknown, so this is a
    # parent reference rather than unrecognised text.
    e = parse_entry("庶三百三十五仕燧長子", own_label="庶")
    assert e.own_id == "庶三百三十五"
    assert e.parent_id is None
    assert e.parent_name == "仕燧"
    assert e.order == "長子"
    assert e.leftover == ""


def test_parse_entry_on_empty_input():
    e = parse_entry(None)
    assert (e.own_id, e.parent_id, e.order) == (None, None, None)


def test_order_marker_is_not_stolen_from_an_id():
    # 庶三百 ends in 百, and 子 follows the parent id — the order marker must not
    # be built from characters already claimed by an id.
    e = parse_entry("庶三百允九長子")
    assert e.own_id == "庶三百"
    assert e.parent_id == "允九"
    assert e.order == "長子"


def test_align_counts_a_substitution_not_an_indel():
    c = align("庶三百四十九", "庶三百四十八")
    assert (c.substitutions, c.insertions, c.deletions) == (1, 0, 0)
    assert c.matches == 5


def test_align_counts_deletions():
    c = align("庶三百四十九", "庶三百四十")
    assert c.deletions == 1 and c.substitutions == 0


def test_score_marks_an_unread_crop_as_deletions_not_a_free_pass():
    pairs = [Pair("a", "庶三百四十九", None, error="no text detected")]
    s = score_pairs(pairs, "b", "v", "tight")
    assert s.unknown_rate == 1.0
    assert s.cer == 1.0
    assert s.exact_entry == 0.0


def test_exact_field_accuracy_is_stricter_than_cer():
    # One wrong digit: CER looks mild, the id field is simply wrong.
    pairs = [Pair("a", "庶三百四十九", "庶三百四十八")]
    s = score_pairs(pairs, "b", "v", "tight")
    assert 0 < s.cer < 0.2
    assert s.exact_entry == 0.0
    assert s.field_exact["own_id"] == 0.0


def test_substitutions_are_reported_separately_from_indels():
    pairs = [Pair("a", "庶三百四十九", "庶三百四十八")]
    s = score_pairs(pairs, "b", "v", "tight")
    assert s.substitution_rate > 0
    assert s.deletion_rate == 0 and s.insertion_rate == 0


def test_rare_characters_picks_out_name_glyphs():
    refs = ["庶一長子", "庶二長子", "庶三長子仕燧"]
    rare = rare_characters(refs, max_count=1)
    assert "仕" in rare and "燧" in rare
    assert "庶" not in rare and "長" not in rare


def test_rare_char_accuracy_is_none_without_rare_characters():
    s = score_pairs([Pair("a", "庶一", "庶一")], "b", "v", "tight")
    assert s.rare_char_accuracy is None


def test_perfect_run_scores_clean():
    pairs = [Pair("a", "庶一長子", "庶一長子"), Pair("b", "庶二次子", "庶二次子")]
    s = score_pairs(pairs, "b", "v", "tight")
    assert s.cer == 0.0
    assert s.exact_entry == 1.0
    assert s.field_exact["own_id"] == 1.0
    assert math.isclose(s.unknown_rate, 0.0)


def test_bare_zi_is_an_only_son_and_ranks_first():
    e = parse_entry("庶三百四十允二百十九子", own_label="庶")
    assert e.own_id == "庶三百四十"
    assert e.parent_id == "允二百十九"
    # Recorded exactly as printed, ranked without rewriting it.
    assert e.order == "子"
    assert e.order_rank == 1


def test_order_ranks():
    ranks = {
        "長子": 1, "元子": 1, "次子": 2, "三子": 3, "四子": 4, "子": 1, "女": 1,
    }
    for marker, rank in ranks.items():
        e = parse_entry(f"庶一{marker}", own_label="庶")
        assert e.order_rank == rank, (marker, e.order, e.order_rank)


def test_a_named_father_is_captured_instead_of_an_id():
    # When the father's generation id is unknown the page names him instead.
    e = parse_entry("敎二百八十九李芳長子", own_label="教")
    assert e.own_id == "教二百八十九"
    assert e.parent_id is None
    assert e.parent_name == "李芳"
    assert e.leftover == ""


def test_a_numbered_father_is_not_mistaken_for_a_name():
    e = parse_entry("富二百九十三庶二百五十一次子", own_label="富")
    assert e.parent_id == "庶二百五十一"
    assert e.parent_name is None


def test_long_leftover_is_not_treated_as_a_name():
    # Watermark bleed-through must not be promoted to a father's name.
    e = parse_entry("教三百一書館圖書館長子", own_label="教")
    assert e.parent_name is None
    assert e.leftover


def test_variant_band_label_is_recorded_not_silently_folded():
    e = parse_entry("敎二百八十九李芳長子", own_label="教")
    assert e.label_variant == {"敎": "教"}


def test_lookalike_label_still_fails_loudly():
    # 族 resembles 庶 but is a recognition error, not an orthographic variant.
    e = parse_entry("族二百八十七允三百廿八長子", own_label="庶")
    assert e.own_id is None
    assert e.label_variant == {}


def test_band_label_can_come_from_geometry_when_the_glyph_blurs():
    # 庶 misrecognized as 庚. The band is known from rule detection, so the
    # numeral is still usable — but the substitution must be recorded.
    e = parse_entry("庚三百四十允二百十九子", own_label="庶", trust_band=True)
    assert e.own_id == "庶三百四十"
    assert e.label_from_geometry is True
    assert e.observed_label == "庚"
    assert e.text == "庚三百四十允二百十九子"      # raw transcription untouched


def test_geometry_label_is_off_by_default():
    e = parse_entry("庚三百四十允二百十九子", own_label="庶")
    assert e.own_id is None
    assert e.label_from_geometry is False


def test_correct_label_is_not_marked_as_recovered():
    e = parse_entry("庶三百四十允二百十九子", own_label="庶", trust_band=True)
    assert e.own_id == "庶三百四十"
    assert e.label_from_geometry is False
    assert e.observed_label is None


def test_geometry_label_needs_a_leading_numeral_run():
    # Pure noise must not be promoted into an id just because a band is known.
    e = parse_entry("書館圖書館", own_label="庶", trust_band=True)
    assert e.own_id is None
    assert e.label_from_geometry is False


def test_numeral_confusable_is_repaired_and_recorded():
    # 干 differs from 千 by one stroke and is never a numeral; it accounted for
    # 231 of 337 unparsed entries across the volume.
    e = parse_entry("庶干十四允五百六十一子", own_label="庶", trust_band=True)
    assert e.own_id == "庶千十四"
    assert e.numeral_repairs == {"干": "千"}
    assert e.text == "庶干十四允五百六十一子"      # raw transcription untouched


def test_no_repair_recorded_when_none_was_needed():
    e = parse_entry("庶千十六允七百五十七三子", own_label="庶", trust_band=True)
    assert e.own_id == "庶千十六"
    assert e.numeral_repairs == {}


def test_shu_orthographic_variant_is_accepted():
    # 庻 is a printed variant of 庶, unlike 庚/族 which are misreads.
    e = parse_entry("庻三百四十允二百十九子", own_label="庶")
    assert e.own_id == "庶三百四十"
    assert e.label_variant == {"庻": "庶"}


def test_lookalike_numerals_are_not_blanket_rewritten():
    # 大 resembles 六 but occurs in real text, so rewriting it would be guessing.
    e = parse_entry("庶千十大允七百次子", own_label="庶", trust_band=True)
    assert e.own_id != "庶千十六"
