from foresight_ocr.ocr.base import OCRResult
from foresight_ocr.ocr.fields import parse_entry
from foresight_ocr.ocr.watermarks import filter_watermark_text


def test_logo_only_reading_becomes_empty():
    got = filter_watermark_text("图书馆 LIBRARY")
    assert got.transcription is None
    assert got.changed


def test_watermark_suffix_is_removed_without_touching_genealogy_text():
    got = filter_watermark_text("富九百二十 欽 長子 FUYANG")
    assert got.transcription == "富九百二十 欽 長子"


def test_misread_latin_fragments_are_removed_without_touching_entry_text():
    assert (
        filter_watermark_text("庶二富\n几\n大日子\nFUYAN").transcription
        == "庶二富\n几\n大日子"
    )
    assert (
        filter_watermark_text("富\n庶十四\n七\n繼子\nG LIBRAF").transcription
        == "富\n庶十四\n七\n繼子"
    )


def test_short_logo_fragments_are_removed_only_with_definitive_latin_match():
    assert filter_watermark_text("富旺FUYA").transcription is None
    assert filter_watermark_text("图书\nG LIBRAI").transcription is None
    assert filter_watermark_text("富旺").transcription == "富旺"
    assert (
        filter_watermark_text("富千三百八十一\n三十\nBRAR").transcription
        == "富千三百八十一\n三十"
    )


def test_mixed_traditional_watermark_fragments_are_removed():
    got = filter_watermark_text("教三百一書館圖書館長子")
    assert got.transcription == "教三百一長子"


def test_partial_logo_remnants_are_removed_only_after_a_definitive_match():
    assert filter_watermark_text("F 阳 FUYANG").transcription is None
    assert filter_watermark_text("富陽").transcription == "富陽"


def test_ocr_result_keeps_unfiltered_model_output_in_provenance():
    result = OCRResult(
        crop_id="c",
        transcription="富九百二十長子 FUYANG",
        backend="fake",
        model_version="1",
        raw={"prompt": "OCR:"},
    )
    assert result.transcription == "富九百二十長子"
    assert result.raw["unfiltered_transcription"] == "富九百二十長子 FUYANG"
    assert result.raw["ignored_watermark_fragments"] == ["FUYANG"]


def test_field_parser_ignores_watermark_but_keeps_raw_text():
    raw = "教三百一書館圖書館長子"
    parsed = parse_entry(raw, own_label="教")
    assert parsed.own_id == "教三百一"
    assert parsed.order == "長子"
    assert parsed.leftover == ""
    assert parsed.text == raw
    assert parsed.watermark_noise == ["圖書館", "書館"]
