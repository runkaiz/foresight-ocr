"""Run a recognizer in its own interpreter.

PaddleOCR and mlx-vlm cannot share a virtualenv — the transformers pin required
by one conflicts with the other, and PaddleOCR's own docs say to keep the
Transformers engine and vLLM in separate environments. Rather than fight the
resolver, each backend gets a venv and is driven as a subprocess over a JSON
manifest.

That isolation buys two more things worth having: a segfault in a native OCR
library kills the child instead of the pipeline, and a backend can be swapped
for a remote worker later without any caller noticing, since the wire format is
already JSON in / JSON out.

Manifest (written by us, read by the runner):

    {"model": str, "options": {...},
     "items": [{"crop_id": str, "path": str, "orientation": str}, ...]}

Results (written by the runner, read by us):

    {"model_version": str,
     "results": [{"crop_id": str, "transcription": str|null,
                  "confidence": float|null,
                  "characters": [{"character": str, "confidence": float|null}],
                  "latency_ms": float|null, "error": str|null,
                  "raw": {...}|null}, ...]}
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from foresight_ocr.ocr.base import CharacterResult, OCRRequest, OCRResult


class SubprocessBackend:
    """Drives a runner script inside another virtualenv."""

    def __init__(
        self,
        name: str,
        python: Path,
        runner: Path,
        model: str,
        model_version: str = "unknown",
        options: dict[str, Any] | None = None,
        timeout_s: float = 1800.0,
        batch_size: int = 64,
    ) -> None:
        self.name = name
        self.python = Path(python)
        self.runner = Path(runner)
        self.model = model
        self.model_version = model_version
        self.options = options or {}
        self.timeout_s = timeout_s
        self.batch_size = batch_size

    def available(self) -> tuple[bool, str]:
        if not self.python.exists():
            return False, f"interpreter not found: {self.python}"
        if not self.runner.exists():
            return False, f"runner not found: {self.runner}"
        try:
            probe = subprocess.run(
                [str(self.python), str(self.runner), "--probe"],
                capture_output=True,
                text=True,
                timeout=600,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"probe failed: {exc}"
        if probe.returncode != 0:
            detail = (probe.stderr or probe.stdout).strip().splitlines()
            return False, detail[-1] if detail else "probe failed"
        return True, (probe.stdout or "").strip()

    def recognize(self, requests: list[OCRRequest]) -> list[OCRResult]:
        results: list[OCRResult] = []
        for batch in self.recognize_batches(requests):
            results.extend(batch)
        return results

    def recognize_batches(self, requests: list[OCRRequest]):
        """Yield completed runner batches so long jobs can report real progress.

        The runner still uses the same deliberately large batches.  Yielding at
        this boundary exposes work that has genuinely finished without loading
        the model once per page merely to make a progress bar move more often.
        """
        for i in range(0, len(requests), self.batch_size):
            yield self._run_batch(requests[i : i + self.batch_size])

    def _run_batch(self, requests: list[OCRRequest]) -> list[OCRResult]:
        by_id = {r.crop_id: r for r in requests}
        workdir = Path(tempfile.mkdtemp(prefix="foresight-ocr-ocr-"))
        try:
            manifest = workdir / "manifest.json"
            out_path = workdir / "results.json"
            manifest.write_text(
                json.dumps(
                    {
                        "model": self.model,
                        "options": self.options,
                        "items": [
                            {
                                "crop_id": r.crop_id,
                                "path": str(r.path),
                                "orientation": r.orientation,
                                **r.options,
                            }
                            for r in requests
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            started = time.perf_counter()
            try:
                proc = subprocess.run(
                    [str(self.python), str(self.runner), str(manifest), str(out_path)],
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_s,
                )
            except subprocess.TimeoutExpired:
                return self._batch_errors(
                    requests, f"runner timed out after {self.timeout_s:g}s"
                )
            except OSError as exc:
                return self._batch_errors(requests, f"runner could not start: {exc}")
            wall_ms = (time.perf_counter() - started) * 1000.0

            if proc.returncode != 0 or not out_path.exists():
                # The whole batch failed. Every crop still gets a row, so a
                # missing result is never mistaken for an empty transcription.
                detail = (proc.stderr or proc.stdout).strip()[-400:]
                return self._batch_errors(
                    requests, f"runner exited {proc.returncode}: {detail}"
                )

            try:
                payload = json.loads(out_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                return self._batch_errors(requests, f"invalid runner results: {exc}")
            if not isinstance(payload, dict) or not isinstance(
                payload.get("results", []), list
            ):
                return self._batch_errors(
                    requests, "invalid runner results: expected an object with a list"
                )
            self.model_version = payload.get("model_version", self.model_version)
            results: list[OCRResult] = []
            seen: set[str] = set()
            for row in payload.get("results", []):
                crop_id = row.get("crop_id")
                req = by_id.get(crop_id)
                if req is None:
                    continue
                seen.add(crop_id)
                results.append(
                    OCRResult(
                        crop_id=crop_id,
                        transcription=row.get("transcription"),
                        backend=self.name,
                        model_version=self.model_version,
                        confidence=row.get("confidence"),
                        character_results=[
                            CharacterResult(
                                character=c.get("character", ""),
                                confidence=c.get("confidence"),
                                bbox=c.get("bbox"),
                            )
                            for c in (row.get("characters") or [])
                        ],
                        input_variant=req.variant,
                        context=req.context,
                        latency_ms=row.get("latency_ms")
                        or wall_ms / max(len(requests), 1),
                        error=row.get("error"),
                        raw=row.get("raw"),
                    )
                )
            for r in requests:
                if r.crop_id not in seen:
                    results.append(
                        OCRResult(
                            crop_id=r.crop_id,
                            transcription=None,
                            backend=self.name,
                            model_version=self.model_version,
                            input_variant=r.variant,
                            context=r.context,
                            error="runner returned no row",
                        )
                    )
            return results
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def _batch_errors(self, requests: list[OCRRequest], detail: str) -> list[OCRResult]:
        """Turn a runner-level failure into one explicit result per requested crop."""
        return [
            OCRResult(
                crop_id=request.crop_id,
                transcription=None,
                backend=self.name,
                model_version=self.model_version,
                input_variant=request.variant,
                context=request.context,
                error=detail,
            )
            for request in requests
        ]
