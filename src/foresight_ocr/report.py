"""Generate a layout report strictly from the selected document's artifacts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

import yaml

from foresight_ocr.project import Project


def _load(path: Path) -> Any | None:
    if not path.exists():
        return None
    if path.suffix in (".yaml", ".yml"):
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    return json.loads(path.read_text(encoding="utf-8"))


def _pct(n: int, total: int) -> str:
    return f"{n} / {total} ({n / total:.1%})" if total else "—"


def _metric(value: Any, digits: int) -> str:
    return "—" if value is None else f"{float(value):.{digits}f}"


def write_layout_poc_report(project: Project, document_id: str) -> Path:
    """Write measured layout results without corpus-specific observations."""
    analysis = project.analysis_dir(document_id, "")
    structure = _load(analysis / "inspect" / "structure.json")
    frames = _load(analysis / "frames" / "summary.json")
    watermark = _load(analysis / "watermark" / "scores.json")
    structures = _load(analysis / "layout" / "structures.json")
    template = _load(project.configs / f"template_{document_id}.yaml")

    lines: list[str] = [
        f"# Layout report — `{document_id}`",
        "",
        f"Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')} "
        "by `foresight-ocr report`.",
        "",
        "## Evidence boundary",
        "",
        "This report contains only values read from this document's current "
        "artifacts. Missing stages are marked as missing; no observations from "
        "another document or an earlier run are substituted.",
        "",
        "## Corpus characteristics",
        "",
    ]
    add = lines.append

    if isinstance(structure, dict) and isinstance(structure.get("pages"), list):
        pages = structure["pages"]
        geometry = sorted({f"{page['width']}x{page['height']}" for page in pages})
        encodings = sorted({str(page.get("encoding") or "unknown") for page in pages})
        ppi = sorted(
            {
                round(float(page["x_ppi"]))
                for page in pages
                if page.get("x_ppi") is not None
            }
        )
        add(f"- pages inspected: {len(pages)}")
        add(f"- geometry: {', '.join(geometry) or '—'}")
        add(f"- encoding: {', '.join(encodings) or '—'}")
        add(f"- horizontal resolution: {', '.join(map(str, ppi)) or '—'} ppi")
        add(f"- producer: {structure.get('producer') or '—'}")
        add(f"- source sha256: `{structure.get('checksum') or '—'}`")
    else:
        add("_No inspect artifact found. Run `foresight-ocr inspect`._")

    add("")
    add("## Frame detection and normalization")
    add("")
    if isinstance(frames, dict) and isinstance(frames.get("pages"), list):
        frame_pages = frames["pages"]
        total = len(frame_pages)
        for status in ("clean", "inferred", "failed"):
            count = sum(page.get("status") == status for page in frame_pages)
            add(f"- {status}: {_pct(count, total)}")

        space = frames.get("canonical_space")
        if isinstance(space, dict):
            width = float(space["width"])
            height = float(space["height"])
            width_mad = float(space.get("width_mad", 0))
            height_mad = float(space.get("height_mad", 0))
            add(f"- canonical page: {width:g} x {height:g} px")
            add(
                f"- frame-size MAD: {width_mad:g} x {height_mad:g} px "
                f"({width_mad / width:.2%} x {height_mad / height:.2%})"
            )

        skews = sorted(
            abs(float(page["skew_deg"]))
            for page in frame_pages
            if page.get("forward") and page.get("skew_deg") is not None
        )
        if skews:
            p95_index = min(int(len(skews) * 0.95), len(skews) - 1)
            add(
                f"- absolute skew: median {median(skews):.2f} deg, "
                f"p95 {skews[p95_index]:.2f} deg, max {skews[-1]:.2f} deg"
            )
        if frames.get("worst_roundtrip_px") is not None:
            add(
                "- worst original-to-canonical-to-original round trip: "
                f"{float(frames['worst_roundtrip_px']):.4f} px"
            )
    else:
        add("_No frame summary found. Run `foresight-ocr normalize`._")

    add("")
    add("## Watermark variants")
    add("")
    if isinstance(watermark, dict) and isinstance(watermark.get("variants"), dict):
        add(f"Pages sampled: {len(watermark.get('pages', []))}")
        add("")
        add("| variant | watermark residual | ink contrast under | ink retention |")
        add("|---|---:|---:|---:|")
        for name, values in sorted(watermark["variants"].items()):
            add(
                f"| `{name}` | {_metric(values['watermark_residual'], 1)} | "
                f"{_metric(values['ink_contrast_under'], 1)} | "
                f"{_metric(values['ink_retention'], 3)} |"
            )
        add("")
        add(
            "These image statistics do not establish OCR accuracy. Compare the "
            "variants with `foresight-ocr benchmark` before choosing one."
        )
    else:
        add("_No watermark scores found. Run `foresight-ocr restore`._")

    add("")
    add("## Layout regularity")
    add("")
    if isinstance(template, dict) and isinstance(structures, list):
        edges = [float(value) for value in template.get("band_edges", [])]
        edge_mad = [float(value) for value in template.get("band_edge_mad", [])]
        add(f"- bands per page: {template.get('band_count', '—')}")
        add(f"- band boundaries: {', '.join(f'{value:g}' for value in edges) or '—'}")
        add(f"- boundary MAD: {', '.join(f'{value:.1f}' for value in edge_mad) or '—'}")
        add(f"- entry pitch: {float(template.get('column_pitch', 0)):.1f} px")
        add(f"- entry-pitch MAD: {float(template.get('column_pitch_mad', 0)):.1f} px")
        add(f"- pages contributing to template: {template.get('pages_used', '—')}")
        confidences = [
            float(item["pitch_confidence"])
            for item in structures
            if item.get("pitch_confidence") is not None
        ]
        if confidences:
            add(
                f"- pitch confidence: median {median(confidences):.2f}, "
                f"min {min(confidences):.2f}"
            )
        families = template.get("layout_families") or {}
        if isinstance(families, dict):
            add("")
            add("| layout family | page count | examples |")
            add("|---|---:|---|")
            for name, members in sorted(families.items()):
                examples = ", ".join(str(page) for page in members[:12])
                suffix = " ..." if len(members) > 12 else ""
                add(f"| `{name}` | {len(members)} | {examples}{suffix} |")
    else:
        add(
            "_No complete layout/template artifact pair found. Run `foresight-ocr layout`._"
        )

    add("")
    add("## Review targets")
    add("")
    if isinstance(frames, dict) and isinstance(frames.get("pages"), list):
        for status in ("failed", "inferred"):
            affected = [
                str(page["page_index"])
                for page in frames["pages"]
                if page.get("status") == status
            ]
            add(f"- frame {status}: {', '.join(affected) or 'none'}")
    if isinstance(template, dict):
        outliers = (template.get("layout_families") or {}).get("outlier", [])
        add(f"- layout outliers: {', '.join(map(str, outliers)) or 'none'}")
    if not frames and not template:
        add("_No frame or template review targets are available._")

    crops = sorted(project.crops_dir(document_id).glob("*_tight.png"))
    add("")
    add("## Segmentation artifacts")
    add("")
    if crops:
        add(f"- tight crops present: {len(crops)}")
    else:
        add("_No tight crops found. Run `foresight-ocr segment`._")

    path = project.docs / "layout-poc-report.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
