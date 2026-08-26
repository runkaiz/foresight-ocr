"""A band's id run, separated from recognizer strays."""

from __future__ import annotations

from foresight_ocr.cli.main import _core_run


def test_a_single_stray_does_not_stretch_the_run():
    # The real case: 富 ids 2..1198 plus one misread 5105, which made every
    # integer up to 5105 look like a missing person.
    values = set(range(2, 1199)) | {5105}
    core, outliers = _core_run(values)
    assert outliers == {5105}
    assert max(core) == 1198


def test_a_clean_band_keeps_every_member():
    values = set(range(1, 500))
    core, outliers = _core_run(values)
    assert core == values
    assert outliers == set()


def test_genuine_gaps_survive():
    values = set(range(1, 200)) - {50, 51, 120}
    core, outliers = _core_run(values)
    assert outliers == set()
    assert {50, 51, 120} & core == set()


def test_small_bands_are_left_alone():
    # Too few values to tell a stray from the shape of the band.
    values = {3, 4, 9000}
    core, outliers = _core_run(values)
    assert core == values
    assert outliers == set()
