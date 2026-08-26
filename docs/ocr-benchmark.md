# OCR benchmark — `丙辰庶富教1`

Generated 2026-08-16T13:44:41+00:00 by `foresight-ocr report-ocr`. Numbers come from the artifacts of the last `benchmark` or `rescore` run.

## What is being measured

Two scoring paths, deliberately independent:

**Sequence checksum** — entry IDs run consecutively through the volume, so per band a correct transcription must be a gap-free increasing run. This needs no ground truth and covers every entry scored, which makes it the metric with real statistical weight. It is only valid on a *contiguous* page range: on a sampled page set the gaps are real and the check cannot tell them from OCR errors.

**Gold set** — 5 hand-verified entries (丙辰庶富教1_p0058.tsv). Small, but the only way to measure character error and exact-field accuracy. Entries whose characters could not be read with confidence were left out rather than guessed.

Exact-field accuracy on `own_id` is the headline, not CER. One wrong digit in `庶三百四十九` is a mild CER and a completely different person.

Only whitespace is normalized before comparison — PaddleOCR-VL separates the printed fields with newlines and PP-OCR concatenates them, which is formatting, not recognition. No Traditional-to-Simplified folding and no Unicode canonicalization is applied anywhere.

## Results

| backend | config | variant | crop | read | id parsed | clean run | gold exact-id | CER | ms/crop |
|---|---|---|---|---|---|---|---|---|---|
| `paddleocr_vl` | default | `binary` | `tight` | 376/378 | 91.5% | 46.65% | 40.0% | 0.222 | 757 |
| `ppocr_v5` | default | `binary` | `tight` | 371/378 | 87.8% | 38.91% | 0.0% | 0.333 | 1445 |
| `paddleocr_vl` | default | `maxrgb` | `tight` | 378/378 | 98.7% | 91.35% | 100.0% | 0.000 | 762 |
| `paddleocr_vl` | batched | `maxrgb` | `tight` | 60/60 | 95.0% | 83.33% | — | — | — |
| `paddleocr_vl` | scale-0.4 | `maxrgb` | `tight` | 378/378 | 98.4% | 95.66% | 80.0% | 0.032 | 292 |
| `paddleocr_vl` | scale-0.6 | `maxrgb` | `tight` | 378/378 | 98.4% | 94.58% | 80.0% | 0.032 | 302 |
| `ppocr_v5` | default | `maxrgb` | `tight` | 376/378 | 91.8% | 75.00% | 100.0% | 0.016 | 1491 |
| `paddleocr_vl` | default | `original` | `tight` | 378/378 | 98.7% | 80.81% | 100.0% | 0.000 | 1093 |
| `ppocr_v5` | default | `original` | `tight` | 378/378 | 90.5% | 70.80% | 100.0% | 0.016 | 1498 |

`read` counts crops that produced any text. `id parsed` is the share of entries where an `own_id` could be extracted at all. `clean run` is the share of consecutive-ID transitions that were exactly +1 — the sequence checksum.

### Sequence checksum by band

