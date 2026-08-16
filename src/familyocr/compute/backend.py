"""Compute abstraction.

Document logic states *requirements*; it never names a device. That is the whole
point of this module: when a remote GPU worker arrives it implements
`ComputeBackend` and nothing in `document/`, `layout/` or `segmentation/` has to
change. No CUDA-specific code belongs anywhere else in the tree.

Only `LocalBackend` exists today — the corpus is 201 pages and the machine is an
Apple Silicon Mac, so remote execution has nothing to prove yet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@dataclass
class ComputeRequest:
    """What a stage needs, not where it runs."""

    task_type: str                       # e.g. "ocr", "layout_detect"
    model: str
    minimum_vram_gb: float = 0.0
    preferred_device: str | None = None  # cpu | mps | cuda | remote_cuda
    batch_size: int = 1


@dataclass
class InferenceJob:
    request: ComputeRequest
    assets: list[Path]                   # crops or pages to process
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class InferenceResult:
    outputs: list[dict[str, Any]]
    model: str
    model_version: str
    backend: str
    timings_ms: dict[str, float] = field(default_factory=dict)
    confidence: float | None = None


@runtime_checkable
class ComputeBackend(Protocol):
    name: str

    def can_serve(self, request: ComputeRequest) -> bool:
        ...

    def execute(self, job: InferenceJob) -> InferenceResult:
        ...


class LocalBackend:
    """Runs work in-process on this machine."""

    name = "local"

    def __init__(self, device: str | None = None) -> None:
        self.device = device or self._detect_device()

    @staticmethod
    def _detect_device() -> str:
        try:
            import torch  # noqa: PLC0415 — optional dependency

            if torch.backends.mps.is_available():
                return "mps"
            if torch.cuda.is_available():
                return "cuda"
        except ImportError:
            pass
        return "cpu"

    def can_serve(self, request: ComputeRequest) -> bool:
        if request.preferred_device and request.preferred_device.startswith("remote"):
            return False
        return True

    def execute(self, job: InferenceJob) -> InferenceResult:
        raise NotImplementedError(
            "no local model backend is registered yet — OCR backends arrive with "
            "the benchmark (Deliverable 2)"
        )
