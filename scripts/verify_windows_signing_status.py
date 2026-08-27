#!/usr/bin/env python3
"""Verify the Windows signing disclosure shipped with release artifacts."""

from __future__ import annotations

import argparse
import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def verify(path: Path, expected: str) -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = str(project["project"]["version"])
    expected_name = f"foresight-ocr-{version}-windows-x86_64-signing.json"
    if path.name != expected_name:
        raise AssertionError(
            f"expected signing disclosure {expected_name}, got {path.name}"
        )

    document = json.loads(path.read_text(encoding="utf-8"))
    expected_artifacts = [
        f"foresight-ocr-{version}-windows-x86_64.msi",
        f"foresight-ocr-{version}-windows-x86_64.zip",
    ]
    expected_document = {
        "schema_version": 1,
        "version": version,
        "target": "windows-x86_64",
        "authenticode": expected,
        "artifacts": expected_artifacts,
    }
    if document != expected_document:
        raise AssertionError(
            f"Windows signing disclosure mismatch: {document!r} != {expected_document!r}"
        )
    missing = [
        name for name in expected_artifacts if not (path.parent / name).is_file()
    ]
    if missing:
        raise AssertionError(
            f"Windows signing disclosure names missing artifacts: {missing}"
        )
    print(f"Windows signing disclosure: {expected}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("--expected", required=True, choices=("signed", "unsigned"))
    args = parser.parse_args()
    verify(args.path, args.expected)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
