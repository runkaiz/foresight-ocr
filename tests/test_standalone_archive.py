from __future__ import annotations

import importlib.util
import io
import tarfile
import zipfile
from pathlib import Path

import pytest


@pytest.fixture
def archive_verifier():
    path = Path(__file__).parents[1] / "scripts" / "verify_standalone_archive.py"
    spec = importlib.util.spec_from_file_location("standalone_verifier", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _notices(archive_verifier) -> bytes:
    names = "\n".join(archive_verifier._direct_dependencies())
    return (names + "\n" + "license terms\n" * 100).encode()


def _tar_member(archive: tarfile.TarFile, name: str, payload: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    archive.addfile(info, io.BytesIO(payload))


def test_tar_standalone_contract(archive_verifier, tmp_path: Path) -> None:
    path = tmp_path / "foresight-ocr-1.0.0-linux-x86_64.tar.gz"
    root = path.name.removesuffix(".tar.gz")
    with tarfile.open(path, "w:gz") as archive:
        _tar_member(archive, f"{root}/foresight-ocr", b"binary")
        _tar_member(archive, f"{root}/LICENSE", b"license")
        _tar_member(archive, f"{root}/README.txt", b"linux-x86_64")
        _tar_member(
            archive,
            f"{root}/THIRD_PARTY_NOTICES.txt",
            _notices(archive_verifier),
        )
        _tar_member(archive, f"{root}/_internal/library/README.txt", b"dependency")

    archive_verifier.verify(path)


def test_zip_standalone_contract(archive_verifier, tmp_path: Path) -> None:
    path = tmp_path / "foresight-ocr-1.0.0-windows-x86_64.zip"
    root = path.stem
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{root}/foresight-ocr.exe", b"binary")
        archive.writestr(f"{root}/LICENSE", b"license")
        archive.writestr(f"{root}/README.txt", b"windows-x86_64")
        archive.writestr(f"{root}/THIRD_PARTY_NOTICES.txt", _notices(archive_verifier))
        archive.writestr(f"{root}/_internal/library/README.txt", b"dependency")

    archive_verifier.verify(path)


def test_standalone_contract_rejects_parent_traversal(
    archive_verifier, tmp_path: Path
) -> None:
    path = tmp_path / "foresight-ocr-1.0.0-linux-arm64.tar.gz"
    root = path.name.removesuffix(".tar.gz")
    with tarfile.open(path, "w:gz") as archive:
        _tar_member(archive, f"{root}/../escape", b"bad")

    with pytest.raises(AssertionError, match="unsafe archive member"):
        archive_verifier.verify(path)


def test_standalone_contract_rejects_case_collisions(
    archive_verifier, tmp_path: Path
) -> None:
    path = tmp_path / "foresight-ocr-1.0.0-windows-x86_64.zip"
    root = path.stem
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{root}/README.txt", b"one")
        archive.writestr(f"{root}/readme.TXT", b"two")

    with pytest.raises(AssertionError, match="case-colliding"):
        archive_verifier.verify(path)
