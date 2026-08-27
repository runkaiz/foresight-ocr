#!/usr/bin/env python3
"""Fail closed if a standalone release archive is incomplete or unsafe."""

from __future__ import annotations

import argparse
import re
import stat
import tarfile
import tomllib
import unicodedata
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_SUFFIXES = {".env", ".pdf", ".pem", ".pfx", ".sqlite", ".sqlite3"}


def _archive_root(path: Path) -> str:
    if path.name.endswith(".tar.gz"):
        return path.name.removesuffix(".tar.gz")
    if path.suffix == ".zip":
        return path.stem
    raise AssertionError(f"unsupported standalone archive: {path.name}")


def _validate_name(raw: str, expected_root: str) -> PurePosixPath:
    normalized = raw.replace("\\", "/").rstrip("/")
    name = PurePosixPath(normalized)
    if not normalized or name.is_absolute() or ".." in name.parts:
        raise AssertionError(f"unsafe archive member: {raw!r}")
    if name.parts[0] != expected_root:
        raise AssertionError(f"archive member is outside {expected_root!r}: {raw!r}")
    if unicodedata.normalize("NFC", normalized) != normalized:
        raise AssertionError(f"archive member is not NFC-normalized: {raw!r}")
    if name.suffix.lower() in FORBIDDEN_SUFFIXES:
        raise AssertionError(f"forbidden standalone member: {raw!r}")
    return name


def _validate_macos_internal_symlink(
    name: PurePosixPath, raw_target: str, root: str
) -> None:
    if (
        not raw_target
        or "\\" in raw_target
        or PurePosixPath(raw_target).is_absolute()
        or unicodedata.normalize("NFC", raw_target) != raw_target
    ):
        raise AssertionError(f"unsafe macOS archive link target: {raw_target!r}")

    resolved_parts: list[str] = []
    for part in (name.parent / PurePosixPath(raw_target)).parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not resolved_parts:
                raise AssertionError(
                    f"unsafe macOS archive link target: {raw_target!r}"
                )
            resolved_parts.pop()
        else:
            resolved_parts.append(part)
    resolved = PurePosixPath(*resolved_parts)
    archive_root = PurePosixPath(root)
    if archive_root not in resolved.parents:
        raise AssertionError(f"unsafe macOS archive link target: {raw_target!r}")


def _read_tar(
    path: Path, root: str, *, allow_macos_internal_symlinks: bool = False
) -> tuple[set[PurePosixPath], dict[str, bytes]]:
    names: set[PurePosixPath] = set()
    folded_names: set[str] = set()
    payloads: dict[str, bytes] = {}
    with tarfile.open(path, "r:gz") as archive:
        for member in archive.getmembers():
            name = _validate_name(member.name, root)
            folded = name.as_posix().casefold()
            if folded in folded_names:
                raise AssertionError(f"duplicate/case-colliding member: {member.name}")
            folded_names.add(folded)
            if member.islnk():
                raise AssertionError(
                    f"standalone archive contains a link: {member.name}"
                )
            if member.issym():
                if not allow_macos_internal_symlinks:
                    raise AssertionError(
                        f"standalone archive contains a link: {member.name}"
                    )
                _validate_macos_internal_symlink(name, member.linkname, root)
                continue
            if not (member.isdir() or member.isfile()):
                raise AssertionError(
                    f"standalone archive contains a special file: {member.name}"
                )
            if member.isfile():
                names.add(name)
                if name in {
                    PurePosixPath(root, "README.txt"),
                    PurePosixPath(root, "THIRD_PARTY_NOTICES.txt"),
                }:
                    extracted = archive.extractfile(member)
                    assert extracted is not None
                    payloads[name.name] = extracted.read()
    return names, payloads


def _read_zip(path: Path, root: str) -> tuple[set[PurePosixPath], dict[str, bytes]]:
    names: set[PurePosixPath] = set()
    folded_names: set[str] = set()
    payloads: dict[str, bytes] = {}
    with zipfile.ZipFile(path) as archive:
        for item in archive.infolist():
            name = _validate_name(item.filename, root)
            folded = name.as_posix().casefold()
            if folded in folded_names:
                raise AssertionError(
                    f"duplicate/case-colliding member: {item.filename}"
                )
            folded_names.add(folded)
            mode = item.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise AssertionError(
                    f"standalone archive contains a link: {item.filename}"
                )
            file_type = stat.S_IFMT(mode)
            if file_type and not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise AssertionError(
                    f"standalone archive contains a special file: {item.filename}"
                )
            if not item.is_dir():
                names.add(name)
                if name in {
                    PurePosixPath(root, "README.txt"),
                    PurePosixPath(root, "THIRD_PARTY_NOTICES.txt"),
                }:
                    payloads[name.name] = archive.read(item)
    return names, payloads


def _direct_dependencies() -> list[str]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    names = []
    for requirement in project["project"]["dependencies"]:
        match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", requirement)
        if not match:
            raise AssertionError(f"cannot parse dependency name: {requirement!r}")
        names.append(match.group(0))
    return names


def verify(path: Path) -> None:
    root = _archive_root(path)
    target = root.rsplit("-", 2)[-2:]
    if len(target) != 2:
        raise AssertionError(f"cannot determine target from {path.name}")
    executable = "foresight-ocr.exe" if path.suffix == ".zip" else "foresight-ocr"
    expected_target = "-".join(target)
    if path.suffix == ".zip":
        names, payloads = _read_zip(path, root)
    else:
        names, payloads = _read_tar(
            path,
            root,
            allow_macos_internal_symlinks=expected_target.startswith("macos-"),
        )

    required = {
        PurePosixPath(root, executable),
        PurePosixPath(root, "LICENSE"),
        PurePosixPath(root, "README.txt"),
        PurePosixPath(root, "THIRD_PARTY_NOTICES.txt"),
    }
    missing = sorted(str(name) for name in required - names)
    if missing:
        raise AssertionError(f"standalone archive is missing: {missing}")

    readme = payloads["README.txt"].decode("utf-8")
    if expected_target not in readme:
        raise AssertionError(f"README does not identify target {expected_target}")

    notices = payloads["THIRD_PARTY_NOTICES.txt"].decode("utf-8")
    folded = notices.casefold()
    absent = [name for name in _direct_dependencies() if name.casefold() not in folded]
    if absent:
        raise AssertionError(f"third-party notices omit direct dependencies: {absent}")
    if len(notices) < 1_000:
        raise AssertionError("third-party notices are implausibly short")

    print(f"standalone archive contract: {path.name} ({len(names)} files)")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("archives", nargs="+", type=Path)
    args = parser.parse_args()
    for archive in args.archives:
        verify(archive)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
