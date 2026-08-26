from __future__ import annotations

import sys
from pathlib import Path

from foresight_ocr.ocr.base import OCRRequest
from foresight_ocr.ocr.subprocess_backend import SubprocessBackend


def _runner(tmp_path: Path, body: str) -> Path:
    path = tmp_path / f"runner_{len(list(tmp_path.glob('runner_*.py')))}.py"
    path.write_text(body, encoding="utf-8")
    return path


def _backend(runner: Path, **kwargs) -> SubprocessBackend:
    return SubprocessBackend(
        "fake",
        Path(sys.executable),
        runner,
        "fixture-model",
        options={"language": "zh-Hant"},
        **kwargs,
    )


def test_available_checks_paths_and_runner_probe(tmp_path: Path) -> None:
    missing_python = SubprocessBackend(
        "fake", tmp_path / "python", tmp_path / "runner.py", "model"
    )
    assert missing_python.available()[0] is False

    missing_runner = SubprocessBackend(
        "fake", Path(sys.executable), tmp_path / "runner.py", "model"
    )
    assert missing_runner.available()[0] is False

    working = _runner(
        tmp_path,
        """import sys
if sys.argv[1] == "--probe":
    print("fixture ready")
""",
    )
    assert _backend(working).available() == (True, "fixture ready")

    failing = _runner(
        tmp_path,
        """import sys
if sys.argv[1] == "--probe":
    print("dependency missing", file=sys.stderr)
    raise SystemExit(9)
""",
    )
    assert _backend(failing).available() == (False, "dependency missing")


def test_recognize_round_trips_manifest_and_marks_missing_rows(tmp_path: Path) -> None:
    runner = _runner(
        tmp_path,
        """import json
import sys

if sys.argv[1] == "--probe":
    print("ready")
    raise SystemExit
manifest = json.load(open(sys.argv[1], encoding="utf-8"))
first = manifest["items"][0]
json.dump({
    "model_version": "fixture-v2",
    "results": [
        {
            "crop_id": first["crop_id"],
            "transcription": "庶一",
            "confidence": 0.9,
            "characters": [
                {"character": "庶", "confidence": 0.8, "bbox": [1, 2, 3, 4]}
            ],
            "latency_ms": 12.5,
            "raw": {"manifest_item": first, "options": manifest["options"]},
        },
        {"crop_id": "not-requested", "transcription": "ignore"},
    ],
}, open(sys.argv[2], "w", encoding="utf-8"), ensure_ascii=False)
""",
    )
    backend = _backend(runner, batch_size=2)
    requests = [
        OCRRequest(
            "crop-1",
            tmp_path / "one.png",
            variant="restored",
            context="full",
            options={"temperature": 0},
        ),
        OCRRequest("crop-2", tmp_path / "two.png"),
    ]

    results = backend.recognize(requests)
    assert backend.model_version == "fixture-v2"
    assert [result.crop_id for result in results] == ["crop-1", "crop-2"]
    first, missing = results
    assert first.ok and first.transcription == "庶一"
    assert first.character_results[0].bbox == [1, 2, 3, 4]
    assert first.raw is not None
    assert first.raw["options"] == {"language": "zh-Hant"}
    assert first.raw["manifest_item"]["temperature"] == 0
    assert first.input_variant == "restored" and first.context == "full"
    assert missing.error == "runner returned no row"


def test_runner_failures_become_explicit_per_crop_results(tmp_path: Path) -> None:
    request = OCRRequest("crop-1", tmp_path / "one.png")

    exits = _runner(
        tmp_path,
        """import sys
print("backend exploded", file=sys.stderr)
raise SystemExit(7)
""",
    )
    exited = _backend(exits).recognize([request])[0]
    assert not exited.ok
    assert exited.error == "runner exited 7: backend exploded"

    malformed = _runner(
        tmp_path,
        """import sys
open(sys.argv[2], "w", encoding="utf-8").write("not-json")
""",
    )
    invalid = _backend(malformed).recognize([request])[0]
    assert invalid.error is not None and invalid.error.startswith(
        "invalid runner results:"
    )

    sleeps = _runner(
        tmp_path,
        """import time
time.sleep(1)
""",
    )
    timed_out = _backend(sleeps, timeout_s=0.01).recognize([request])[0]
    assert timed_out.error == "runner timed out after 0.01s"
