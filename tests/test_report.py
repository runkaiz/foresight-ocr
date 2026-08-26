from __future__ import annotations

import json
from pathlib import Path

import yaml

from foresight_ocr.project import Project
from foresight_ocr.report import write_layout_poc_report


def _write(path: Path, payload, *, yaml_file: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(payload) if yaml_file else json.dumps(payload)
    path.write_text(text, encoding="utf-8")


def test_layout_report_marks_every_missing_evidence_source(tmp_path: Path) -> None:
    path = write_layout_poc_report(Project(tmp_path), "unknown")
    report = path.read_text(encoding="utf-8")
    assert "only values read from this document's current artifacts" in report
    assert "No inspect artifact found" in report
    assert "No frame summary found" in report
    assert "No watermark scores found" in report
    assert "No complete layout/template artifact pair found" in report
    assert "No tight crops found" in report
    assert "庶字第" not in report
    assert "page 58" not in report.lower()


def test_layout_report_renders_measured_artifacts_only(tmp_path: Path) -> None:
    project = Project(tmp_path)
    analysis = project.artifacts / "analysis" / "demo"
    _write(
        analysis / "inspect" / "structure.json",
        {
            "checksum": "abc123",
            "producer": "fixture",
            "pages": [
                {"width": 100, "height": 200, "encoding": "jpeg", "x_ppi": 300},
                {"width": 100, "height": 200, "encoding": "jpeg", "x_ppi": 300},
            ],
        },
    )
    _write(
        analysis / "frames" / "summary.json",
        {
            "canonical_space": {
                "width": 90,
                "height": 180,
                "width_mad": 1,
                "height_mad": 2,
            },
            "worst_roundtrip_px": 0.125,
            "pages": [
                {"page_index": 1, "status": "clean", "skew_deg": 0.2, "forward": [1]},
                {
                    "page_index": 2,
                    "status": "inferred",
                    "skew_deg": -0.4,
                    "forward": [1],
                },
            ],
        },
    )
    _write(
        analysis / "watermark" / "scores.json",
        {
            "pages": [1, 2],
            "variants": {
                "gray": {
                    "watermark_residual": None,
                    "ink_contrast_under": None,
                    "ink_retention": None,
                },
                "maxrgb": {
                    "watermark_residual": 20,
                    "ink_contrast_under": 100,
                    "ink_retention": 0.8,
                },
            },
        },
    )
    _write(
        analysis / "layout" / "structures.json",
        [{"pitch_confidence": 0.9}, {"pitch_confidence": 0.7}],
    )
    _write(
        project.configs / "template_demo.yaml",
        {
            "band_count": 2,
            "band_edges": [0, 90, 180],
            "band_edge_mad": [0, 1, 0],
            "column_pitch": 12,
            "column_pitch_mad": 0.5,
            "pages_used": 2,
            "layout_families": {"regular": [1], "outlier": [2]},
        },
        yaml_file=True,
    )
    crop = project.crops_dir("demo") / "demo_p0001_tight.png"
    crop.parent.mkdir(parents=True)
    crop.touch()

    report = write_layout_poc_report(project, "demo").read_text(encoding="utf-8")
    assert "pages inspected: 2" in report
    assert "clean: 1 / 2 (50.0%)" in report
    assert "frame inferred: 2" in report
    assert "`gray` | — | — | —" in report
    assert "`maxrgb` | 20.0 | 100.0 | 0.800" in report
    assert "pitch confidence: median 0.80, min 0.70" in report
    assert "layout outliers: 2" in report
    assert "tight crops present: 1" in report
