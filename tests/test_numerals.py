import pytest

from familyocr.validation.numerals import (
    format_numeral,
    parse_entry_id,
    parse_numeral,
)


@pytest.mark.parametrize(
    "text,value",
    [
        ("一", 1),
        ("十", 10),
        ("十二", 12),
        ("二十", 20),
        ("廿", 20),
        ("廿二", 22),
        ("卅五", 35),
        ("五十九", 59),
        ("六十", 60),
        ("百", 100),
        ("一百", 100),
        ("三百四十九", 349),
        ("五百八十七", 587),
        ("千", 1000),
        # Leading unit with an implied 一 — how this book writes 1193.
        ("千百九十三", 1193),
        ("一千一百九十四", 1194),
        ("二千", 2000),
    ],
)
def test_parse_numeral(text, value):
    assert parse_numeral(text) == value


@pytest.mark.parametrize("text", ["", "長子", "三廿", "abc", "五十X"])
def test_parse_numeral_rejects_garbage(text):
    assert parse_numeral(text) is None


def test_parse_entry_id_splits_band():
    parsed = parse_entry_id("庶五百八十七")
    assert parsed.ok
    assert parsed.band == "庶"
    assert parsed.value == 587


def test_parse_entry_id_without_band():
    parsed = parse_entry_id("三百四十九")
    assert parsed.ok and parsed.band is None and parsed.value == 349


def test_parse_entry_id_reports_failure_instead_of_guessing():
    parsed = parse_entry_id("庶三百四十X")
    assert not parsed.ok
    assert parsed.value is None


@pytest.mark.parametrize("value", [1, 9, 10, 12, 20, 59, 100, 349, 587, 1000, 1193])
def test_format_roundtrip(value):
    assert parse_numeral(format_numeral(value)) == value