| backend | config | variant | band | entries | parsed | range | clean run | findings |
|---|---|---|---|---|---|---|---|---|
| `paddleocr_vl` | default | `binary` | 富 | 126 | 118 (93.7%) | 387–412 | 24.79% | 124 |
| `paddleocr_vl` | default | `binary` | 庶 | 126 | 107 (84.9%) | 287–412 | 54.72% | 79 |
| `paddleocr_vl` | default | `binary` | 教 | 126 | 121 (96.0%) | 287–413 | 60.83% | 71 |
| `ppocr_v5` | default | `binary` | 富 | 126 | 92 (73.0%) | 287–412 | 27.47% | 117 |
| `ppocr_v5` | default | `binary` | 庶 | 126 | 122 (96.8%) | 603–680 | 20.66% | 116 |
| `ppocr_v5` | default | `binary` | 教 | 126 | 118 (93.7%) | 287–412 | 66.67% | 60 |
| `paddleocr_vl` | default | `maxrgb` | 富 | 126 | 124 (98.4%) | 287–412 | 79.67% | 37 |
| `paddleocr_vl` | default | `maxrgb` | 庶 | 126 | 125 (99.2%) | 287–412 | 97.58% | 4 |
| `paddleocr_vl` | default | `maxrgb` | 教 | 126 | 124 (98.4%) | 287–412 | 96.75% | 6 |
| `paddleocr_vl` | batched | `maxrgb` | 富 | 18 | 17 (94.4%) | 287–304 | 56.25% | 10 |
| `paddleocr_vl` | batched | `maxrgb` | 庶 | 24 | 23 (95.8%) | 287–310 | 95.45% | 2 |
| `paddleocr_vl` | batched | `maxrgb` | 教 | 18 | 17 (94.4%) | 287–304 | 93.75% | 2 |
| `paddleocr_vl` | scale-0.4 | `maxrgb` | 富 | 126 | 124 (98.4%) | 287–412 | 91.87% | 16 |
| `paddleocr_vl` | scale-0.4 | `maxrgb` | 庶 | 126 | 125 (99.2%) | 287–412 | 97.58% | 4 |
| `paddleocr_vl` | scale-0.4 | `maxrgb` | 教 | 126 | 123 (97.6%) | 287–412 | 97.54% | 6 |
| `paddleocr_vl` | scale-0.6 | `maxrgb` | 富 | 126 | 124 (98.4%) | 287–412 | 88.62% | 20 |
| `paddleocr_vl` | scale-0.6 | `maxrgb` | 庶 | 126 | 125 (99.2%) | 287–412 | 97.58% | 4 |
| `paddleocr_vl` | scale-0.6 | `maxrgb` | 教 | 126 | 123 (97.6%) | 287–412 | 97.54% | 6 |
| `ppocr_v5` | default | `maxrgb` | 富 | 126 | 96 (76.2%) | 287–412 | 43.16% | 93 |
| `ppocr_v5` | default | `maxrgb` | 庶 | 126 | 126 (100.0%) | 287–412 | 80.00% | 28 |
| `ppocr_v5` | default | `maxrgb` | 教 | 126 | 125 (99.2%) | 287–412 | 94.35% | 11 |
| `paddleocr_vl` | default | `original` | 富 | 126 | 124 (98.4%) | 287–412 | 47.97% | 95 |
| `paddleocr_vl` | default | `original` | 庶 | 126 | 125 (99.2%) | 287–412 | 97.58% | 4 |
| `paddleocr_vl` | default | `original` | 教 | 126 | 124 (98.4%) | 287–412 | 96.75% | 6 |
| `ppocr_v5` | default | `original` | 富 | 126 | 91 (72.2%) | 287–412 | 25.56% | 110 |
| `ppocr_v5` | default | `original` | 庶 | 126 | 126 (100.0%) | 287–412 | 80.00% | 28 |
| `ppocr_v5` | default | `original` | 教 | 126 | 125 (99.2%) | 287–412 | 94.35% | 11 |

### What the errors are

- `paddleocr_vl` / default / `binary`: gap ×94, non_monotonic ×89, duplicate ×59, unparsed ×32
- `ppocr_v5` / default / `binary`: gap ×103, non_monotonic ×98, unparsed ×46, duplicate ×46
- `paddleocr_vl` / default / `maxrgb`: gap ×17, non_monotonic ×15, duplicate ×10, unparsed ×5
- `paddleocr_vl` / batched / `maxrgb`: gap ×5, non_monotonic ×4, unparsed ×3, duplicate ×2
- `paddleocr_vl` / scale-0.4 / `maxrgb`: gap ×10, unparsed ×6, non_monotonic ×6, duplicate ×4
- `paddleocr_vl` / scale-0.6 / `maxrgb`: gap ×12, non_monotonic ×8, unparsed ×6, duplicate ×4
- `ppocr_v5` / default / `maxrgb`: gap ×48, non_monotonic ×38, unparsed ×31, duplicate ×15
- `paddleocr_vl` / default / `original`: non_monotonic ×37, gap ×34, duplicate ×29, unparsed ×5
- `ppocr_v5` / default / `original`: gap ×57, non_monotonic ×42, unparsed ×36, duplicate ×14

`gap` and `non_monotonic` are recognition or segmentation failures. `unparsed` means no ID could be read at all — which is the *safe* failure: it is visibly missing rather than confidently wrong. `duplicate` usually means one physical entry was cut twice.

