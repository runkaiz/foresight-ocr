# familyocr

Digitization pipeline for scanned Chinese genealogy records (族譜 / 宗譜).

First corpus: `source/丙辰庶富教1.pdf` — 卷十 雁序圖 of 富陽長壽章氏宗譜, 201 pages,
one JPEG2000 raster per page at 2424×3744 @ 535 ppi.

The goal is maximum reliable transcription and structural reconstruction, with
provenance back to source pixels. Accuracy and auditability come before compute
efficiency. Every intermediate representation stays inspectable — no
page-image-to-JSON black box.

## Setup

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
```

Python 3.12 is required: `paddlepaddle` and `paddleocr` have no wheels for 3.13+.

## Pipeline

Stages are independently rerunnable and communicate only through artifacts on
disk and rows in SQLite, so changing the OCR model never forces a re-extract or
a re-layout.

```bash
familyocr inspect source/丙辰庶富教1.pdf   # PDF structure  -> docs/corpus-analysis.md
familyocr extract 丙辰庶富教1              # preserve originals + decode PNGs
familyocr normalize 丙辰庶富教1            # frame detection -> canonical space
familyocr restore 丙辰庶富教1              # watermark suppression benchmark
familyocr layout 丙辰庶富教1               # bands, columns, learned template
familyocr segment 丙辰庶富教1              # entry crops at 3 context widths
familyocr validate 丙辰庶富教1             # sequential-ID checksum
familyocr db                               # what has been produced so far
```

OCR benchmark (Deliverable 2):

```bash
familyocr segment 丙辰庶富教1 --pages 50-70 --variants original,maxrgb
familyocr benchmark 丙辰庶富教1 --backends ppocr_v5,paddleocr_vl \
                                 --variants original,maxrgb --contexts tight
familyocr rescore 丙辰庶富教1              # re-measure without re-running models
familyocr review-queue 丙辰庶富教1         # crops worth hand-verifying
familyocr report-ocr 丙辰庶富教1           # -> docs/ocr-benchmark.md
```

Use a **contiguous** page range for the benchmark. The sequence checksum treats
a break in the ID run as an error, and on a sampled page set the breaks are real.

## OCR backends

Each backend runs in its own virtualenv, driven as a subprocess over a JSON
manifest. PaddleOCR and mlx-vlm cannot share an environment — their transformers
pins conflict, and PaddleOCR's own documentation says to keep the Transformers
engine and vLLM apart. Isolation also means a segfault in a native OCR library
kills the child rather than the pipeline.

```bash
uv venv --python 3.12 .venv-paddle && VIRTUAL_ENV=.venv-paddle uv pip install paddlepaddle paddleocr
uv venv --python 3.12 .venv-vlm    && VIRTUAL_ENV=.venv-vlm    uv pip install mlx-vlm
.venv-paddle/bin/python runners/ppocr_v5.py --probe
.venv-vlm/bin/python runners/paddleocr_vl.py --probe
```

| backend | model | device |
|---|---|---|
| `ppocr_v5` | PP-OCRv5 (`chinese_cht`) | CPU |
| `paddleocr_vl` | PaddleOCR-VL 1.6 (0.9B) | Metal via MLX |

Crops are fed **unrotated**. In vertical Chinese the glyphs are upright and only
the line direction is vertical; rotating the crop lays every character on its
side and accuracy collapses.

## What this corpus makes possible

Entry IDs (`庶五百八十七`, `教千百九十三`) run sequentially across the whole
volume. Per band, a correct transcription must be a complete, strictly
increasing integer run — so `familyocr validate` localizes OCR and segmentation
errors to individual crops **without any manual ground truth**. Findings are
recorded, never repaired: rewriting a number to satisfy the sequence would
manufacture the agreement the check exists to measure.

## Entry structure

An entry holds three printed fields:

```
庶三百三十五   允二百八十六   次子
own id         father's id     birth order
```

The father's id names a person in the *previous* generation band, so parent
links are printed on the page rather than inferred from geometry. The generation
chain is `允 → 庶 → 富 → 教`, which means **the same character is an own label in
one band and a parent label in the next**: 庶 identifies the entry in band 0 and
its father in band 1. `parse_entry` must be told which band a crop came from.

The parent slot holds an id **or a name**: where the father's generation id is
unknown, the page prints his name instead. Both are links to the previous
generation, so `parent_id` and `parent_name` are scored together. Named fathers
are common in the 教 band, which makes it the rare-character band.

A bare `子` marks an only son and ranks as 長子. It is stored exactly as printed
and ranked through `ParsedEntry.order_rank` — the rank is derived, the
transcription is never rewritten.

The block prints 教 as the variant form 敎 (U+654E). That one substitution is
mapped explicitly in `LABEL_VARIANTS` and recorded on the parse result; glyphs
that merely resemble a label (族 / 康 / 鹿 for 庶) are recognition errors and are
left to fail loudly.

## Layout

```
src/familyocr/
  document/      PDF ingest, original-raster preservation, checksums
  imaging/       variants, watermark suppression, debug overlays
  layout/        rule detection, frame fitting, normalization, template discovery
  segmentation/  entry lattice and crop generation
  validation/    Chinese numeral parsing, sequence checking
  compute/       ComputeBackend protocol (LocalBackend only, for now)
  persistence/   SQLite schema
  cli/           stage commands
artifacts/       generated; safe to delete and regenerate
docs/            corpus-analysis.md, layout-poc-report.md
configs/         learned template YAML (hand-editable)
```

`artifacts/pages/<doc>/original/` holds the untouched embedded JPEG2000 streams.
Nothing in the pipeline writes to them after extraction.

## Tests

```bash
.venv/bin/python -m pytest -q
```

Structural tests only, on synthetic pages: coordinate transforms round-trip,
band boundaries stay ordered, crops stay in bounds, numerals parse or fail
loudly. The real corpus is exercised through the stage reports.

## Status

Deliverable 1 (corpus + layout proof of concept) is implemented — see
`docs/layout-poc-report.md`. Deliverable 2 (OCR benchmark) and Deliverable 3
(cross-page continuity) have not started; no OCR backend is wired up yet.
