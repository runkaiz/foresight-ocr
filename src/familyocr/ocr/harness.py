"""Benchmark harness.

Every backend sees byte-identical crops, and every raw answer is kept. The
harness does not pick a winner; it produces the table that the report reasons
over.

Two scoring paths, deliberately independent:

- **Sequence check** over a contiguous block of pages. Needs no ground truth and
  covers every entry, so it is the metric with real statistical weight. It only
  works on contiguous pages — a sampled page set has genuine gaps in it, and the
  check cannot tell those from OCR errors.
- **Gold set** on a hand-verified subset. Small, but it is the only thing that
  can measure character-level error and rare-character accuracy.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from familyocr.ocr.base import OCRRequest, OCRResult, get_backend
from familyocr.ocr.fields import parse_entry
from familyocr.ocr.metrics import Pair, Score, rare_characters, score_pairs
from familyocr.validation.sequence import Observation, check_sequence


@dataclass
class CropRef:
    crop_id: str
    path: Path
    page_index: int
    band_index: int
    entry_index: int
    context: str
    variant: str


def discover_crops(
    crops_root: Path, variant: str, context: str, document_id: str
) -> list[CropRef]:
    """Find crops for one (variant, context) combination, in reading order."""
    vdir = crops_root / variant
    refs: list[CropRef] = []
    for path in sorted(vdir.glob(f"*_{context}.png")):
        stem = path.stem
        try:
            _, page_s, band_s, entry_s, _ = stem.rsplit("_", 4)
            refs.append(CropRef(
                crop_id=stem,
                path=path,
                page_index=int(page_s.lstrip("p")),
                band_index=int(band_s.lstrip("b")),
                entry_index=int(entry_s.lstrip("e")),
                context=context,
                variant=variant,
            ))
        except (ValueError, IndexError):
            continue
    refs.sort(key=lambda r: (r.page_index, r.band_index, r.entry_index))
    return refs


@dataclass
class RunOutcome:
    backend: str
    variant: str
    context: str
    model_version: str
    results: list[OCRResult] = field(default_factory=list)
    refs: list[CropRef] = field(default_factory=list)
    tag: str = ""      # configuration label, e.g. "batched-0.5x"

    @property
    def by_crop(self) -> dict[str, OCRResult]:
        return {r.crop_id: r for r in self.results}


def run_backend(
    backend_name: str,
    refs: list[CropRef],
    variant: str,
    context: str,
    options: dict[str, Any] | None = None,
    tag: str = "",
) -> RunOutcome:
    backend = get_backend(backend_name, **(options or {}))
    ok, reason = backend.available()
    if not ok:
        raise RuntimeError(f"backend {backend_name!r} unavailable: {reason}")

    requests = [
        OCRRequest(crop_id=r.crop_id, path=r.path, variant=variant, context=context)
        for r in refs
    ]
    results = backend.recognize(requests)
    return RunOutcome(
        backend=backend_name,
        variant=variant,
        context=context,
        model_version=getattr(backend, "model_version", "unknown"),
        results=results,
        refs=refs,
        tag=tag,
    )


BAND_LABELS = {0: "庶", 1: "富", 2: "教"}


def sequence_score(outcome: RunOutcome) -> dict[str, Any]:
    """Run the corpus checksum over one backend's output.

    Only the *own id* field is checked. The parent id points into a different
    generation and is not sequential, so feeding it in would produce noise that
    looks like errors.
    """
    by_crop = outcome.by_crop
    observations: list[Observation] = []
    recovered: list[dict[str, Any]] = []
    for ref in outcome.refs:
        res = by_crop.get(ref.crop_id)
        label = BAND_LABELS.get(ref.band_index)
        # The band is known from rule detection, so a blurred label glyph is not
        # a reason to discard a correctly read numeral. Recoveries are counted
        # below rather than passed off as clean reads.
        parsed = parse_entry(
            res.transcription if res else None, own_label=label, trust_band=True
        )
        if parsed.label_from_geometry:
            recovered.append({
                "page_index": ref.page_index, "band": label,
                "entry_index": ref.entry_index,
                "observed_label": parsed.observed_label,
                "own_id": parsed.own_id,
            })
        own = parsed.own_id
        observations.append(Observation(
            page_index=ref.page_index,
            band_label=BAND_LABELS.get(ref.band_index, f"band{ref.band_index}"),
            entry_index=ref.entry_index,
            text=own or "",
        ))

    reports = check_sequence(observations)
    out: dict[str, Any] = {
        band: {
            "observed": rep.observed,
            "parsed": rep.parsed,
            "parse_rate": rep.parse_rate,
            "clean_run_rate": rep.clean_run_rate,
            "first_value": rep.first_value,
            "last_value": rep.last_value,
            "findings": [f.to_dict() for f in rep.findings],
        }
        for band, rep in sorted(reports.items())
    }
    out["_label_recovered"] = recovered
    return out


def load_gold(paths: Iterable[Path]) -> dict[tuple[int, int, int], str]:
    """Gold transcriptions keyed by (page, band_index, entry_index)."""
    label_to_band = {v: k for k, v in BAND_LABELS.items()}
    gold: dict[tuple[int, int, int], str] = {}
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 4:
                continue
            page, band, entry, text = parts[0], parts[1], parts[2], parts[3]
            band_index = label_to_band.get(band)
            if band_index is None:
                continue
            gold[(int(page), band_index, int(entry))] = text.strip()
    return gold


def gold_score(
    outcome: RunOutcome,
    gold: dict[tuple[int, int, int], str],
    rare: set[str] | None = None,
) -> Score | None:
    by_crop = outcome.by_crop
    pairs: list[Pair] = []
    for ref in outcome.refs:
        key = (ref.page_index, ref.band_index, ref.entry_index)
        if key not in gold:
            continue
        res = by_crop.get(ref.crop_id)
        hyp = res.transcription if res else None
        pairs.append(Pair(
            crop_id=ref.crop_id,
            reference=gold[key],
            # Whitespace only. PaddleOCR-VL separates the printed fields with
            # newlines and PP-OCR concatenates them; that is a formatting
            # difference, not a recognition error, and penalizing it would
            # distort the comparison. Nothing else is normalized — no
            # Traditional-to-Simplified folding, no Unicode canonicalization.
            hypothesis=("".join(hyp.split()) if hyp is not None else None),
            latency_ms=(res.latency_ms if res else None),
            error=(res.error if res else "no result"),
            own_label=BAND_LABELS.get(ref.band_index),
        ))
    if not pairs:
        return None
    return score_pairs(
        pairs, outcome.backend, outcome.variant, outcome.context,
        model_version=outcome.model_version,
        rare=rare if rare is not None else rare_characters(
            [p.reference for p in pairs]
        ),
    )


def agreement(a: RunOutcome, b: RunOutcome) -> dict[str, Any]:
    """Where two backends agree, and on what.

    Agreement is not correctness — both can share a failure mode — but on the
    `own_id` field it is cheap corroboration, and the disagreements are exactly
    the crops a human should look at first.
    """
    ab, bb = a.by_crop, b.by_crop
    labels = {r.crop_id: BAND_LABELS.get(r.band_index) for r in a.refs}
    shared = sorted(set(ab) & set(bb))
    same_text = same_id = both_read = 0
    disagreements: list[dict[str, Any]] = []
    for crop_id in shared:
        ta, tb = ab[crop_id].transcription, bb[crop_id].transcription
        if ta is None or tb is None:
            continue
        both_read += 1
        if ta == tb:
            same_text += 1
        label = labels.get(crop_id)
        ida = parse_entry(ta, own_label=label).own_id
        idb = parse_entry(tb, own_label=label).own_id
        if ida is not None and ida == idb:
            same_id += 1
        else:
            disagreements.append({"crop_id": crop_id, a.backend: ida, b.backend: idb})
    return {
        "compared": both_read,
        "identical_text": same_text,
        "identical_own_id": same_id,
        "own_id_agreement_rate": same_id / both_read if both_read else 0.0,
        "disagreements": disagreements[:60],
    }


def load_outcomes(conn, document_id: str) -> list[RunOutcome]:
    """Rebuild every stored OCR run from the database.

    Scoring must not require re-running a model. The same rule the pipeline
    applies to extraction and layout applies here: changing how results are
    measured is not a reason to recompute them.
    """
    rows = conn.execute(
        """SELECT m.backend AS backend, m.version AS model_version,
                  r.input_variant AS variant, sr.context AS context,
                  r.tag AS tag,
                  sr.crop_id AS crop_id, sr.crop_path AS crop_path,
                  sr.page_index AS page_index,
                  b.band_index AS band_index, pe.entry_index AS entry_index,
                  oc.transcription AS transcription, oc.confidence AS confidence,
                  oc.raw_json AS raw_json, oc.id AS candidate_id
           FROM ocr_candidates oc
           JOIN ocr_runs r ON r.id = oc.ocr_run_id
           JOIN models m ON m.id = r.model_id
           JOIN source_regions sr ON sr.id = oc.source_region_id
           JOIN physical_entries pe ON pe.id = sr.entry_id
           JOIN bands b ON b.id = pe.band_id
           WHERE sr.document_id = ?
           ORDER BY oc.id""",
        (document_id,),
    ).fetchall()

    grouped: dict[tuple[str, str, str], RunOutcome] = {}
    seen: dict[tuple[str, str, str], set[str]] = {}
    for row in rows:
        key = (row["backend"], row["variant"], row["context"], row["tag"])
        outcome = grouped.get(key)
        if outcome is None:
            outcome = RunOutcome(
                backend=row["backend"], variant=row["variant"],
                context=row["context"], model_version=row["model_version"],
                tag=row["tag"] or "",
            )
            grouped[key] = outcome
            seen[key] = set()
        # A crop re-run later supersedes the earlier answer; ordering by
        # candidate id means the last write wins.
        if row["crop_id"] in seen[key]:
            outcome.results = [
                r for r in outcome.results if r.crop_id != row["crop_id"]
            ]
            outcome.refs = [r for r in outcome.refs if r.crop_id != row["crop_id"]]
        seen[key].add(row["crop_id"])

        extra = json.loads(row["raw_json"]) if row["raw_json"] else {}
        outcome.results.append(OCRResult(
            crop_id=row["crop_id"],
            transcription=row["transcription"],
            backend=row["backend"],
            model_version=row["model_version"],
            confidence=row["confidence"],
            input_variant=row["variant"],
            context=row["context"],
            latency_ms=extra.get("latency_ms"),
            error=extra.get("error"),
            raw=extra.get("raw"),
        ))
        outcome.refs.append(CropRef(
            crop_id=row["crop_id"],
            path=Path(row["crop_path"] or ""),
            page_index=row["page_index"],
            band_index=row["band_index"],
            entry_index=row["entry_index"],
            context=row["context"],
            variant=row["variant"],
        ))

    for outcome in grouped.values():
        outcome.refs.sort(key=lambda r: (r.page_index, r.band_index, r.entry_index))
    return list(grouped.values())


def summarize(
    outcomes: list[RunOutcome], gold: dict[tuple[int, int, int], str]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Score every run and compare backends that saw identical crops."""
    rows: list[dict[str, Any]] = []
    for outcome in sorted(
        outcomes, key=lambda o: (o.variant, o.context, o.backend, o.tag)
    ):
        seq = sequence_score(outcome)
        g = gold_score(outcome, gold)
        rows.append({
            "backend": outcome.backend,
            "tag": outcome.tag,
            "model_version": outcome.model_version,
            "variant": outcome.variant,
            "context": outcome.context,
            "crops": len(outcome.refs),
            "read": sum(1 for r in outcome.results if r.ok),
            "sequence": seq,
            "gold": g.to_dict() if g else None,
        })

    agreements: dict[str, Any] = {}
    done: set[tuple[str, str, str, str]] = set()
    for o1 in outcomes:
        for o2 in outcomes:
            if (o1.variant, o1.context, o1.tag) != (o2.variant, o2.context, o2.tag):
                continue
            if o1.backend >= o2.backend:
                continue
            key = (o1.backend, o2.backend, o1.variant, o1.context)
            if key in done:
                continue
            done.add(key)
            agreements["|".join(key)] = agreement(o1, o2)
    return rows, agreements


