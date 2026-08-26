#!/usr/bin/env python3
"""Verify that every supported target resolves entirely from binary wheels."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]
PYTHON_VERSION = "3.12"
TARGETS = (
    ("windows-x86_64", "x86_64-pc-windows-msvc", None),
    ("linux-x86_64", "x86_64-manylinux_2_35", None),
    ("linux-arm64", "aarch64-manylinux_2_35", None),
    ("macos-x86_64", "x86_64-apple-darwin", "14.0"),
    ("macos-arm64", "aarch64-apple-darwin", "14.0"),
)


def _resolved_names(path: Path) -> set[str]:
    names: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "--")):
            continue
        names.add(canonicalize_name(Requirement(stripped).name))
    return names


def main() -> int:
    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("uv is required to check supported-platform wheels")

    document = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    direct = {
        canonicalize_name(Requirement(raw).name)
        for raw in document["project"]["dependencies"]
    }

    with tempfile.TemporaryDirectory(prefix="foresight-platform-wheels-") as temp:
        for label, platform_target, macos_floor in TARGETS:
            output = Path(temp) / f"{label}.txt"
            environment = os.environ.copy()
            environment.pop("MACOSX_DEPLOYMENT_TARGET", None)
            if macos_floor is not None:
                environment["MACOSX_DEPLOYMENT_TARGET"] = macos_floor
            subprocess.run(
                [
                    uv,
                    "--quiet",
                    "pip",
                    "compile",
                    str(ROOT / "pyproject.toml"),
                    "--python-version",
                    PYTHON_VERSION,
                    "--python-platform",
                    platform_target,
                    "--only-binary",
                    ":all:",
                    "--no-header",
                    "--no-annotate",
                    "--output-file",
                    str(output),
                ],
                check=True,
                cwd=ROOT,
                env=environment,
            )
            missing = direct - _resolved_names(output)
            if missing:
                raise SystemExit(
                    f"{label} resolution omitted direct dependencies: {sorted(missing)}"
                )
            print(f"{label}: binary-wheel resolution passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
