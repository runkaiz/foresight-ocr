import sqlite3
from pathlib import Path

import pytest

from foresight_ocr.ocr.base import OCRResult
from foresight_ocr.ocr.harness import (
    CropRef,
    RunOutcome,
    agreement,
    discover_crops,
    gold_score,
    load_gold,
    load_outcomes,
    sequence_score,
    sequence_totals,
    store_results,
    summarize,
)
from foresight_ocr.persistence.db import init_schema


def make_outcome(backend, texts, variant="original", context="tight"):
    """texts: list of (page, band, entry, transcription|None)."""
    refs, results = [], []
    for page, band, entry, text in texts:
        crop_id = f"doc_p{page:04d}_b{band}_e{entry:02d}_{context}"
        refs.append(
            CropRef(
                crop_id, Path(f"{crop_id}.png"), page, band, entry, context, variant
            )
        )
        results.append(
            OCRResult(
                crop_id=crop_id,
                transcription=text,
                backend=backend,
                model_version="v1",
                input_variant=variant,
                context=context,
                error=None if text else "no text detected",
            )
        )
    return RunOutcome(backend, variant, context, "v1", results, refs)


def test_discover_crops_parses_page_band_entry(tmp_path):
    vdir = tmp_path / "original"
    vdir.mkdir()
    for name in [
        "doc_p0058_b0_e01_tight.png",
        "doc_p0058_b0_e00_tight.png",
        "doc_p0057_b2_e03_tight.png",
        "doc_p0058_b0_e00_medium.png",  # different context, must be ignored
    ]:
        (vdir / name).touch()

    refs = discover_crops(tmp_path, "original", "tight", "doc")
    assert [(r.page_index, r.band_index, r.entry_index) for r in refs] == [
        (57, 2, 3),
        (58, 0, 0),
        (58, 0, 1),
    ]


def test_sequence_score_uses_only_the_own_id_field():
    # The parent id is not sequential; including it would look like errors.
    outcome = make_outcome(
        "b",
        [
            (58, 0, 0, "庶一允二百八十六次子"),
            (58, 0, 1, "庶二允九長子"),
            (58, 0, 2, "庶三允四百長子"),
        ],
    )
    seq = sequence_score(outcome)
    assert seq["庶"]["clean_run_rate"] == 1.0
    assert seq["庶"]["findings"] == []


def test_sequence_score_catches_a_substituted_digit():
    outcome = make_outcome(
        "b",
        [
            (58, 0, 0, "庶三百三十五長子"),
            (58, 0, 1, "庶三百三十六長子"),
            (58, 0, 2, "庶三百三十八長子"),  # should be 三十七
        ],
    )
    seq = sequence_score(outcome)
    kinds = [f["kind"] for f in seq["庶"]["findings"]]
    assert "gap" in kinds
    assert seq["庶"]["clean_run_rate"] < 1.0


def test_unreadable_crop_is_unparsed_not_a_sequence_break():
    outcome = make_outcome(
        "b",
        [
            (58, 0, 0, "庶一長子"),
            (58, 0, 1, None),
            (58, 0, 2, "庶二長子"),
        ],
    )
    seq = sequence_score(outcome)
    assert [f["kind"] for f in seq["庶"]["findings"]] == ["unparsed"]
    assert seq["庶"]["clean_run_rate"] == 1.0


def test_bands_are_scored_separately():
    outcome = make_outcome(
        "b",
        [
            (58, 0, 0, "庶一"),
            (58, 1, 0, "富一"),
            (58, 0, 1, "庶二"),
            (58, 1, 1, "富五"),
        ],
    )
    seq = sequence_score(outcome)
    assert seq["庶"]["findings"] == []
    assert [f["kind"] for f in seq["富"]["findings"]] == ["gap"]


def test_sequence_totals_weights_bands_by_transition_count():
    outcome = make_outcome(
        "b",
        [
            (58, 0, 0, "庶一"),
            (58, 0, 1, "庶二"),
            (58, 0, 2, "庶三"),
            (58, 1, 0, "富一"),
            (58, 1, 1, "富九"),
        ],
    )
    parse_rate, clean = sequence_totals(sequence_score(outcome))
    assert parse_rate == 1.0
    # 庶 contributes 2 clean transitions, 富 contributes 1 broken one.
    assert clean == pytest.approx(2 / 3)


def test_agreement_reports_disagreeing_ids():
    a = make_outcome(
        "a", [(58, 0, 0, "庶三百四十九長子"), (58, 0, 1, "庶三百五十長子")]
    )
    b = make_outcome(
        "b", [(58, 0, 0, "庶三百四十八長子"), (58, 0, 1, "庶三百五十長子")]
    )
    agr = agreement(a, b)
    assert agr["compared"] == 2
    assert agr["identical_own_id"] == 1
    assert agr["own_id_agreement_rate"] == 0.5
    assert agr["disagreements"][0]["a"] == "庶三百四十九"
    assert agr["disagreements"][0]["b"] == "庶三百四十八"


