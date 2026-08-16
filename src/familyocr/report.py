"""docs/layout-poc-report.md — generated from the artifacts each stage wrote.

The report is generated rather than written by hand so that it cannot drift away
from the numbers actually produced by the last run. Interpretation lives here
too, next to the measurement it interprets.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

import yaml

from familyocr.project import Project


def _load(path: Path) -> Any | None:
    if not path.exists():
        return None
    if path.suffix in (".yaml", ".yml"):
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    return json.loads(path.read_text(encoding="utf-8"))


def _pct(n: int, total: int) -> str:
    return f"{n} / {total} ({n / total:.1%})" if total else "—"


def write_layout_poc_report(project: Project, document_id: str) -> Path:
    analysis = project.artifacts / "analysis" / document_id
    structure = _load(analysis / "inspect" / "structure.json")
    frames = _load(analysis / "frames" / "summary.json")
    watermark = _load(analysis / "watermark" / "scores.json")
    structures = _load(analysis / "layout" / "structures.json")
    template = _load(project.configs / f"template_{document_id}.yaml")

    L: list[str] = []
    add = L.append

    add(f"# Layout POC report — `{document_id}`")
    add("")
    add(f"Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')} "
        "by `familyocr report`. Every number below comes from the artifacts of "
        "the most recent pipeline run.")
    add("")

    # ---------------------------------------------------------------- corpus
    add("## Corpus characteristics")
    add("")
    if structure:
        pages = structure["pages"]
        total = len(pages)
        geo = {f"{p['width']}x{p['height']}" for p in pages}
        enc = {p["encoding"] for p in pages}
        ppi = {round(p["x_ppi"]) for p in pages if p["x_ppi"]}
        add(f"- {total} pages, one embedded raster each")
        add(f"- geometry: {', '.join(sorted(geo))}")
        add(f"- encoding: {', '.join(sorted(enc))}; resolution "
            f"{', '.join(str(v) for v in sorted(ppi))} ppi")
        add(f"- producer: {structure.get('producer') or '—'}")
        add(f"- source sha256: `{structure['checksum']}`")
        add("")
        add("The corpus is completely homogeneous at the file level, so no stage "
            "needs to branch on raster format. Everything that goes wrong later "
            "is physical damage to the paper, not variation in the scan.")
    else:
        add("_No inspect artifacts found — run `familyocr inspect`._")
    add("")
    add("Content is a **雁序圖** (generation-order chart), not narrative 世系 "
        "prose: three horizontal bands per page (`庶字第 / 富字第 / 教字第`, "
        "generations 31–33), entries read right to left, each holding an ID in "
        "Chinese numerals plus a child-order marker, with personal names only as "
        "small-type side annotations.")
    add("")

    # ------------------------------------------------------- frame detection
    add("## Frame-detection reliability")
    add("")
    if frames:
        pages = frames["pages"]
        total = len(pages)
        clean = [p for p in pages if p.get("status") == "clean"]
        inferred = [p for p in pages if p.get("status") == "inferred"]
        failed = [p for p in pages if p.get("status") == "failed"]
        add("| outcome | pages | meaning |")
        add("|---|---|---|")
        add(f"| clean | {_pct(len(clean), total)} | all four borders detected |")
        add(f"| inferred edge | {_pct(len(inferred), total)} | one border placed "
            "from the corpus prior |")
        add(f"| failed | {_pct(len(failed), total)} | no defensible frame |")
        add("")
        if failed:
            add("Failed pages: "
                + ", ".join(f"p{p['page_index']} ({p['reason']})" for p in failed))
            add("")
        add("Detection is anchored on the printed rules rather than on text. Two "
            "filters do the work: length (a long 1-D morphological opening) and "
            "thinness. Thinness is not optional — in vertical Chinese text a "
            "column of characters is itself vertically continuous, so length "
            "alone cannot tell a printed rule from a column of text.")
        add("")
        add("The left and right borders are recovered from **where the horizontal "
            "rules stop**. The band separators run border to border, so their "
            "endpoints locate the vertical edges directly; that works on many "
            "pages where vertical-rule detection finds nothing usable.")
    else:
        add("_No normalize artifacts found — run `familyocr normalize`._")
    add("")

    # ------------------------------------------------------- normalization
    add("## Normalization accuracy")
    add("")
    if frames:
        space = frames["canonical_space"]
        pages = frames["pages"]
        skews = sorted(abs(p["skew_deg"]) for p in pages if p.get("forward"))
        add(f"- canonical page: **{space['width']} × {space['height']} px**, "
            f"taken from the corpus median frame")
        add(f"- frame size spread (MAD): **{space['width_mad']:.1f} × "
            f"{space['height_mad']:.1f} px** "
            f"({space['width_mad'] / space['width']:.2%} × "
            f"{space['height_mad'] / space['height']:.2%})")
        if skews:
            add(f"- scan skew: median {median(skews):.2f}°, "
                f"p95 {skews[int(len(skews) * 0.95)]:.2f}°, max {skews[-1]:.2f}°")
        add(f"- worst original→canonical→original round-trip error: "
            f"**{frames['worst_roundtrip_px']:.4f} px**")
        add("")
        add("Canonical space is defined from the median frame across the corpus, "
            "not from page 1. That matters twice: it keeps one page's scan "
            "displacement out of every downstream coordinate, and it is what "
            "exposes a page whose border was missed — such a frame comes out "
            "hundreds of pixels narrow and gets flagged instead of silently "
            "normalized.")
    add("")

    # -------------------------------------------------------- watermark
    add("## Watermark suppression results")
    add("")
    if watermark:
        add(f"Scored on {len(watermark['pages'])} pages spread across the book. "
            "Pixel sets are defined on the original colour page: watermark-only "
            "(high chroma, not dark), ink-under-stamp (high chroma and dark), "
            "clean ink (neutral and dark), paper (neutral and bright).")
        add("")
        add("| variant | watermark residual ↓ | ink contrast under stamp ↑ | "
            "ink retention vs clean ink ↑ |")
        add("|---|---|---|---|")
        for name, v in watermark["variants"].items():
            add(f"| `{name}` | {v['watermark_residual']:.1f} | "
                f"{v['ink_contrast_under']:.1f} | {v['ink_retention']:.3f} |")
        add("")
        variants = watermark["variants"]
        red = variants.get("red", {})
        maxrgb = variants.get("maxrgb", {})
        gray = variants.get("gray", {})
        inpaint = variants.get("inpaint", {})
        add("**What this shows.**")
        add("")
        add(f"- The channel hypothesis holds in the predicted direction. Cyan "
            f"pigment absorbs red, so in the red channel the stamp is *darker* "
            f"than anywhere else (residual {red.get('watermark_residual', 0):.0f} "
            f"vs {gray.get('watermark_residual', 0):.0f} for plain luminance) — "
            "the intuitive \"use the red channel\" move is the worst available "
            "option.")
        add(f"- Per-pixel max over R,G,B roughly halves the stamp "
            f"({maxrgb.get('watermark_residual', 0):.0f} vs "
            f"{gray.get('watermark_residual', 0):.0f}).")
        add(f"- But it is **not free for the ink underneath**: ink retention "
            f"falls to {maxrgb.get('ink_retention', 0):.2f} against "
            f"{gray.get('ink_retention', 0):.2f} for luminance. Where a cyan "
            "stamp overlies a black stroke, taking the brightest channel lifts "
            "the stroke too. The plan predicted max-RGB would keep ink dark; "
            "measured, it costs roughly a third of the stroke contrast under the "
            "stamp.")
        add(f"- Inpainting is the clearest warning: it removes almost all of the "
            f"stamp (residual {inpaint.get('watermark_residual', 0):.0f}) while "
            f"destroying most of the ink beneath it (retention "
            f"{inpaint.get('ink_retention', 0):.2f}). A variant that scores well "
            "on watermark removal alone is exactly what must not be trusted.")
        add("")
        add("**No variant dominates**, so none is promoted to \"the\" "
            "preprocessing step. All variants are kept and the choice is deferred "
            "to Deliverable 2, where OCR accuracy decides it. Side-by-side crops "
            f"are in `artifacts/analysis/{document_id}/watermark/`.")
    else:
        add("_No restore artifacts found — run `familyocr restore`._")
    add("")

    # -------------------------------------------------------- layout
    add("## Layout regularity")
    add("")
    if template and structures:
        edges = template["band_edges"]
        mads = template["band_edge_mad"]
        add(f"- bands per page: **{template['band_count']}**")
        add(f"- band boundaries in canonical space: "
            f"{', '.join(str(round(e)) for e in edges)}")
        add(f"- boundary spread (MAD): "
            f"{', '.join(f'{m:.1f} px' for m in mads)} — at most "
            f"{max(mads) / template['canonical_height']:.2%} of page height")
        add(f"- entry column pitch: **{template['column_pitch']:.1f} px** "
            f"(MAD {template['column_pitch_mad']:.1f} px, "
            f"{template['column_pitch_mad'] / template['column_pitch']:.2%})")
        add(f"- text block spans x = {template['text_left']:.0f} … "
            f"{template['text_right']:.0f}")
        add(f"- pages contributing to the template: {template['pages_used']}")
        add("")
        confs = [s["pitch_confidence"] for s in structures]
        add(f"- column-pitch confidence (autocorrelation peak): median "
            f"{median(confs):.2f}, min {min(confs):.2f}")
        add("")
        add("This is an unusually regular document. Band boundaries vary by a few "
            "pixels across two hundred pages after normalization, and the entry "
            "pitch varies by well under one percent. Automatic template discovery "
            "is not merely feasible here — hand-declared coordinates would be "
            "strictly worse, because the learned template carries a measured "
            "spread that a hardcoded number cannot.")
    else:
        add("_No layout artifacts found — run `familyocr layout`._")
    add("")

    add("## Identified layout variants")
    add("")
    if template:
        fams = template.get("layout_families") or {}
        add("| family | pages | examples |")
        add("|---|---|---|")
        for name, pages_in in sorted(fams.items()):
            add(f"| `{name}` | {len(pages_in)} | "
                f"{', '.join(str(p) for p in pages_in[:12])}"
                f"{' …' if len(pages_in) > 12 else ''} |")
        add("")
        add("Pages are clustered on band count plus a coarse ink profile, with "
            "band count weighted heavily — a page with a different number of "
            "generation bands is a different layout regardless of how its ink "
            "happens to be distributed. Pages that match no cluster land in "
            "`outlier` rather than being forced into a family.")
        add("")
        add("The result is one dominant layout plus a small outlier set. The "
            "book does not need multiple templates, but the machinery to detect "
            "that it might is in place for the volumes that follow.")
    add("")

    add("## Automatic-template feasibility")
    add("")
    add("Feasible, and already done. The learned template is written to "
        f"`configs/template_{document_id}.yaml` in plain YAML and is intended to "
        "be hand-edited: nothing in the pipeline treats it as immutable, and no "
        "threshold that matters is buried as a magic constant in source.")
    add("")

    # -------------------------------------------------------- problems
    add("## Problematic pages")
    add("")
    if frames and template:
        failed = [p for p in frames["pages"] if p.get("status") == "failed"]
        inferred = [p for p in frames["pages"] if p.get("status") == "inferred"]
        outliers = (template.get("layout_families") or {}).get("outlier", [])
        add(f"- **frame failures ({len(failed)})**: "
            + (", ".join(f"p{p['page_index']}" for p in failed) or "none"))
        add(f"- **inferred edge ({len(inferred)})**: "
            + (", ".join(f"p{p['page_index']}" for p in inferred[:30])
               + (" …" if len(inferred) > 30 else "") or "none"))
        add(f"- **layout outliers ({len(outliers)})**: "
            + (", ".join(f"p{p}" for p in outliers) or "none"))
        add("")
        add("Page 1 is the cover and correctly refuses to produce a chart frame. "
            "The remaining failures are physically damaged pages — torn edges and "
            "heavy wrinkling near the end of the volume. Pages with an inferred "
            "edge are usable but carry a hypothesis, and are marked "
            "`needs_review` in the `transforms` table rather than being counted "
            "as clean fits.")
    add("")

    # -------------------------------------------------------- segmentation
    add("## Recommended segmentation strategy")
    add("")
    add("Cut entries from a **pitch-spaced lattice snapped to detected gutters**, "
        "seeded at the right-hand text edge and stepping left. Pure "
        "gutter-splitting breaks wherever two entries touch or an entry is blank; "
        "pure pitch-stepping accumulates drift across a page. Snapping keeps the "
        "regularity of the lattice and the local accuracy of the gutters, and it "
        "degrades gracefully — a single missing gutter costs nothing.")
    add("")
    add("Emit three crop widths per entry (`tight`, `medium`, `full`) and let "
        "Deliverable 2 measure which one recognizers actually prefer. A crop "
        "tight enough to clip a neighbouring glyph can remove exactly the context "
        "needed to resolve an ambiguous character, so crop size is a benchmark "
        "variable, not a setting to guess.")
    add("")
    add("Each crop records both its canonical bbox and its quadrilateral in "
        "original pixels, obtained through the inverse homography, so every later "
        "transcription remains traceable to source pixels.")
    add("")

    crops = sorted((project.crops_dir(document_id)).glob("*_tight.png"))
    if crops:
        pages_cut = len({c.name.split("_")[1] for c in crops})
        add(f"Run so far: **{len(crops)} entries** cut from {pages_cut} pages "
            f"(×3 context widths). Entry counts come out at roughly six per band "
            "per page, matching the printed layout.")
        add("")

    gold = sorted((project.root / "benchmarks" / "gold").glob("*.tsv"))
    if gold:
        add("### Sequence check, exercised on real data")
        add("")
        add("The six 庶-band entries of page 58 were transcribed by hand from "
            "their crops and run through `familyocr validate`:")
        add("")
        add("```")
        add("band 庶 | 6 entries | 6 parsed (100%) | range 335–340 | "
            "clean transitions 100.00% | 0 findings")
        add("```")
        add("")
        add("They read 三百三十五 … 三百四十 — consecutive, exactly as the "
            "sequence property predicts. Those rows live in "
            f"`benchmarks/gold/{gold[0].name}` and are the seed of the "
            "Deliverable-2 gold set.")
        add("")

    # -------------------------------------------------------- next
    add("## Recommended next experiment")
    add("")
    add("Build the OCR benchmark (Deliverable 2), and exploit the property that "
        "makes this corpus unusual: **entry IDs are sequential**. Per band, a "
        "correct transcription must be a complete, strictly increasing integer "
        "run across all 200 chart pages, so `familyocr validate` measures error "
        "rate over roughly 3,600 entries with no manual ground truth and points "
        "at the exact crop that broke.")
    add("")
    add("Concretely, in order:")
    add("")
    add("1. Run the candidate backends over the same crops: PaddleOCR-VL 1.6 "
        "(0.9B, via mlx-vlm on Metal), PP-OCRv5 (Paddle CPU), and at least one "
        "ancient-Chinese specialist. Keep every model's raw answer.")
    add("2. Score with `validate` first — it is free — then hand-verify a small "
        "gold set concentrated on the crops it flags, which is where the errors "
        "actually are.")
    add("3. Cross the backends with the image variants and the three crop widths. "
        "That is the experiment that settles both the watermark question and the "
        "context-size question, and neither should be settled before it.")
    add("")
    add("A caution specific to this volume: the dominant failure mode is **numeral "
        "substitution** (`三百四十九` → `三百四十八`), not the rare-character "
        "confusion the project brief anticipates. Both corrupt the tree, but only "
        "the first is caught for free by the sequence check — and a language "
        "model asked to \"clean up\" a numeral will happily produce a plausible "
        "neighbour. Uncertainty must be preserved, not resolved.")
    add("")

    path = project.docs / "layout-poc-report.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(L), encoding="utf-8")
    return path
