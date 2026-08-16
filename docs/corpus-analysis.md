# Corpus analysis — `丙辰庶富教1`

Generated 2026-08-16T10:16:11+00:00 by `familyocr inspect`.

## Document

| property | value |
|---|---|
| source | `source/丙辰庶富教1.pdf` |
| sha256 | `d1b4bab6f328f6acfa24e3738927325025e0708a00fcef76d86e9eabd2f7d744` |
| pages | 201 |
| PDF version | 1.5 |
| creator | FreePic2Pdf |
| producer | FreePic2Pdf_Lib - v3.07 |

## Raster uniformity

Each row lists the distinct values observed across all pages. A single value means the corpus is homogeneous for that property; multiple values mean later stages must branch.

| property | observed values (count) |
|---|---|
| geometry | `2424x3744` ×201 |
| colorspace | `jpx-internal` ×201 |
| encoding | `jpx` ×201 |
| ppi | `535` ×201 |
| images_per_page | `1` ×201 |

All 201 pages share identical geometry, encoding and resolution. Extraction and normalization can assume one raster profile; any page that later fails frame detection is a physical-damage outlier, not a format outlier.

## Encoding note

Every page holds a single JPEG2000 (`JPXDecode`) stream. Colorspace and bit depth live inside the codestream rather than in the PDF image dictionary, which is why `colorspace` reads `jpx-internal` above.

Because the embedded stream is already a complete `.jp2` file, `familyocr extract` copies those bytes verbatim as the archival original. Re-rasterizing the PDF page would resample data we already hold losslessly.

## Stream sizes

| statistic | bytes |
|---|---|
| min | 485,608 |
| median | 486,152 |
| max | 486,202 |
| total | 97,706,131 |

Compression ratio is roughly uniform, so no page is dramatically noisier or blanker than the rest at the codec level.

## Per-page detail

Full machine-readable detail is in `artifacts/analysis/丙辰庶富教1/inspect/structure.json`. Pages that deviate from the modal profile are listed below.

No deviations: all 201 pages are `2424x3744`.

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
