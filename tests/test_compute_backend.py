from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from foresight_ocr.compute.backend import (
    ComputeBackend,
    ComputeRequest,
    InferenceJob,
    InferenceResult,
    LocalBackend,
)


class _Available:
    def __init__(self, available: bool) -> None:
        self._available = available

    def is_available(self) -> bool:
        return self._available


@pytest.mark.parametrize(
    ("mps", "cuda", "expected"),
    [(True, False, "mps"), (False, True, "cuda"), (False, False, "cpu")],
)
def test_local_backend_detects_available_device(
    monkeypatch: pytest.MonkeyPatch, mps: bool, cuda: bool, expected: str
) -> None:
    torch = SimpleNamespace(
        backends=SimpleNamespace(mps=_Available(mps)), cuda=_Available(cuda)
    )
    monkeypatch.setitem(sys.modules, "torch", torch)
    assert LocalBackend().device == expected


def test_local_backend_contract_and_explicit_override(tmp_path: Path) -> None:
    backend = LocalBackend("cpu")
    local = ComputeRequest("layout", "rules", preferred_device="cpu")
    remote = ComputeRequest("ocr", "model", preferred_device="remote_cuda")
    assert isinstance(backend, ComputeBackend)
    assert backend.can_serve(local)
    assert not backend.can_serve(remote)

    job = InferenceJob(local, [tmp_path / "page.png"], options={"threshold": 3})
    with pytest.raises(NotImplementedError, match="no local model backend"):
        backend.execute(job)

    result = InferenceResult(
        outputs=[{"text": "甲"}],
        model="fixture",
        model_version="v1",
        backend="test",
        timings_ms={"total": 1.5},
        confidence=0.9,
    )
    assert result.outputs[0]["text"] == "甲"
