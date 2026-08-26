"""docs/ocr-benchmark.md — generated from artifacts/analysis/<doc>/benchmark."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from foresight_ocr.ocr.harness import sequence_totals
from foresight_ocr.project import Project


def _fmt_pct(v: float | None) -> str:
    return "—" if v is None else f"{v:.1%}"


def write_ocr_benchmark_report(project: Project, document_id: str) -> Path:
    src = project.analysis_dir(document_id, "benchmark") / "results.json"
    if not src.exists():
        raise FileNotFoundError(f"no benchmark results at {src}; run benchmark first")
    data = json.loads(src.read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = data["rows"]
    agreements: dict[str, Any] = data.get("agreement", {})

    gold_files = sorted((project.root / "benchmarks" / "gold").glob("*.tsv"))
    gold_count = 0
    for f in gold_files:
        gold_count += sum(
            1
            for line in f.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        )

    L: list[str] = []
    add = L.append

    add(f"# OCR benchmark — `{document_id}`")
    add("")
    add(
        f"Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')} "
        "by `foresight-ocr report-ocr`. Numbers come from the artifacts of the last "
        "`benchmark` or `rescore` run."
    )
    add("")
    add(
        "No benchmark outcome is inferred from another document or from static "
        "prose. Interpretations below are limited to the stored result rows."
    )
    add("")

    add("## What is being measured")
    add("")
    add("Two scoring paths, deliberately independent:")
    add("")
    add(
        "**Sequence checksum** — entry IDs run consecutively through the volume, "
        "so per band a correct transcription must be a gap-free increasing run. "
        "This needs no ground truth and covers every entry scored, which makes it "
        "the metric with real statistical weight. It is only valid on a "
        "*contiguous* page range: on a sampled page set the gaps are real and "
        "the check cannot tell them from OCR errors."
    )
    add("")
    add(
        f"**Gold set** — {gold_count} hand-verified entries "
        f"({', '.join(f.name for f in gold_files)}). Small, but the only way to "
        "measure character error and exact-field accuracy. Entries whose "
        "characters could not be read with confidence were left out rather than "
        "guessed."
    )
    add("")
    add("Exact-field accuracy on `own_id` is reported separately from CER.")
    add("")
    add(
        "Only whitespace is normalized before comparison. Traditional characters "
        "are not folded to Simplified Chinese."
    )
    add("")

    add("## Results")
    add("")
    add(
        "| backend | config | variant | crop | read | id parsed | clean run | "
        "gold exact-id | CER | ms/crop |"
    )
    add("|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        parse_rate, clean = sequence_totals(r["sequence"])
        g = r.get("gold")
        exact = _fmt_pct(g["field_exact"]["own_id"]) if g else "—"
        cer = f"{g['cer']:.3f}" if g else "—"
        latency = (
            f"{g['latency_ms_median']:.0f}" if g and g.get("latency_ms_median") else "—"
        )
        add(
            f"| `{r['backend']}` | {r.get('tag') or 'default'} | `{r['variant']}` "
            f"| `{r['context']}` | {r['read']}/{r['crops']} | {parse_rate:.1%} "
            f"| {clean:.2%} | {exact} | {cer} | {latency} |"
        )
    add("")
    add(
        "`read` counts crops that produced any text. `id parsed` is the share of "
        "entries where an `own_id` could be extracted at all. `clean run` is the "
        "share of consecutive-ID transitions that were exactly +1 — the sequence "
        "checksum."
    )
    add("")

    # ------------------------------------------------------------- per band
    add("### Sequence checksum by band")
    add("")
    add(
        "| backend | config | variant | band | entries | parsed | range | "
        "clean run | findings |"
    )
    add("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        for band, b in r["sequence"].items():
            if band.startswith("_"):
                continue
            add(
                f"| `{r['backend']}` | {r.get('tag') or 'default'} | `{r['variant']}` "
                f"| {band} | {b['observed']} | "
                f"{b['parsed']} ({b['parse_rate']:.1%}) | "
                f"{b['first_value']}–{b['last_value']} | "
                f"{b['clean_run_rate']:.2%} | {len(b['findings'])} |"
            )
    add("")

    # ---------------------------------------------------------- error kinds
    add("### What the errors are")
    add("")
    for r in rows:
        kinds = Counter(
            f["kind"]
            for k, b in r["sequence"].items()
            if not k.startswith("_")
            for f in b["findings"]
        )
        if not kinds:
            continue
        add(
            f"- `{r['backend']}` / {r.get('tag') or 'default'} / `{r['variant']}`: "
            + ", ".join(f"{k} ×{n}" for k, n in kinds.most_common())
        )
    add("")
    add(
        "`gap` and `non_monotonic` are recognition or segmentation failures. "
        "`unparsed` means no ID could be read at all — which is the *safe* "
        "failure: it is visibly missing rather than confidently wrong. "
        "`duplicate` usually means one physical entry was cut twice."
    )
    add("")

    # ------------------------------------------------------------ agreement
    if agreements:
        add("## Backend agreement")
        add("")
        add(
            "| pair | variant | crop | compared | same `own_id` | rate | "
            "identical full text |"
        )
        add("|---|---|---|---|---|---|---|")
        for key, a in sorted(agreements.items()):
            b1, b2, v, c = key.split("|")
            add(
                f"| `{b1}` vs `{b2}` | `{v}` | `{c}` | {a['compared']} | "
                f"{a['identical_own_id']} | {a['own_id_agreement_rate']:.2%} | "
                f"{a['identical_text']} |"
            )
        add("")
        add(
            "Agreement is corroboration, not proof — two models trained on "
            "similar data can share a failure mode. Its real value is the "
            "**disagreement list**: those crops are where a human should look "
            "first, and they are recorded in `results.json`."
        )
        add("")
        worst = max(agreements.values(), key=lambda a: len(a.get("disagreements", [])))
        if worst.get("disagreements"):
            add("Sample disagreements:")
            add("")
            add("```")
            for d in worst["disagreements"][:10]:
                pairs = [f"{k}={v}" for k, v in d.items() if k != "crop_id"]
                add(f"{d['crop_id'].split('_', 1)[-1]:38s} " + "  ".join(pairs))
            add("```")
            add("")

    add("## Reading the result")
    add("")
    best = None
    for r in rows:
        _, clean = sequence_totals(r["sequence"])
        if best is None or clean > best[1]:
            best = (r, clean)
    if best:
        r, clean = best
        add(
            f"Best sequence-checksum score: **`{r['backend']}`** on "
            f"`{r['variant']}` / `{r['context']}` "
            f"({r.get('tag') or 'default'} config) at **{clean:.2%}** clean "
            "transitions."
        )
        add("")
    add("Evidence limits:")
    add("")
    add(
        f"- The report found {gold_count} non-comment gold rows. A small gold set "
        "produces uncertain exact-match and CER estimates."
    )
    add(
        "- A high clean-run rate does not prove correct transcription. The "
        "checksum verifies that IDs form a consecutive run; a systematic offset "
        "affecting every entry equally would pass it. The gold set is what rules "
        "that out."
    )
    add(
        "- Nothing here was auto-corrected. Every raw answer from every backend "
        "is preserved in `ocr_candidates`, and validation findings are recorded "
        "as `needs_review`."
    )
    add("")

    path = project.docs / "ocr-benchmark.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(L), encoding="utf-8")
    return path