## Backend agreement

| pair | variant | crop | compared | same `own_id` | rate | identical full text |
|---|---|---|---|---|---|---|
| `paddleocr_vl` vs `ppocr_v5` | `binary` | `tight` | 369 | 140 | 37.94% | 0 |
| `paddleocr_vl` vs `ppocr_v5` | `maxrgb` | `tight` | 376 | 274 | 72.87% | 0 |
| `paddleocr_vl` vs `ppocr_v5` | `original` | `tight` | 378 | 248 | 65.61% | 0 |

Agreement is corroboration, not proof — two models trained on similar data can share a failure mode, which is exactly why the brief warns against naive majority voting. Its real value is the **disagreement list**: those crops are where a human should look first, and they are recorded in `results.json`.

Sample disagreements:

```
p0050_b0_e00_tight                     paddleocr_vl=None  ppocr_v5=None
p0050_b0_e01_tight                     paddleocr_vl=庶二百八十八  ppocr_v5=庶二百八十八九三百二十
p0050_b0_e02_tight                     paddleocr_vl=庶二百八十九  ppocr_v5=庶二百八十九九百六十
p0050_b0_e05_tight                     paddleocr_vl=None  ppocr_v5=庶二百九十二
p0050_b1_e01_tight                     paddleocr_vl=富二百八十八  ppocr_v5=富二百八十八百廿三
p0050_b1_e02_tight                     paddleocr_vl=富二百八十  ppocr_v5=None
p0050_b1_e03_tight                     paddleocr_vl=富二百九  ppocr_v5=富百九
p0050_b1_e04_tight                     paddleocr_vl=富二百九十  ppocr_v5=富十
p0051_b0_e05_tight                     paddleocr_vl=None  ppocr_v5=None
p0051_b1_e00_tight                     paddleocr_vl=富二百九十三  ppocr_v5=富一百九十三
```

## Throughput

Per-crop inference dominates wall clock; subprocess spawn and model load are single-digit percent. Two levers were measured:

**Downscaling wins twice.** Crops come off a 535 ppi scan at roughly 150 px per character, about three times what a recognizer needs, and dynamic-resolution encoders bill by patch count. At 0.4× the model is ~2.6× faster *and* scores better on the sequence checksum.

That improvement is not uniform, and the two metrics disagreed at first: downscaling makes the **numerals** more reliable while making the **band-label glyphs** worse (庶 read as 庚, 允 as 尤). Numerals are stroke-sparse and survive; 庶 and 允 are stroke-dense and blur. The gold set happened to be dominated by label errors, which is why five entries disagreed with three hundred and seventy-eight.

**Batching did not help here.** `mlx_vlm.batch_generate` groups images by shape, but entry crops vary in width because the segmentation lattice snaps to detected gutters — only 1 crop in 12 shares a shape, so the groups degenerate to singletons. Cutting entries to a fixed width would make batching worthwhile; it is not free, because a fixed width stops the crop from following the printed column.

## Reading the result

Best sequence-checksum score: **`paddleocr_vl`** on `maxrgb` / `tight` (scale-0.4 config) at **95.66%** clean transitions.

Cautions before treating any of this as a production decision:

- The gold set is small. Treat CER and rare-character numbers as indicative, and grow the gold set from the crops the checksum flags — that is where the errors actually are, so hand-verification effort goes furthest there.
- A high clean-run rate does not prove correct transcription. The checksum verifies that IDs form a consecutive run; a systematic offset affecting every entry equally would pass it. The gold set is what rules that out.
- The dominant failure mode on this volume is numeral substitution, not the rare-character confusion the project brief anticipates. Both corrupt the tree; only the first is caught for free.
- Nothing here was auto-corrected. Every raw answer from every backend is preserved in `ocr_candidates`, and validation findings are recorded as `needs_review`.
- The band label is taken from **page geometry**, not from the recognizer, when the printed glyph comes back unusable. Which band a crop belongs to was established by rule and frame detection, independently of OCR, so requiring the model to re-derive it threw away correct numerals — id extraction rose from 93.9% to 98.7% once it stopped doing so. This is not repair from linguistic plausibility: the raw transcription is untouched and every such entry carries `label_from_geometry` plus the glyph actually seen.
