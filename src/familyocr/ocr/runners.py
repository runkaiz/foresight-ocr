"""Backend registry.

Each entry declares which interpreter and runner script to use. Paths are
resolved against the project root so the registry works regardless of cwd.
"""

from __future__ import annotations

from typing import Any

from familyocr.ocr.base import register_backend
from familyocr.ocr.subprocess_backend import SubprocessBackend
from familyocr.project import Project


def _root():
    return Project.discover().root


@register_backend("ppocr_v5")
def _ppocr_v5(**options: Any) -> SubprocessBackend:
    root = _root()
    return SubprocessBackend(
        name="ppocr_v5",
        python=root / ".venv-paddle" / "bin" / "python",
        runner=root / "runners" / "ppocr_v5.py",
        model="PP-OCRv5",
        model_version="pending",
        options=options,
        batch_size=64,
    )


@register_backend("paddleocr_vl")
def _paddleocr_vl(**options: Any) -> SubprocessBackend:
    root = _root()
    return SubprocessBackend(
        name="paddleocr_vl",
        python=root / ".venv-vlm" / "bin" / "python",
        runner=root / "runners" / "paddleocr_vl.py",
        model=options.pop("model", "PaddlePaddle/PaddleOCR-VL-1.6"),
        model_version="pending",
        options=options,
        # Model load dominates per-crop cost, so batches are large: the runner
        # loads weights once and reuses them for the whole batch.
        batch_size=256,
    )