def test_gold_scoring_ignores_whitespace_but_nothing_else(tmp_path):
    gold_file = tmp_path / "g.tsv"
    gold_file.write_text("58\t庶\t0\t庶一允二次子\n", encoding="utf-8")
    gold = load_gold([gold_file])

    # PaddleOCR-VL style: same characters, newlines between printed fields.
    outcome = make_outcome("vl", [(58, 0, 0, "庶一\n允二次子")])
    score = gold_score(outcome, gold)
    assert score.exact_entry == 1.0
    assert score.cer == 0.0


def test_gold_scoring_does_not_forgive_a_character_difference(tmp_path):
    gold_file = tmp_path / "g.tsv"
    gold_file.write_text("58\t庶\t0\t庶三百四十九\n", encoding="utf-8")
    gold = load_gold([gold_file])
    outcome = make_outcome("b", [(58, 0, 0, "庶三百四十八")])
    score = gold_score(outcome, gold)
    assert score.exact_entry == 0.0
    assert score.field_exact["own_id"] == 0.0


def test_load_gold_skips_comments_and_blank_lines(tmp_path):
    f = tmp_path / "g.tsv"
    f.write_text("# a note\n\n58\t庶\t0\t庶一\n", encoding="utf-8")
    assert load_gold([f]) == {(58, 0, 0): "庶一"}


def _seeded_db(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    conn.execute(
        "INSERT INTO documents (id, title, source_path, checksum, page_count, "
        "created_at) VALUES ('doc','t','p','c',1,'now')"
    )
    conn.execute(
        "INSERT INTO processing_runs (document_id, stage, params_json, "
        "params_hash, compute_backend, pipeline_version, git_commit, started_at) "
        "VALUES ('doc','benchmark','{}','h','local','0','x','now')"
    )
    conn.execute(
        "INSERT INTO page_layouts (id, document_id, page_index) VALUES (1,'doc',58)"
    )
    conn.execute(
        "INSERT INTO bands (id, page_layout_id, band_index, bbox_json) "
        "VALUES (1,1,0,'[]')"
    )
    conn.execute(
        "INSERT INTO physical_entries (id, band_id, entry_index, bbox_json) "
        "VALUES (1,1,0,'[]'), (2,1,1,'[]')"
    )
    for sid, eid, crop in (
        (1, 1, "doc_p0058_b0_e00_tight"),
        (2, 2, "doc_p0058_b0_e01_tight"),
    ):
        conn.execute(
            "INSERT INTO source_regions (id, entry_id, document_id, page_index, "
            "role, context, bbox_json, crop_id, crop_path) "
            "VALUES (?,?,'doc',58,'entry','tight','[]',?,?)",
            (sid, eid, crop, f"{crop}.png"),
        )
    conn.commit()
    return conn


def test_stored_results_round_trip_through_the_database(tmp_path):
    conn = _seeded_db(tmp_path)
    outcome = make_outcome(
        "ppocr_v5",
        [
            (58, 0, 0, "庶一長子"),
            (58, 0, 1, "庶二次子"),
        ],
    )
    stored = store_results(conn, "doc", 1, outcome)
    conn.commit()
    assert stored == 2

    loaded = load_outcomes(conn, "doc")
    assert len(loaded) == 1
    got = loaded[0]
    assert got.backend == "ppocr_v5"
    assert got.variant == "original" and got.context == "tight"
    assert [r.transcription for r in got.results] == ["庶一長子", "庶二次子"]
    assert [(r.page_index, r.entry_index) for r in got.refs] == [(58, 0), (58, 1)]

    # Rescoring works entirely from the database — no model is re-run.
    rows, _ = summarize(loaded, {})
    assert rows[0]["sequence"]["庶"]["clean_run_rate"] == 1.0


def test_reloaded_run_keeps_the_latest_answer_for_a_crop(tmp_path):
    conn = _seeded_db(tmp_path)
    store_results(conn, "doc", 1, make_outcome("b", [(58, 0, 0, "庶一長子")]))
    store_results(conn, "doc", 1, make_outcome("b", [(58, 0, 0, "庶九長子")]))
    conn.commit()
    loaded = load_outcomes(conn, "doc")
    texts = [r.transcription for o in loaded for r in o.results]
    assert texts == ["庶九長子"]


def test_error_and_latency_survive_the_round_trip(tmp_path):
    conn = _seeded_db(tmp_path)
    outcome = make_outcome("b", [(58, 0, 0, None)])
    outcome.results[0].latency_ms = 42.5
    store_results(conn, "doc", 1, outcome)
    conn.commit()
    got = load_outcomes(conn, "doc")[0].results[0]
    assert got.transcription is None
    assert got.error == "no text detected"
    assert got.latency_ms == 42.5
