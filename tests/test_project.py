from pathlib import Path

import pytest

from foresight_ocr.project import InvalidDocumentId, Project, validate_document_id


def test_new_projects_use_the_foresight_ocr_database_name(tmp_path: Path):
    project = Project(tmp_path)

    assert project.db_path == tmp_path / "artifacts" / "foresight-ocr.db"


def test_existing_familyocr_database_remains_readable(tmp_path: Path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    legacy = artifacts / "familyocr.db"
    legacy.touch()

    assert Project(tmp_path).db_path == legacy


def test_new_database_wins_when_both_names_exist(tmp_path: Path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "familyocr.db").touch()
    current = artifacts / "foresight-ocr.db"
    current.touch()

    assert Project(tmp_path).db_path == current


@pytest.mark.parametrize(
    "document_id",
    [
        "../escape",
        ".hidden",
        "/absolute",
        r"..\\escape",
        "bad:name",
        "CON",
        "nul.txt",
        "trailing.",
        " surrounding ",
        "e\N{COMBINING ACUTE ACCENT}",
        "x" * 201,
    ],
)
def test_document_ids_are_safe_portable_filename_components(
    tmp_path: Path, document_id: str
) -> None:
    with pytest.raises(InvalidDocumentId):
        validate_document_id(document_id)
    with pytest.raises(InvalidDocumentId):
        Project(tmp_path).pages_dir(document_id, "decoded")


def test_document_ids_preserve_valid_traditional_chinese_and_spaces() -> None:
    assert validate_document_id("丙辰庶富教1") == "丙辰庶富教1"
    assert validate_document_id("富陽長春章氏宗譜 1") == "富陽長春章氏宗譜 1"


def test_discovery_ignores_an_unrelated_python_project(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "other"\n')
    nested = tmp_path / "work" / "volume"
    nested.mkdir(parents=True)

    assert Project.discover(nested).root == nested.resolve()


def test_discovery_finds_foresight_source_or_data_project(tmp_path: Path) -> None:
    source = tmp_path / "source-project"
    nested_source = source / "src" / "package"
    nested_source.mkdir(parents=True)
    (source / "pyproject.toml").write_text(
        '[project]\nname = "foresight-ocr"\n', encoding="utf-8"
    )
    assert Project.discover(nested_source).root == source.resolve()

    data = tmp_path / "data-project"
    nested_data = data / "working"
    (data / "artifacts").mkdir(parents=True)
    nested_data.mkdir()
    (data / "artifacts" / "foresight-ocr.db").touch()
    assert Project.discover(nested_data).root == data.resolve()
