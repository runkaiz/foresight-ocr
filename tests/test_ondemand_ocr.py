"""What re-reading a region does, and — mostly — what it does not do.

Every assertion here is a cost. The product model says a reviewer can correct
where a thing is, what it says, and what order it comes in, independently; that
is only true if changing one of them does not silently pay for the others. So
these tests count model calls and files written rather than inspecting text.

The recognizer is a stub that records what it was asked. A real one would make
these tests slow enough not to be run, which is how an invariant stops being
enforced.
"""

import json
import sqlite3

import cv2
import numpy as np
import pytest

from familyocr.ocr import ondemand
from familyocr.ocr.base import OCRResult, register_backend
from familyocr.persistence.db import connect
from familyocr.project import Project
from familyocr.regions import store as region_store
from familyocr.regions.model import geometry_hash, new_region_uid

DOC = "丙辰庶富教9"


class FakeRecognizer:
    """Counts what it is asked to read, and reads it as its own crop key."""

    name = "fake"
    model_version = "fake-1.0"
    calls: list[list[str]] = []

    def __init__(self, **options):
        self.options = options

    def available(self):
        return True, "fake"

    def recognize(self, requests):
        FakeRecognizer.calls.append([r.crop_id for r in requests])
        return [
            OCRResult(
                crop_id=r.crop_id,
                transcription=f"read:{r.crop_id[:8]}",
                backend=self.name,
                model_version=self.model_version,
                confidence=0.9,
            )
            for r in requests
        ]


register_backend("fake")(FakeRecognizer)


@pytest.fixture(autouse=True)
def _reset_calls():
    FakeRecognizer.calls = []
    yield


@pytest.fixture
def page(tmp_path):
    """A document with one normalized page and two regions on it."""
    project = Project(tmp_path)
    normalized = project.pages_dir(DOC, "normalized")
    normalized.mkdir(parents=True)
    image = np.full((1200, 2300, 3), 240, dtype=np.uint8)
    image[100:900, 1600:1900] = 30            # something for a crop to contain
    cv2.imwrite(str(normalized / "p0058.png"), image)

    conn = connect(project.db_path)
    from familyocr.persistence.db import init_schema

    init_schema(conn)
    conn.execute(
        "INSERT INTO documents VALUES (?,?,?,?,?,?)", (DOC, DOC, "p", "c", 1, "now")
    )
    conn.execute(
        "INSERT INTO pages (document_id, page_index, width, height) VALUES (?,58,?,?)",
        (DOC, 2300, 1200),
    )
    for order, box in enumerate([[1600.0, 0.0, 1900.0, 1000.0],
                                 [1200.0, 0.0, 1600.0, 1000.0]]):
        conn.execute(
            "INSERT INTO regions (region_uid, document_id, page_index, bbox_json, "
            "geometry_hash, band_label, band_ordinal, reading_order, entry_index, "
            "created_at, updated_at) VALUES (?,?,58,?,?,'庶',0,?,?, 'now','now')",
            (f"uid{order}" + new_region_uid()[:28], DOC, json.dumps(box),
             geometry_hash(DOC, 58, box), order, order),
        )
    conn.commit()
    return project, conn


def _uids(conn):
    return [r["region_uid"] for r in conn.execute(
        "SELECT region_uid FROM regions ORDER BY reading_order")]


def _read(project, conn, uids, **kw):
    return ondemand.recognize_regions(
        conn, project, DOC, uids, backend="fake", variant="maxrgb", **kw
    )


def _counts(conn):
    return tuple(
        conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("ocr_candidates", "region_crops")
    )


def test_reading_a_region_cuts_one_crop_and_stores_one_answer(page):
    project, conn = page
    answers = _read(project, conn, _uids(conn)[:1])
    assert len(answers) == 1 and answers[0].reused is False
    assert _counts(conn) == (1, 1)
    assert len(FakeRecognizer.calls) == 1


def test_reading_again_without_changing_anything_calls_no_model(page):
    project, conn = page
    uid = _uids(conn)[0]
    _read(project, conn, [uid])
    before = _counts(conn)

    FakeRecognizer.calls = []
    answers = _read(project, conn, [uid])
    assert answers[0].reused is True
    assert FakeRecognizer.calls == []      # the recognizer was never started
    assert _counts(conn) == before         # and nothing new was written


def test_moving_one_region_re_reads_only_that_region(page):
    project, conn = page
    first, second = _uids(conn)
    _read(project, conn, [first, second])
    assert _counts(conn) == (2, 2)

    region_store.set_geometry(conn, first, [1580.0, 0.0, 1900.0, 1000.0])
    FakeRecognizer.calls = []
    _read(project, conn, [first, second])

    assert len(FakeRecognizer.calls) == 1
    assert len(FakeRecognizer.calls[0]) == 1    # exactly one crop went to the model
    assert _counts(conn) == (3, 3)              # one new answer, one new crop


