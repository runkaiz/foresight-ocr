"""Measure machine readings against durable human corrections.

This is deliberately evaluation, not silent auto-correction.  A correction is
paired with the machine reading for the same live region, parsed with the active
document profile, and reported field by field.  The resulting artifact is the
feedback loop for deciding whether a document-specific recognition rule earned
its place: rerun this report and require the corrected set to improve.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from foresight_ocr.context import get_profile
from foresight_ocr.ocr.fields import parse_entry
from foresight_ocr.project import Project
from foresight_ocr.review.data import latest_ocr_tag, page_entries, reviewable_pages


@dataclass(frozen=True)
class CorrectionLearningReport:
    document_id: str
    ocr_tag: str | None
    reviewed_entries: int
    eligible_entries: int
    machine_present: int
    exact_core_entries: int
    field_exact: dict[str, int]
    recoveries: dict[str, Any]
    mismatches: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["rates"] = {
            field: count / self.eligible_entries if self.eligible_entries else 0.0
            for field, count in self.field_exact.items()
        }
        data["exact_core_rate"] = (
            self.exact_core_entries / self.eligible_entries
            if self.eligible_entries
            else 0.0
        )
        return data


def analyze_corrections(
    conn, document_id: str, tag: str | None = None, *, example_limit: int = 80
) -> CorrectionLearningReport:
    """Score the current machine fields against readable, complete corrections.

    A row enters the headline denominator only when the reviewer supplied all
    three core genealogy fields.  Empty, unreadable, header, and legacy
    non-person confirmations remain durable data but cannot serve as an OCR
    reference for this metric.
    """
    selected_tag = tag if tag is not None else latest_ocr_tag(conn, document_id)
    reviewed = eligible = machine_present = exact_core = 0
    exact = {"own_id": 0, "parent": 0, "birth_order": 0}
    own_geometry = parent_structure = 0
    numeral_repairs: dict[str, int] = {}
    mismatches: list[dict[str, Any]] = []

    for page in reviewable_pages(conn, document_id):
        for entry in page_entries(conn, document_id, page, selected_tag):
            if entry.role != "entry" or entry.human is None or entry.unreadable:
                continue
            reviewed += 1
            human = parse_entry(entry.human, entry.band_label, trust_band=True)
            human_fields = (
                human.own_id,
                human.parent_id or human.parent_name,
                human.order,
            )
            if not all(human_fields):
                continue
            eligible += 1
            machine_present += entry.machine is not None
            machine = parse_entry(entry.machine, entry.band_label, trust_band=True)
            machine_fields = (
                machine.own_id,
                machine.parent_id or machine.parent_name,
                machine.order,
            )
            for name, observed, reference in zip(
                exact, machine_fields, human_fields, strict=True
            ):
                exact[name] += observed == reference
            exact_core += machine_fields == human_fields
            own_geometry += machine.label_from_geometry
            parent_structure += machine.parent_label_from_structure
            for wrong, right in machine.numeral_repairs.items():
                key = f"{wrong}->{right}"
                numeral_repairs[key] = numeral_repairs.get(key, 0) + 1
            if machine_fields != human_fields and len(mismatches) < example_limit:
                mismatches.append(
                    {
                        "page": entry.page_index,
                        "band": entry.band_label,
                        "entry": entry.entry_index,
                        "machine_text": entry.machine,
                        "human_text": entry.human,
                        "machine_fields": list(machine_fields),
                        "human_fields": list(human_fields),
                    }
                )

    return CorrectionLearningReport(
        document_id=document_id,
        ocr_tag=selected_tag,
        reviewed_entries=reviewed,
        eligible_entries=eligible,
        machine_present=machine_present,
        exact_core_entries=exact_core,
        field_exact=exact,
        recoveries={
            "own_label_from_geometry": own_geometry,
            "parent_label_from_structure": parent_structure,
            "numeral_repairs": dict(sorted(numeral_repairs.items())),
            "configured_label_confusions": dict(get_profile().label_confusions),
            "configured_numeral_confusions": dict(get_profile().numeral_confusions),
        },
        mismatches=mismatches,
    )


def write_learning_report(
    project: Project, conn, document_id: str, tag: str | None = None
) -> tuple[Path, Path, CorrectionLearningReport]:
    """Write auditable JSON and a compact human-readable scorecard."""
    report = analyze_corrections(conn, document_id, tag)
    data = report.to_dict()
    data["analyzed_at"] = datetime.now(timezone.utc).isoformat()
    out = project.analysis_dir(document_id, "ocr-learning")
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / "corrections.json"
    md_path = out / "REPORT.md"
    json_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    n = report.eligible_entries

    def pct(value: int) -> str:
        return f"{value / n:.1%}" if n else "0.0%"

    lines = [
        f"# {document_id} 人工校對學習報告",
        "",
        f"- OCR 標籤：`{report.ocr_tag or 'none'}`",
        f"- 人物校對：{report.reviewed_entries}",
        f"- 完整三字段樣本：{n}",
        f"- 三字段全對：{report.exact_core_entries}/{n} "
        f"({pct(report.exact_core_entries)})",
        f"- 本人編號：{report.field_exact['own_id']}/{n} "
        f"({pct(report.field_exact['own_id'])})",
        f"- 父輩字段：{report.field_exact['parent']}/{n} "
        f"({pct(report.field_exact['parent'])})",
        f"- 排行：{report.field_exact['birth_order']}/{n} "
        f"({pct(report.field_exact['birth_order'])})",
        "",
        "原始 OCR 與人工校對均未被本報告改寫；修復計數只描述可審計的派生解析。",
    ]
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path, report
