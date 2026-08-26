"""Sequential-ID validation — this corpus's built-in checksum.

Entry IDs run 庶一, 庶二, … across the whole volume, and likewise for 富 and 教.
That means the correct transcription of any band is a complete, strictly
increasing run of integers, and any break in it localizes an OCR or segmentation
error to a specific crop *without a single character of manual ground truth*.

The one rule that must never be broken here: a finding is recorded, never
repaired. Snapping a misread number onto the value the sequence expects would
manufacture agreement and destroy the only independent signal available.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable

from foresight_ocr.validation.numerals import format_numeral, parse_entry_id


@dataclass
class Observation:
    """One transcribed entry in reading order."""

    page_index: int
    band_label: str
    entry_index: int
    text: str


@dataclass
class Finding:
    kind: str  # gap | non_monotonic | duplicate | unparsed
    band_label: str
    page_index: int
    entry_index: int
    observed: str
    expected: str | None
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BandReport:
    band_label: str
    observed: int
    parsed: int
    first_value: int | None
    last_value: int | None
    findings: list[Finding]

    @property
    def parse_rate(self) -> float:
        return self.parsed / self.observed if self.observed else 0.0

    @property
    def clean_run_rate(self) -> float:
        """Share of transitions that were exactly +1 — the headline accuracy proxy."""
        transitions = max(self.parsed - 1, 0)
        breaks = sum(1 for f in self.findings if f.kind in ("gap", "non_monotonic"))
        return (transitions - breaks) / transitions if transitions else 0.0


def check_sequence(observations: Iterable[Observation]) -> dict[str, BandReport]:
    """Validate each band's ID run independently."""
    by_band: dict[str, list[Observation]] = {}
    for obs in observations:
        by_band.setdefault(obs.band_label, []).append(obs)

    reports: dict[str, BandReport] = {}
    for band, items in by_band.items():
        items = sorted(items, key=lambda o: (o.page_index, o.entry_index))
        findings: list[Finding] = []
        values: list[tuple[Observation, int]] = []

        for obs in items:
            parsed = parse_entry_id(obs.text)
            if not parsed.ok or parsed.value is None:
                findings.append(
                    Finding(
                        kind="unparsed",
                        band_label=band,
                        page_index=obs.page_index,
                        entry_index=obs.entry_index,
                        observed=obs.text,
                        expected=None,
                        detail=parsed.reason,
                    )
                )
                continue
            if parsed.band is not None and parsed.band != band:
                findings.append(
                    Finding(
                        kind="band_mismatch",
                        band_label=band,
                        page_index=obs.page_index,
                        entry_index=obs.entry_index,
                        observed=obs.text,
                        expected=band,
                        detail=f"id carries band {parsed.band}",
                    )
                )
            values.append((obs, parsed.value))

        seen: dict[int, Observation] = {}
        for i, (obs, value) in enumerate(values):
            if value in seen:
                prev = seen[value]
                findings.append(
                    Finding(
                        kind="duplicate",
                        band_label=band,
                        page_index=obs.page_index,
                        entry_index=obs.entry_index,
                        observed=obs.text,
                        expected=None,
                        detail=f"also read on page {prev.page_index} "
                        f"entry {prev.entry_index}",
                    )
                )
            seen[value] = obs

            if i == 0:
                continue
            prev_obs, prev_value = values[i - 1]
            step = value - prev_value
            if step == 1:
                continue
            expected = format_numeral(prev_value + 1)
            if step <= 0:
                findings.append(
                    Finding(
                        kind="non_monotonic",
                        band_label=band,
                        page_index=obs.page_index,
                        entry_index=obs.entry_index,
                        observed=obs.text,
                        expected=expected,
                        detail=f"{prev_value} -> {value}",
                    )
                )
            else:
                findings.append(
                    Finding(
                        kind="gap",
                        band_label=band,
                        page_index=obs.page_index,
                        entry_index=obs.entry_index,
                        observed=obs.text,
                        expected=expected,
                        detail=f"{step - 1} value(s) missing between "
                        f"{prev_value} and {value}",
                    )
                )

        reports[band] = BandReport(
            band_label=band,
            observed=len(items),
            parsed=len(values),
            first_value=values[0][1] if values else None,
            last_value=values[-1][1] if values else None,
            findings=findings,
        )
    return reports
