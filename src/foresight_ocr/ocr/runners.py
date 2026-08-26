"""Backend registry.

Each entry declares which interpreter and runner script to use. Paths are
resolved against the project root so the registry works regardless of cwd.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from foresight_ocr.ocr.base import register_backend
from foresight_ocr.ocr.engines import engine_environment
from foresight_ocr.ocr.subprocess_backend import SubprocessBackend
from foresight_ocr.project import Project


def _root():
    return Project.discover().root


def _venv_python(root: Path, name: str, *, platform: str | None = None) -> Path:
    """Return the interpreter path used by a backend's isolated environment."""
    platform = platform or os.name
    if platform == "nt":
        return root / name / "Scripts" / "python.exe"
    return root / name / "bin" / "python"


def _engine_python(root: Path, legacy_name: str, engine_name: str) -> Path:
    """Honor a project's existing environment, then use the managed shared one."""
    legacy = _venv_python(root, legacy_name)
    if legacy.is_file():
        return legacy
    return _venv_python(engine_environment(engine_name), ".")


def _runner(root: Path, filename: str) -> Path:
    """Prefer the runner bundled in an install, with a checkout fallback."""
    bundled = Path(__file__).resolve().parents[1] / "backend_runners" / filename
    if bundled.is_file():
        return bundled
    checkout = Path(__file__).resolve().parents[3] / "runners" / filename
    if checkout.is_file():
        return checkout
    return root / "runners" / filename


@register_backend("ppocr_v5")
def _ppocr_v5(**options: Any) -> SubprocessBackend:
    root = _root()
    return SubprocessBackend(
        name="ppocr_v5",
        python=_engine_python(root, ".venv-paddle", "ppocr_v5"),
        runner=_runner(root, "ppocr_v5.py"),
        model="PP-OCRv5",
        model_version="pending",
        options=options,
        batch_size=64,
    )


@register_backend("paddleocr_vl")
def _paddleocr_vl(**options: Any) -> SubprocessBackend:
    root = _root()
    # `batch_generate` may hold a shape-group in memory, so retain the proven
    # conservative boundary for that opt-in path.  Normal document OCR is
    # sequential inside the runner: a larger runner batch only keeps the same
    # loaded model alive across more crops and does not batch their tensors.
    runner_batch_size = 256 if options.get("batched", False) else 1024
    return SubprocessBackend(
        name="paddleocr_vl",
        python=_engine_python(root, ".venv-vlm", "paddleocr_vl"),
        runner=_runner(root, "paddleocr_vl.py"),
        model=options.pop("model", "PaddlePaddle/PaddleOCR-VL-1.6"),
        model_version="pending",
        options=options,
        timeout_s=3600.0,
        batch_size=runner_batch_size,
    )
