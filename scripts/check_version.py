#!/usr/bin/env python3
"""Verify that every release-facing version agrees with the package metadata."""

from __future__ import annotations

import argparse
import ast
import os
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _package_versions() -> dict[str, str]:
    tree = ast.parse(
        (ROOT / "src" / "foresight_ocr" / "__init__.py").read_text(encoding="utf-8")
    )
    versions: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        if target.id in {"__version__", "PIPELINE_VERSION"}:
            versions[target.id] = ast.literal_eval(node.value)
    return versions


def main() -> int:
    parser = argparse.ArgumentParser()
    default_tag = (
        os.environ.get("GITHUB_REF_NAME")
        if os.environ.get("GITHUB_REF_TYPE") == "tag"
        else None
    )
    parser.add_argument(
        "--tag",
        default=default_tag,
        help="Release tag to compare, normally GITHUB_REF_NAME.",
    )
    args = parser.parse_args()

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = project["project"]["version"]
    package_versions = _package_versions()
    expected = {
        "pyproject.toml": version,
        "foresight_ocr.__version__": package_versions.get("__version__"),
        "foresight_ocr.PIPELINE_VERSION": package_versions.get("PIPELINE_VERSION"),
    }
    mismatches = {name: value for name, value in expected.items() if value != version}
    if mismatches:
        detail = ", ".join(f"{name}={value!r}" for name, value in mismatches.items())
        raise SystemExit(f"version mismatch; expected {version!r}: {detail}")

    if args.tag and args.tag != f"v{version}":
        raise SystemExit(
            f"release tag {args.tag!r} does not match project version v{version}"
        )

    print(f"version contract: {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
