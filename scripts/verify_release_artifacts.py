#!/usr/bin/env python3
"""Fail closed when Python release archives contain unsafe or private material."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import stat
import tarfile
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_TOP_LEVEL = {
    ".env",
    ".github",
    "artifacts",
    "benchmarks",
    "build",
    "configs",
    "dist",
    "docs",
    "graphify-out",
    "source",
}
FORBIDDEN_SUFFIXES = {".ckpt", ".db", ".onnx", ".safetensors"}
REQUIRED_WHEEL = {
    "foresight_ocr/__init__.py",
    "foresight_ocr/__main__.py",
    "foresight_ocr/review/app.html",
    "foresight_ocr/backend_runners/ppocr_v5.py",
    "foresight_ocr/backend_runners/paddleocr_vl.py",
}
REQUIRED_SDIST = {
    "LICENSE",
    "PUBLICATION.toml",
    "PYPI.md",
    "README.md",
    "pyproject.toml",
    "runners/ppocr_v5.py",
    "runners/paddleocr_vl.py",
    "scripts/audit_public_release.py",
    "scripts/audit_secrets.py",
    "scripts/build_binary.py",
    "scripts/check_platform_wheels.py",
    "scripts/check_version.py",
    "scripts/smoke_cli_distribution.py",
    "scripts/verify_release_artifacts.py",
    "scripts/verify_standalone_archive.py",
    "src/foresight_ocr/review/app.html",
}


def _safe_parts(name: str) -> tuple[str, ...]:
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise AssertionError(f"unsafe archive path: {name!r}")
    return path.parts


def _forbidden(paths: set[str]) -> list[str]:
    findings: list[str] = []
    for name in sorted(paths):
        parts = _safe_parts(name)
        if (
            parts[0] in FORBIDDEN_TOP_LEVEL
            or PurePosixPath(name).suffix.lower() in FORBIDDEN_SUFFIXES
            or "familyocr" in parts
        ):
            findings.append(name)
    return findings


def _single(paths: list[Path], label: str) -> Path:
    if len(paths) != 1:
        rendered = ", ".join(path.name for path in paths) or "none"
        raise AssertionError(f"expected exactly one {label}; found {rendered}")
    return paths[0]


def _requirements(metadata, extra: str = "") -> set[Requirement]:
    resolved = set()
    for raw in metadata.get_all("Requires-Dist") or []:
        requirement = Requirement(raw)
        if requirement.marker is None or requirement.marker.evaluate({"extra": extra}):
            resolved.add(Requirement(f"{requirement.name}{requirement.specifier}"))
    return resolved


def _verify_metadata(metadata, project: dict, version: str) -> None:
    if metadata["Name"] != project["name"] or metadata["Version"] != version:
        raise AssertionError(
            "distribution metadata name/version does not match project"
        )
    if metadata["Summary"] != project["description"]:
        raise AssertionError("distribution summary does not match pyproject.toml")
    if metadata["License-Expression"] != project["license"]:
        raise AssertionError("distribution license expression does not match project")
    if SpecifierSet(metadata["Requires-Python"]) != SpecifierSet(
        project["requires-python"]
    ):
        raise AssertionError("distribution Python requirement does not match project")

    expected_urls = {
        f"{label}, {url}" for label, url in project.get("urls", {}).items()
    }
    actual_urls = set(metadata.get_all("Project-URL") or [])
    if actual_urls != expected_urls:
        raise AssertionError(
            f"distribution project URLs mismatch: {actual_urls ^ expected_urls}"
        )

    expected_runtime = {Requirement(raw) for raw in project["dependencies"]}
    actual_runtime = _requirements(metadata)
    if actual_runtime != expected_runtime:
        raise AssertionError(
            f"runtime dependency metadata mismatch: {actual_runtime ^ expected_runtime}"
        )

    expected_extras = set(project.get("optional-dependencies", {}))
    actual_extras = set(metadata.get_all("Provides-Extra") or [])
    if actual_extras != expected_extras:
        raise AssertionError(
            f"optional dependency groups mismatch: {actual_extras ^ expected_extras}"
        )
    for extra, raw_requirements in project.get("optional-dependencies", {}).items():
        expected = expected_runtime | {Requirement(raw) for raw in raw_requirements}
        actual = _requirements(metadata, extra)
        if actual != expected:
            raise AssertionError(
                f"dependency metadata mismatch for extra {extra!r}: {actual ^ expected}"
            )


def _verify_record(archive: zipfile.ZipFile, dist_info: str) -> None:
    record_name = f"{dist_info}/RECORD"
    rows = list(csv.reader(io.StringIO(archive.read(record_name).decode("utf-8"))))
    recorded = {name: (digest, size) for name, digest, size in rows}
    files = {item.filename for item in archive.infolist() if not item.is_dir()}
    if set(recorded) != files:
        raise AssertionError(f"wheel RECORD paths mismatch: {set(recorded) ^ files}")
    for name in sorted(files - {record_name}):
        algorithm, separator, encoded = recorded[name][0].partition("=")
        if separator != "=" or algorithm != "sha256" or not encoded:
            raise AssertionError(f"wheel RECORD has no SHA-256 for {name}")
        payload = archive.read(name)
        expected = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(
            b"="
        )
        if encoded.encode() != expected:
            raise AssertionError(f"wheel RECORD hash mismatch for {name}")
        if recorded[name][1] != str(len(payload)):
            raise AssertionError(f"wheel RECORD size mismatch for {name}")
    if recorded[record_name] != ("", ""):
        raise AssertionError("wheel RECORD must not hash itself")


def _verify_wheel(path: Path, project: dict, version: str) -> None:
    with zipfile.ZipFile(path) as archive:
        members = [info.filename.rstrip("/") for info in archive.infolist()]
        if len(members) != len(set(members)):
            raise AssertionError("wheel contains duplicate archive members")
        names = set(members)
        for info in archive.infolist():
            _safe_parts(info.filename)
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise AssertionError(f"wheel contains a symlink: {info.filename}")

        missing = REQUIRED_WHEEL - names
        if missing:
            raise AssertionError(f"wheel is missing required files: {sorted(missing)}")
        forbidden = _forbidden(names)
        if forbidden:
            raise AssertionError(f"wheel contains forbidden files: {forbidden}")

        dist_info = f"foresight_ocr-{version}.dist-info"
        metadata = BytesParser().parsebytes(archive.read(f"{dist_info}/METADATA"))
        _verify_metadata(metadata, project, version)
        if metadata.get_all("Import-Name") != ["foresight_ocr"]:
            raise AssertionError("wheel metadata does not declare its import name")
        entry_points = archive.read(f"{dist_info}/entry_points.txt").decode("utf-8")
        expected_entry = (
            "[console_scripts]\nforesight-ocr = foresight_ocr.cli.main:app\n"
        )
        if entry_points != expected_entry:
            raise AssertionError(f"unexpected console entry points: {entry_points!r}")
        _verify_record(archive, dist_info)


def _verify_sdist(path: Path, project: dict, version: str) -> None:
    root_name = f"foresight_ocr-{version}"
    normalized: set[str] = set()
    metadata = None
    with tarfile.open(path, "r:gz") as archive:
        seen_members: set[str] = set()
        for member in archive.getmembers():
            if member.name in seen_members:
                raise AssertionError(f"sdist contains duplicate member: {member.name}")
            seen_members.add(member.name)
            parts = _safe_parts(member.name)
            if parts[0] != root_name:
                raise AssertionError(
                    f"sdist member escaped archive root: {member.name}"
                )
            if member.issym() or member.islnk():
                raise AssertionError(f"sdist contains a link: {member.name}")
            if not (member.isdir() or member.isfile()):
                raise AssertionError(f"sdist contains a special file: {member.name}")
            if len(parts) > 1:
                relative = PurePosixPath(*parts[1:]).as_posix()
                normalized.add(relative)
                if relative == "PKG-INFO":
                    extracted = archive.extractfile(member)
                    assert extracted is not None
                    metadata = BytesParser().parsebytes(extracted.read())

    missing = REQUIRED_SDIST - normalized
    if missing:
        raise AssertionError(f"sdist is missing required files: {sorted(missing)}")
    forbidden = _forbidden(normalized)
    if forbidden:
        raise AssertionError(f"sdist contains forbidden files: {forbidden}")
    if metadata is None:
        raise AssertionError("sdist is missing PKG-INFO")
    _verify_metadata(metadata, project, version)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dist", nargs="?", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    dist = args.dist.resolve()

    document = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = document["project"]
    version = project["version"]
    wheel = _single(sorted(dist.glob(f"foresight_ocr-{version}-*.whl")), "wheel")
    sdist = _single(
        sorted(dist.glob(f"foresight_ocr-{version}.tar.gz")), "source distribution"
    )

    _verify_wheel(wheel, project, version)
    _verify_sdist(sdist, project, version)
    print(f"release artifact contract: {wheel.name}, {sdist.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
