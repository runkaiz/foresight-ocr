# Layout POC report — `丙辰庶富教1`

Generated 2026-08-16T10:16:42+00:00 by `familyocr report`. Every number below comes from the artifacts of the most recent pipeline run.

## Corpus characteristics

- 201 pages, one embedded raster each
- geometry: 2424x3744
- encoding: jpx; resolution 535 ppi
- producer: FreePic2Pdf_Lib - v3.07
- source sha256: `d1b4bab6f328f6acfa24e3738927325025e0708a00fcef76d86e9eabd2f7d744`

The corpus is completely homogeneous at the file level, so no stage needs to branch on raster format. Everything that goes wrong later is physical damage to the paper, not variation in the scan.

Content is a **雁序圖** (generation-order chart), not narrative 世系 prose: three horizontal bands per page (`庶字第 / 富字第 / 教字第`, generations 31–33), entries read right to left, each holding an ID in Chinese numerals plus a child-order marker, with personal names only as small-type side annotations.

## Frame-detection reliability

| outcome | pages | meaning |
|---|---|---|
| clean | 195 / 201 (97.0%) | all four borders detected |
| inferred edge | 1 / 201 (0.5%) | one border placed from the corpus prior |
| failed | 5 / 201 (2.5%) | no defensible frame |

Failed pages: p1 (no horizontal rule can anchor the frame), p3 (non-rectangular by 64px), p196 (non-rectangular by 113px), p197 (non-rectangular by 139px), p200 (no vertical rule can anchor the frame)

Detection is anchored on the printed rules rather than on text. Two filters do the work: length (a long 1-D morphological opening) and thinness. Thinness is not optional — in vertical Chinese text a column of characters is itself vertically continuous, so length alone cannot tell a printed rule from a column of text.

The left and right borders are recovered from **where the horizontal rules stop**. The band separators run border to border, so their endpoints locate the vertical edges directly; that works on many pages where vertical-rule detection finds nothing usable.

## Normalization accuracy

- canonical page: **2300 × 3025 px**, taken from the corpus median frame
- frame size spread (MAD): **3.1 × 4.8 px** (0.13% × 0.16%)
- scan skew: median 0.30°, p95 0.75°, max 0.98°
- worst original→canonical→original round-trip error: **0.0000 px**

Canonical space is defined from the median frame across the corpus, not from page 1. That matters twice: it keeps one page's scan displacement out of every downstream coordinate, and it is what exposes a page whose border was missed — such a frame comes out hundreds of pixels narrow and gets flagged instead of silently normalized.

## Watermark suppression results

Scored on 12 pages spread across the book. Pixel sets are defined on the original colour page: watermark-only (high chroma, not dark), ink-under-stamp (high chroma and dark), clean ink (neutral and dark), paper (neutral and bright).

| variant | watermark residual ↓ | ink contrast under stamp ↑ | ink retention vs clean ink ↑ |
|---|---|---|---|
| `gray` | 48.8 | 67.2 | 0.806 |
| `red` | 90.4 | 110.1 | 1.320 |
| `maxrgb` | 25.1 | 44.0 | 0.527 |
| `lab_l` | 38.7 | 56.2 | 0.698 |
| `neutral` | 20.4 | 33.4 | 0.400 |
| `inpaint` | 5.4 | 20.4 | 0.246 |
| `contrast` | 39.2 | 68.1 | 0.680 |
| `binary` | 78.4 | 101.1 | 0.612 |

**What this shows.**

- The channel hypothesis holds in the predicted direction. Cyan pigment absorbs red, so in the red channel the stamp is *darker* than anywhere else (residual 90 vs 49 for plain luminance) — the intuitive "use the red channel" move is the worst available option.
- Per-pixel max over R,G,B roughly halves the stamp (25 vs 49).
- But it is **not free for the ink underneath**: ink retention falls to 0.53 against 0.81 for luminance. Where a cyan stamp overlies a black stroke, taking the brightest channel lifts the stroke too. The plan predicted max-RGB would keep ink dark; measured, it costs roughly a third of the stroke contrast under the stamp.
- Inpainting is the clearest warning: it removes almost all of the stamp (residual 5) while destroying most of the ink beneath it (retention 0.25). A variant that scores well on watermark removal alone is exactly what must not be trusted.

**No variant dominates**, so none is promoted to "the" preprocessing step. All variants are kept and the choice is deferred to Deliverable 2, where OCR accuracy decides it. Side-by-side crops are in `artifacts/analysis/丙辰庶富教1/watermark/`.

## Layout regularity

