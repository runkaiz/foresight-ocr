from __future__ import annotations

import json
from pathlib import Path

import pytest

from foresight_ocr.ocr.report import write_ocr_benchmark_report
from foresight_ocr.project import Project


def test_ocr_report_requires_benchmark_results(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="run benchmark first"):
        write_ocr_benchmark_report(Project(tmp_path), "missing")


def test_ocr_report_renders_only_stored_outcomes(tmp_path: Path) -> None:
    project = Project(tmp_path)
    source = project.analysis_dir("demo", "benchmark") / "results.json"
    source.parent.mkdir(parents=True)
    sequence = {
        "甲": {
            "observed": 2,
            "parsed": 2,
            "parse_rate": 1.0,
            "first_value": 1,
            "last_value": 3,
            "clean_run_rate": 0.0,
            "findings": [{"kind": "gap"}],
        }
    }
    source.write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "backend": "fixture",
                        "tag": "safe",
                        "variant": "gray",
                        "context": "tight",
                        "read": 2,
                        "crops": 2,
                        "sequence": sequence,
                        "gold": None,
                    }
                ],
                "agreement": {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    gold = tmp_path / "benchmarks" / "gold" / "demo.tsv"
    gold.parent.mkdir(parents=True)
    gold.write_text("# verified\n1\t0\t0\t甲一\n", encoding="utf-8")

    report = write_ocr_benchmark_report(project, "demo").read_text(encoding="utf-8")
    assert "`fixture` | safe | `gray` | `tight` | 2/2" in report
    assert "gap ×1" in report
    assert "found 1 non-comment gold rows" in report
    assert "535 ppi" not in report
    assert "93.9%" not in report
    assert "PaddleOCR-VL" not in report
