from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def binary_builder():
    path = Path(__file__).parents[1] / "scripts" / "build_binary.py"
    spec = importlib.util.spec_from_file_location("foresight_build_binary", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_signing_is_fail_closed_starting_at_one_dot_zero(
    binary_builder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FORESIGHT_REQUIRE_SIGNING", raising=False)
    assert not binary_builder._requires_signing("0.9.9")
    assert binary_builder._requires_signing("1.0.0")
    assert binary_builder._requires_signing("2.3.4")

    monkeypatch.setenv("FORESIGHT_REQUIRE_SIGNING", "1")
    assert binary_builder._requires_signing("0.1.0")


def test_binary_version_comparisons_are_numeric(binary_builder) -> None:
    assert binary_builder._version_tuple("14.0") == (14, 0)
    assert binary_builder._version_tuple("2.35") < binary_builder._version_tuple(
        "2.100"
    )


def test_release_stage_preserves_framework_symlinks(
    binary_builder, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    version = source / "_internal" / "Python.framework" / "Versions" / "3.12"
    version.mkdir(parents=True)
    (version / "Python").write_bytes(b"python")
    (version.parent / "Current").symlink_to("3.12")
    (version.parent.parent / "Python").symlink_to("Versions/Current/Python")
    (source / "_internal" / "Python").symlink_to(
        "Python.framework/Versions/Current/Python"
    )

    stage = tmp_path / "stage"
    binary_builder._copy_release_stage(source, stage, preserve_symlinks=True)

    assert (stage / "_internal" / "Python.framework" / "Python").is_symlink()
    assert (stage / "_internal" / "Python").is_symlink()

    portable_stage = tmp_path / "portable-stage"
    binary_builder._copy_release_stage(source, portable_stage)
    assert not (
        portable_stage / "_internal" / "Python.framework" / "Python"
    ).is_symlink()
    assert not (portable_stage / "_internal" / "Python").is_symlink()


def test_codesign_retries_transient_timestamp_failure(
    binary_builder, monkeypatch: pytest.MonkeyPatch
) -> None:
    responses = iter(
        [
            subprocess.CompletedProcess(
                ["codesign"], 1, "", "The timestamp service is not available."
            ),
            subprocess.CompletedProcess(["codesign"], 0, "signed\n", ""),
        ]
    )
    sleeps: list[int] = []
    monkeypatch.setattr(
        binary_builder.subprocess, "run", lambda *args, **kwargs: next(responses)
    )
    monkeypatch.setattr(binary_builder.time, "sleep", sleeps.append)

    binary_builder._run_codesign(["codesign", "artifact"])

    assert sleeps == [1]


def test_command_line_notarization_uses_codesign_requirement(
    binary_builder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(
        binary_builder, "_run", lambda command: commands.append(command)
    )

    executable = tmp_path / "foresight-ocr"
    binary_builder._verify_macos_notarization(executable)

    assert commands == [
        [
            "/usr/bin/codesign",
            "--verify",
            "--strict",
            "--verbose=4",
            "-R=notarized",
            "--check-notarization",
            str(executable),
        ]
    ]


def test_standalone_build_environment_rejects_dev_and_audit_tools(
    binary_builder, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = {
        "optional-dependencies": {
            "dev": ["pytest>=8", "ruff>=0.13"],
            "release": ["pyinstaller>=6"],
            "audit": ["pip-audit>=2"],
        }
    }
    monkeypatch.setattr(
        binary_builder,
        "_installed_distribution_names",
        lambda: {"pyinstaller", "pytest"},
    )

    with pytest.raises(SystemExit, match="release-only environment.*pytest"):
        binary_builder._verify_build_environment(project)

    monkeypatch.setattr(
        binary_builder,
        "_installed_distribution_names",
        lambda: {"pyinstaller"},
    )
    binary_builder._verify_build_environment(project)


def test_third_party_notices_cover_runtime_dependency_closure(
    binary_builder, tmp_path: Path
) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"

    binary_builder._write_third_party_notices(first)
    binary_builder._write_third_party_notices(second)

    notices = first.read_text(encoding="utf-8")
    assert first.read_bytes() == second.read_bytes()
    assert "foresight-ocr 0.1.0" not in notices
    for dependency in (
        "numpy",
        "opencv-python-headless",
        "pikepdf",
        "pillow",
        "scikit-learn",
        "scipy",
        "PyYAML",
        "typer",
        "rich",
    ):
        assert dependency.casefold() in notices.casefold()
    assert "--- " in notices
