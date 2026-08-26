#!/usr/bin/env python3
"""Exercise an installed or standalone CLI through the structural PDF pipeline."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import tomllib
from itertools import pairwise
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]


def _project_version() -> str:
    document = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(document["project"]["version"])


def _structured_pdf(path: Path) -> None:
    width, height, margin = 2424, 3744, 100
    image = Image.new("RGB", (width, height), (235, 235, 235))
    drawing = ImageDraw.Draw(image)
    x0, x1 = margin, width - margin
    y0, y1 = margin * 4, height - margin * 2
    edges = [y0, y0 + (y1 - y0) // 3, y0 + 2 * (y1 - y0) // 3, y1]
    for y in edges:
        drawing.line((x0, y, x1, y), fill=(20, 20, 20), width=3)
    drawing.line((x0, y0, x0, y1), fill=(20, 20, 20), width=3)
    drawing.line((x1, y0, x1, y1), fill=(20, 20, 20), width=3)
    for top, bottom in pairwise(edges):
        for x in range(x0 + 100, x1 - 50, 180):
            drawing.rectangle((x, top + 80, x + 24, bottom - 80), fill=(30, 30, 30))
    image.save(path, "PDF", resolution=300)


def _run(command: list[str], *, cwd: Path, environment: dict[str, str]) -> None:
    print(f"smoke: {command[-1] if len(command) == 1 else ' '.join(command[1:])}")
    subprocess.run(command, cwd=cwd, env=environment, check=True)


def _assert_pipeline(root: Path) -> None:
    canonical = json.loads(
        (root / "artifacts/pages/ruled/normalized/canonical_space.json").read_text(
            encoding="utf-8"
        )
    )
    if canonical != {"width": 2224, "height": 3144}:
        raise SystemExit(f"unexpected canonical space: {canonical}")

    crops = list((root / "artifacts/crops/ruled/original").glob("*_tight.png"))
    if len(crops) != 36:
        raise SystemExit(f"expected 36 structural crops; found {len(crops)}")

    connection = sqlite3.connect(root / "artifacts/foresight-ocr.db")
    try:
        region_count = connection.execute(
            "SELECT COUNT(*) FROM regions WHERE document_id = 'ruled'"
        ).fetchone()
        band_count = connection.execute(
            "SELECT COUNT(*) FROM bands b "
            "JOIN page_layouts p ON p.id = b.page_layout_id "
            "WHERE p.document_id = 'ruled'"
        ).fetchone()
    finally:
        # sqlite3.Connection's context manager commits or rolls back but does
        # not close the handle. Windows will not remove the temporary project
        # while that database file remains open.
        connection.close()
    if region_count != (36,) or band_count != (3,):
        raise SystemExit(
            f"unexpected structural database counts: regions={region_count}, "
            f"bands={band_count}"
        )


def main() -> int:
    # Child commands already emit UTF-8. Match the parent streams explicitly so
    # captured Rich output can be replayed on Windows consoles whose inherited
    # code page is otherwise CP1252.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "executable",
        nargs="?",
        type=Path,
        help="Standalone or installed foresight-ocr executable; defaults to this Python.",
    )
    parser.add_argument("--expected-version", default=_project_version())
    args = parser.parse_args()

    if args.executable is None:
        command = [sys.executable, "-m", "foresight_ocr"]
    else:
        executable = args.executable.resolve()
        if not executable.is_file():
            raise SystemExit(f"CLI executable not found: {executable}")
        command = [str(executable)]

    environment = os.environ.copy()
    environment.update(
        {
            "NO_COLOR": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
    )
    with tempfile.TemporaryDirectory(prefix="foresight-cli-smoke-") as temp:
        smoke_root = Path(temp)
        version = subprocess.run(
            [*command, "--version"],
            cwd=smoke_root,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
        if version != args.expected_version:
            raise SystemExit(
                f"CLI version smoke failed: expected {args.expected_version!r}, "
                f"found {version!r}"
            )

        _run([*command, "--help"], cwd=smoke_root, environment=environment)
        _run([*command, "doctor"], cwd=smoke_root, environment=environment)
        fixture = smoke_root / "ruled.pdf"
        _structured_pdf(fixture)
        for arguments in (
            ["inspect", str(fixture), "--id", "ruled", "--no-report"],
            ["extract", "ruled"],
            ["normalize", "ruled"],
            ["layout", "ruled"],
            [
                "segment",
                "ruled",
                "--variants",
                "original",
                "--contexts",
                "tight",
            ],
        ):
            _run([*command, *arguments], cwd=smoke_root, environment=environment)
        _assert_pipeline(smoke_root)

        repeated = subprocess.run(
            [
                *command,
                "segment",
                "ruled",
                "--variants",
                "original",
                "--contexts",
                "tight",
            ],
            cwd=smoke_root,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        print(repeated.stdout, end="")
        if "all 36 matched" not in repeated.stdout:
            raise SystemExit("repeat segmentation was not idempotent")

    print("CLI distribution smoke: 36 idempotent structural regions passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