def test_the_earlier_answer_survives_the_move(page):
    project, conn = page
    uid = _uids(conn)[0]
    before = _read(project, conn, [uid])[0].transcription

    region_store.set_geometry(conn, uid, [1580.0, 0.0, 1900.0, 1000.0])
    after = _read(project, conn, [uid])[0].transcription

    assert after != before
    stored = [
        r["transcription"] for r in conn.execute(
            "SELECT transcription FROM ocr_candidates ORDER BY id")
    ]
    assert stored == [before, after]   # the old reading is still on record


def test_moving_back_reuses_the_crop_and_the_answer(page):
    project, conn = page
    uid = _uids(conn)[0]
    original = region_store.get(conn, uid).bbox
    _read(project, conn, [uid])

    region_store.set_geometry(conn, uid, [1580.0, 0.0, 1900.0, 1000.0])
    _read(project, conn, [uid])
    region_store.set_geometry(conn, uid, original)

    FakeRecognizer.calls = []
    answer = _read(project, conn, [uid])[0]
    assert answer.reused is True
    assert FakeRecognizer.calls == []
    assert _counts(conn) == (2, 2)     # two boxes were ever cut, two ever read


def test_correcting_the_text_does_not_re_read_anything(page):
    """A human transcription is a sibling of the machine's, never a replacement.

    Storing it must not disturb the pixels or the answer already on record —
    otherwise every correction would silently queue a model call, and the
    machine's version of a glyph the human overruled would be lost.
    """
    project, conn = page
    uid = _uids(conn)[0]
    machine = _read(project, conn, [uid])[0].transcription
    before = _counts(conn)

    region = region_store.get(conn, uid)
    conn.execute(
        "INSERT INTO human_corrections (document_id, page_index, band_label, "
        "entry_index, role, transcription, corrected_at) "
        "VALUES (?,58,'庶',?, 'entry', '張廷瓚', 'now')", (DOC, region.entry_index)
    )
    FakeRecognizer.calls = []
    answer = _read(project, conn, [uid])[0]

    assert FakeRecognizer.calls == []
    assert _counts(conn) == before
    assert answer.transcription == machine


def test_changing_reading_order_does_not_re_read_anything(page):
    project, conn = page
    uids = _uids(conn)
    _read(project, conn, uids)
    before = _counts(conn)

    conn.execute("UPDATE regions SET reading_order = 1 - reading_order")
    FakeRecognizer.calls = []
    _read(project, conn, uids)

    assert FakeRecognizer.calls == []
    assert _counts(conn) == before


def test_a_new_model_reads_again_and_keeps_the_old_answer(page):
    """Upgrading a recognizer is an explicit pass, and it is not destructive.

    No backend can report which weights it will load without loading them, so a
    new version is taken up deliberately rather than detected. What matters is
    that doing so stores the new reading beside the old one — the answers a
    reviewer has already seen and judged do not disappear because a model moved.
    """
    project, conn = page
    uid = _uids(conn)[0]
    _read(project, conn, [uid])

    FakeRecognizer.model_version = "fake-2.0"
    try:
        FakeRecognizer.calls = []
        _read(project, conn, [uid], force=True)
    finally:
        FakeRecognizer.model_version = "fake-1.0"

    assert len(FakeRecognizer.calls) == 1
    keys = [r["model_key"] for r in conn.execute(
        "SELECT model_key FROM ocr_candidates ORDER BY id")]
    assert len(keys) == 2 and keys[0] != keys[1]
    # The crop was not re-cut: the same pixels were read by a different model.
    assert conn.execute("SELECT COUNT(*) FROM region_crops").fetchone()[0] == 1


def test_forcing_a_re_read_with_the_same_model_updates_one_row(page):
    """Same pixels, same configuration — one address, so one answer."""
    project, conn = page
    uid = _uids(conn)[0]
    _read(project, conn, [uid])
    _read(project, conn, [uid], force=True)
    assert _counts(conn) == (1, 1)


def test_the_answer_records_which_region_and_which_pixels_it_came_from(page):
    project, conn = page
    uid = _uids(conn)[0]
    _read(project, conn, [uid])

    row = conn.execute(
        "SELECT c.region_id, c.crop_key, c.model_key, c.cache_key, r.region_uid, "
        "       rc.path, rc.pixel_bbox_json "
        "FROM ocr_candidates c JOIN regions r ON r.id = c.region_id "
        "JOIN region_crops rc ON rc.crop_key = c.crop_key"
    ).fetchone()
    assert row["region_uid"] == uid
    assert row["pixel_bbox_json"] == "[1600, 0, 1900, 1000]"
    assert row["model_key"].startswith("fake:fake-1.0:")
    assert row["cache_key"]


def test_a_region_whose_page_is_missing_fails_without_stopping_the_others(page):
    """The editor must survive a recognizer or a page it cannot get to."""
    project, conn = page
    uids = _uids(conn)
    conn.execute(
        "INSERT INTO regions (region_uid, document_id, page_index, bbox_json, "
        "geometry_hash, band_label, band_ordinal, reading_order, entry_index, "
        "created_at, updated_at) VALUES ('missing',?,99,'[0,0,10,10]','h','庶',0,0,0,"
        "'now','now')", (DOC,)
    )
    answers = _read(project, conn, [*uids, "missing"])
    assert [a.error is None for a in answers] == [True, True, False]
    assert "normalize" in answers[-1].error
