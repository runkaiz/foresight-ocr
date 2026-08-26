from foresight_ocr.validation.sequence import Observation, check_sequence


def obs(page, entry, text, band="庶"):
    return Observation(page_index=page, band_label=band, entry_index=entry, text=text)


def test_clean_run_has_no_findings():
    reports = check_sequence(
        [
            obs(2, 0, "庶一"),
            obs(2, 1, "庶二"),
            obs(3, 0, "庶三"),
        ]
    )
    r = reports["庶"]
    assert r.findings == []
    assert r.clean_run_rate == 1.0
    assert (r.first_value, r.last_value) == (1, 3)


def test_gap_is_reported_with_the_expected_value():
    reports = check_sequence([obs(2, 0, "庶一"), obs(2, 1, "庶三")])
    (f,) = reports["庶"].findings
    assert f.kind == "gap"
    assert f.expected == "二"
    assert f.page_index == 2 and f.entry_index == 1


def test_backwards_step_is_non_monotonic():
    reports = check_sequence([obs(2, 0, "庶五"), obs(2, 1, "庶四")])
    kinds = {f.kind for f in reports["庶"].findings}
    assert "non_monotonic" in kinds


def test_duplicate_is_reported():
    reports = check_sequence([obs(2, 0, "庶七"), obs(2, 1, "庶七")])
    kinds = {f.kind for f in reports["庶"].findings}
    assert "duplicate" in kinds


def test_unparsed_entries_do_not_break_the_run():
    reports = check_sequence(
        [
            obs(2, 0, "庶一"),
            obs(2, 1, "庶??"),
            obs(2, 2, "庶二"),
        ]
    )
    r = reports["庶"]
    assert r.observed == 3 and r.parsed == 2
    assert [f.kind for f in r.findings] == ["unparsed"]
    # The surviving transition is still clean: the unreadable crop is flagged
    # for review rather than being counted as a sequence error.
    assert r.clean_run_rate == 1.0


def test_bands_are_validated_independently():
    reports = check_sequence(
        [
            obs(2, 0, "庶一"),
            obs(2, 0, "富一", band="富"),
            obs(2, 1, "庶二"),
            obs(2, 1, "富九", band="富"),
        ]
    )
    assert reports["庶"].findings == []
    assert [f.kind for f in reports["富"].findings] == ["gap"]


def test_findings_never_rewrite_the_transcription():
    reports = check_sequence([obs(2, 0, "庶一"), obs(2, 1, "庶三")])
    (f,) = reports["庶"].findings
    assert f.observed == "庶三"
