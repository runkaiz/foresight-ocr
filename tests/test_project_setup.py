from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from foresight_ocr.project import PROJECT_MARKER
from foresight_ocr.project_setup import (
    ProjectSetupError,
    create_project,
    import_pdf_source,
)


def test_create_project_writes_portable_marker_and_database(tmp_path: Path) -> None:
    root = tmp_path / "章氏宗譜"

    project = create_project(root, "章氏宗譜")

    marker = json.loads((root / PROJECT_MARKER).read_text(encoding="utf-8"))
    assert marker["format_version"] == 1
    assert marker["name"] == "章氏宗譜"
    assert marker["created_with"]
    assert project.root == root
    assert project.source.is_dir()
    with sqlite3.connect(project.db_path) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='documents'"
        ).fetchone() == ("documents",)

    assert create_project(root).root == root


def test_create_project_refuses_to_adopt_unrelated_nonempty_directory(
    tmp_path: Path,
) -> None:
    root = tmp_path / "not-a-project"
    root.mkdir()
    (root / "keep.txt").write_text("user data", encoding="utf-8")

    with pytest.raises(ProjectSetupError, match="not empty"):
        create_project(root)

    assert (root / "keep.txt").read_text(encoding="utf-8") == "user data"
    assert not (root / PROJECT_MARKER).exists()


def test_import_pdf_is_atomic_idempotent_and_never_overwrites(
    tmp_path: Path,
) -> None:
    project = create_project(tmp_path / "project")
    first_source = tmp_path / "宗譜.pdf"
    first_source.write_bytes(b"%PDF-1.4\nfirst\n")

    first = import_pdf_source(project, first_source)
    repeated = import_pdf_source(project, first_source)

    assert first.document_id == "宗譜"
    assert first.source.read_bytes() == first_source.read_bytes()
    assert first.copied is True
    assert repeated.copied is False

    other = tmp_path / "other.pdf"
    other.write_bytes(b"%PDF-1.4\ndifferent\n")
    with pytest.raises(ProjectSetupError, match="different PDF bytes"):
        import_pdf_source(project, other, document_id="宗譜")
    assert first.source.read_bytes() == first_source.read_bytes()