- bands per page: **3**
- band boundaries in canonical space: 0, 1012, 1999, 3025
- boundary spread (MAD): 0.0 px, 4.5 px, 5.4 px, 0.0 px — at most 0.18% of page height
- entry column pitch: **378.0 px** (MAD 2.0 px, 0.53%)
- text block spans x = 0 … 2296
- pages contributing to the template: 194

- column-pitch confidence (autocorrelation peak): median 0.64, min 0.24

This is an unusually regular document. Band boundaries vary by a few pixels across two hundred pages after normalization, and the entry pitch varies by well under one percent. Automatic template discovery is not merely feasible here — hand-declared coordinates would be strictly worse, because the learned template carries a measured spread that a hardcoded number cannot.

## Identified layout variants

| family | pages | examples |
|---|---|---|
| `layout_A` | 194 | 2, 4, 5, 6, 7, 8, 9, 11, 12, 13, 14, 15 … |
| `outlier` | 2 | 10, 181 |

Pages are clustered on band count plus a coarse ink profile, with band count weighted heavily — a page with a different number of generation bands is a different layout regardless of how its ink happens to be distributed. Pages that match no cluster land in `outlier` rather than being forced into a family.

The result is one dominant layout plus a small outlier set. The book does not need multiple templates, but the machinery to detect that it might is in place for the volumes that follow.

## Automatic-template feasibility

Feasible, and already done. The learned template is written to `configs/template_丙辰庶富教1.yaml` in plain YAML and is intended to be hand-edited: nothing in the pipeline treats it as immutable, and no threshold that matters is buried as a magic constant in source.

## Problematic pages

- **frame failures (5)**: p1, p3, p196, p197, p200
- **inferred edge (1)**: p201
- **layout outliers (2)**: p10, p181

Page 1 is the cover and correctly refuses to produce a chart frame. The remaining failures are physically damaged pages — torn edges and heavy wrinkling near the end of the volume. Pages with an inferred edge are usable but carry a hypothesis, and are marked `needs_review` in the `transforms` table rather than being counted as clean fits.

## Recommended segmentation strategy

Cut entries from a **pitch-spaced lattice snapped to detected gutters**, seeded at the right-hand text edge and stepping left. Pure gutter-splitting breaks wherever two entries touch or an entry is blank; pure pitch-stepping accumulates drift across a page. Snapping keeps the regularity of the lattice and the local accuracy of the gutters, and it degrades gracefully — a single missing gutter costs nothing.

Emit three crop widths per entry (`tight`, `medium`, `full`) and let Deliverable 2 measure which one recognizers actually prefer. A crop tight enough to clip a neighbouring glyph can remove exactly the context needed to resolve an ambiguous character, so crop size is a benchmark variable, not a setting to guess.

Each crop records both its canonical bbox and its quadrilateral in original pixels, obtained through the inverse homography, so every later transcription remains traceable to source pixels.

Run so far: **360 entries** cut from 20 pages (×3 context widths). Entry counts come out at roughly six per band per page, matching the printed layout.

### Sequence check, exercised on real data

The six 庶-band entries of page 58 were transcribed by hand from their crops and run through `familyocr validate`:

```
band 庶 | 6 entries | 6 parsed (100%) | range 335–340 | clean transitions 100.00% | 0 findings
```

They read 三百三十五 … 三百四十 — consecutive, exactly as the sequence property predicts. Those rows live in `benchmarks/gold/丙辰庶富教1_p0058.tsv` and are the seed of the Deliverable-2 gold set.

## Recommended next experiment

Build the OCR benchmark (Deliverable 2), and exploit the property that makes this corpus unusual: **entry IDs are sequential**. Per band, a correct transcription must be a complete, strictly increasing integer run across all 200 chart pages, so `familyocr validate` measures error rate over roughly 3,600 entries with no manual ground truth and points at the exact crop that broke.

Concretely, in order:

1. Run the candidate backends over the same crops: PaddleOCR-VL 1.6 (0.9B, via mlx-vlm on Metal), PP-OCRv5 (Paddle CPU), and at least one ancient-Chinese specialist. Keep every model's raw answer.
2. Score with `validate` first — it is free — then hand-verify a small gold set concentrated on the crops it flags, which is where the errors actually are.
3. Cross the backends with the image variants and the three crop widths. That is the experiment that settles both the watermark question and the context-size question, and neither should be settled before it.

A caution specific to this volume: the dominant failure mode is **numeral substitution** (`三百四十九` → `三百四十八`), not the rare-character confusion the project brief anticipates. Both corrupt the tree, but only the first is caught for free by the sequence check — and a language model asked to "clean up" a numeral will happily produce a plausible neighbour. Uncertainty must be preserved, not resolved.
