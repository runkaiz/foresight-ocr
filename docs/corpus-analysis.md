# Corpus analysis — `丙辰清廉麗熙2`

Generated 2026-08-16T14:46:00+00:00 by `familyocr inspect`.

## Document

| property | value |
|---|---|
| source | `source/丙辰清廉麗熙2.pdf` |
| sha256 | `45196ada5736b2c74181ea58f738d26b37d6eadc03e9508fbb3553b78e911e71` |
| pages | 351 |
| PDF version | 1.5 |
| creator | FreePic2Pdf |
| producer | FreePic2Pdf_Lib - v3.07 |

## Raster uniformity

Each row lists the distinct values observed across all pages. A single value means the corpus is homogeneous for that property; multiple values mean later stages must branch.

| property | observed values (count) |
|---|---|
| geometry | `2424x3744` ×350, `2388x3749` ×1 |
| colorspace | `jpx-internal` ×351 |
| encoding | `jpx` ×351 |
| ppi | `535` ×351 |
| images_per_page | `1` ×351 |

The corpus is **not** homogeneous. Stages downstream of extraction must branch on the properties listed above.

## Encoding note

Every page holds a single JPEG2000 (`JPXDecode`) stream. Colorspace and bit depth live inside the codestream rather than in the PDF image dictionary, which is why `colorspace` reads `jpx-internal` above.

Because the embedded stream is already a complete `.jp2` file, `familyocr extract` copies those bytes verbatim as the archival original. Re-rasterizing the PDF page would resample data we already hold losslessly.

## Stream sizes

| statistic | bytes |
|---|---|
| min | 485,840 |
| median | 486,159 |
| max | 488,178 |
| total | 170,637,536 |

Compression ratio is roughly uniform, so no page is dramatically noisier or blanker than the rest at the codec level.

## Per-page detail

Full machine-readable detail is in `artifacts/analysis/丙辰清廉麗熙2/inspect/structure.json`. Pages that deviate from the modal profile are listed below.

| page | geometry | encoding | images |
|---|---|---|---|
| 1 | 2388x3749 | jpx | 1 |

## Content structure (manual observation)

Recorded here because it drives every downstream design decision and is not derivable from the PDF structure alone:

- Page 1 is the cover: `卷十 雁序圖 庶富教`.
- Pages 2–201 are a **雁序圖** (generation-order chart), not narrative 世系 prose.
- Page 2 heads the chart: `富陽長壽章氏宗譜雁序圖`, `第三十一世 / 第三十二世 / 第三十三世`.
- Each chart page carries **three horizontal bands**, one per generation, labelled `庶字第 / 富字第 / 教字第`.
- Each cell holds an ID in Chinese numerals (e.g. `庶五百八十七`) plus a child-order marker (`長子 / 次子 / 三子`).
- Personal names appear only as small-type side annotations (`仕燧`, `錦`, `學高`).
- Columns read right-to-left; IDs run sequentially into the thousands across the whole volume.
- Degradation present: cyan `富陽圖書館` watermark near page centre, bleed-through, mild skew, torn edges near p.200.

The sequential IDs are the most useful property of this corpus: per band, the numbers must be monotonically increasing and gap-free across all 200 chart pages. That is a global checksum on OCR output requiring no manual ground truth — see `familyocr validate`.