def sequence_totals(seq: dict[str, Any]) -> tuple[float, float]:
    """(id parse rate, transition-weighted clean-run rate) across all bands."""
    seq = {k: v for k, v in seq.items() if not k.startswith("_")}
    observed = sum(b["observed"] for b in seq.values()) or 1
    parsed = sum(b["parsed"] for b in seq.values())
    weights = sum(max(b["parsed"] - 1, 0) for b in seq.values()) or 1
    clean = sum(
        b["clean_run_rate"] * max(b["parsed"] - 1, 0) for b in seq.values()
    ) / weights
    return parsed / observed, clean


def store_results(conn, document_id: str, run_id: int, outcome: RunOutcome) -> int:
    """Persist every raw answer, keyed to its source region."""
    model_id = f"{outcome.backend}:{outcome.model_version}"
    conn.execute(
        "INSERT INTO models (id, name, version, backend) VALUES (?,?,?,?) "
        "ON CONFLICT(id) DO NOTHING",
        (model_id, outcome.backend, outcome.model_version, outcome.backend),
    )
    cur = conn.execute(
        "INSERT INTO ocr_runs (run_id, model_id, input_variant, tag) "
        "VALUES (?,?,?,?)",
        (run_id, model_id, outcome.variant, outcome.tag),
    )
    ocr_run_id = int(cur.lastrowid)

    stored = 0
    for res in outcome.results:
        row = conn.execute(
            "SELECT id FROM source_regions WHERE document_id = ? AND crop_id = ? "
            "AND context = ? LIMIT 1",
            (document_id, res.crop_id, outcome.context),
        ).fetchone()
        if row is None:
            continue
        cand = conn.execute(
            """INSERT INTO ocr_candidates
               (source_region_id, ocr_run_id, transcription, confidence, raw_json)
               VALUES (?,?,?,?,?)""",
            (row["id"], ocr_run_id, res.transcription, res.confidence,
             json.dumps({"raw": res.raw, "error": res.error,
                         "latency_ms": res.latency_ms}, ensure_ascii=False)),
        )
        for i, ch in enumerate(res.character_results):
            conn.execute(
                """INSERT INTO ocr_characters
                   (candidate_id, char_index, character, confidence, bbox_json)
                   VALUES (?,?,?,?,?)""",
                (int(cand.lastrowid), i, ch.character, ch.confidence,
                 json.dumps(ch.bbox) if ch.bbox else None),
            )
        stored += 1
    return stored
