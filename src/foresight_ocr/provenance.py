"""Run provenance: every generated result records how it was produced.

The get-started spec requires that we can answer "why does this character read as X?"
and reconstruct the full chain. That means each processing run carries pipeline
version, git commit, parameters, input checksum, timestamp and compute backend.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from foresight_ocr import PIPELINE_VERSION


def git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(chunk):
            h.update(block)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def config_hash(params: dict[str, Any]) -> str:
    """Stable hash of a parameter dict, used for stage output caching."""
    canonical = json.dumps(params, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


@dataclass
class ProcessingRun:
    stage: str
    params: dict[str, Any] = field(default_factory=dict)
    input_checksum: str | None = None
    compute_backend: str = "local_cpu"
    pipeline_version: str = PIPELINE_VERSION
    commit: str = field(default_factory=git_commit)
    started_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @property
    def params_hash(self) -> str:
        return config_hash(self.params)

    def as_row(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "params_json": json.dumps(self.params, ensure_ascii=False, default=str),
            "params_hash": self.params_hash,
            "input_checksum": self.input_checksum,
            "compute_backend": self.compute_backend,
            "pipeline_version": self.pipeline_version,
            "git_commit": self.commit,
            "started_at": self.started_at,
        }
