"""One writer per (document, stage)."""

from __future__ import annotations

import pytest

from familyocr.persistence.locks import StageBusy, lock_path, stage_lock


def test_second_run_of_same_stage_is_refused(tmp_path):
    with stage_lock(tmp_path, "丙辰清廉麗1", "segment"):
        with pytest.raises(StageBusy):
            with stage_lock(tmp_path, "丙辰清廉麗1", "segment"):
                pass


def test_other_documents_and_stages_run_freely(tmp_path):
    # The point of locking per stage rather than per database: two volumes are
    # meant to be processed at once.
    with stage_lock(tmp_path, "丙辰清廉麗1", "segment"):
        with stage_lock(tmp_path, "丙辰庶富教1", "segment"):
            pass
        with stage_lock(tmp_path, "丙辰清廉麗1", "layout"):
            pass


def test_lock_is_released_for_the_next_run(tmp_path):
    with stage_lock(tmp_path, "doc", "segment"):
        pass
    with stage_lock(tmp_path, "doc", "segment"):
        pass


def test_cjk_document_ids_get_usable_filenames(tmp_path):
    path = lock_path(tmp_path, "丙辰清廉麗1", "segment")
    assert path.name == "丙辰清廉麗1.segment.lock"
    assert lock_path(tmp_path, "a b/c", "x").name == "a_b_c.x.lock"
