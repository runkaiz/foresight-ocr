# Project: Chinese Genealogy OCR & Structured Digitization System

Build a system for digitizing historical Chinese family genealogy records (族谱 / 宗谱) from scanned PDFs.

The first canonical test document is:

`丙辰庶富教1.pdf`

Do not start by building a generic OCR application. Inspect this document directly and use it as the first real corpus against which architecture and accuracy are evaluated.

The objective is **maximum reliable transcription and structural reconstruction**, not minimum compute usage.

We may use:

- local CPU inference
- local GPU inference where available
- rented/cloud NVIDIA GPUs
- remote inference workers
- multiple OCR/VLM models
- ensemble recognition

Do not constrain architectural decisions around a 2017 iMac Pro.

The application should nevertheless support a local Mac as the primary review workstation.

---

# Core objective

Convert scanned genealogy documents into:

1. original source page images,
2. geometrically normalized pages,
3. detected physical genealogy structure,
4. accurately transcribed Traditional Chinese text,
5. logical records spanning arbitrary physical pages,
6. reconstructed genealogy relationships,
7. auditable provenance back to source pixels,
8. a human-verification workflow,
9. structured data suitable for family-tree/database export.

Accuracy and auditability take priority over computational efficiency.

---

# Fundamental architectural principle

Keep these stages independent:

```text
PDF / source acquisition
        ↓
image extraction
        ↓
image restoration / normalization
        ↓
physical layout understanding
        ↓
text-region detection
        ↓
OCR / visual recognition
        ↓
cross-page structural continuity
        ↓
genealogical semantic parsing
        ↓
validation / ensemble comparison
        ↓
human review
        ↓
structured genealogy database
```

Do **not** turn the entire problem into:

```text
page image → giant VLM prompt → final JSON
```

A VLM may be one component, but all important intermediate representations must remain inspectable.

---

# Source-document characteristics

Inspect the supplied PDF yourself and verify these assumptions.

Expect:

- historical Chinese genealogy material
- vertical Chinese writing
- primarily Traditional Chinese characters
- rare / historical characters
- repetitive genealogy layout
- strong rectangular page boundaries
- recurring horizontal generational regions
- repeated vertical text structures
- page-to-page scan displacement
- skew and perspective distortion
- faded or damaged printing
- bleed-through
- wrinkles
- physical page damage
- central cyan/turquoise `富阳图书馆 / FUYANG LIBRARY` watermark
- primarily grayscale original document beneath that watermark
- logical genealogy structures that continue between physical PDF pages

A physical page is **not** a logical record boundary.

---

# Accuracy philosophy

Genealogy transcription has an unusually asymmetric error cost.

For example:

```text
張廷瓚
```

being transcribed as:

```text
張廷讚
```

is not an acceptable semantic approximation.

Do not allow language models to silently replace unclear glyphs with linguistically probable characters.

Preserve uncertainty.

The system should prefer:

```text
unknown / uncertain character
```

over a confident hallucination.

---

# Provenance-first data model

Every textual result must be traceable to its image source.

Never assume:

```text
one logical field = one bounding box
```

Instead:

```python
SourceRegion:
    page_index: int
    bbox: BBox
    normalized_bbox: BBox | None
    crop_id: str
    transform_id: str

LogicalField:
    id: str
    type: str | None
    transcription: str | None
    source_regions: list[SourceRegion]
    confidence: float | None
```

A record may contain:

```text
page 31 bottom
+
page 32 top
+
page 33 top
```

if that is what the source document requires.

---

# Compute architecture

Design processing so execution location is independent of document logic.

Conceptually:

```text
                 WORKSTATION / APP
                         │
                Processing Scheduler
                         │
          ┌──────────────┼──────────────┐
          ▼              ▼              ▼
       Local CPU     Local GPU      Cloud GPU
          │              │              │
          └──────────────┼──────────────┘
                         ▼
                  same result schema
```

Processing stages should declare requirements rather than assuming hardware.

Example:

```python
ComputeRequest:
    task_type
    model
    minimum_vram
    preferred_device
    batch_size
```

Possible targets:

```text
cpu
cuda
mps
remote_cuda
```

Do not intertwine CUDA-specific code with document logic.

---

# Cloud execution

Support optional GPU workers.

A remote worker should be able to receive:

```text
job
model configuration
page/crop assets
```

and return:

```text
structured inference result
model metadata
confidence
timings
```

Cloud execution may eventually use providers such as:

- RunPod
- Vast.ai
- Lambda
- Modal
- AWS/GCP/Azure GPU instances
- another generic CUDA machine

Do not tightly couple the project to any one provider.

Implement the remote execution layer behind a generic interface.

Example:

```python
class ComputeBackend(Protocol):
    def execute(self, job: InferenceJob) -> InferenceResult: ...
```

Possible implementations:

```text
LocalBackend
RemoteWorkerBackend
```

Provider integration comes later.

---

# Model architecture

Do not lock the project to one OCR model.

Define interchangeable model backends.

Example:

```python
class OCRBackend(Protocol):
    def recognize(self, image, options=None) -> OCRResult: ...
```

Potential backends:

```text
PP-OCRv5
PaddleOCR-VL 1.6
other Chinese OCR models
future historical-Chinese models
experimental VLMs
```

The pipeline must be capable of benchmarking multiple backends against identical crops.

---

# PaddleOCR-VL 1.6

PaddleOCR-VL 1.6 should be treated as a serious primary candidate rather than merely a low-confidence fallback.

Since GPU compute is allowed, benchmark at least:

```text
A. PaddleOCR-VL on tightly segmented regions

B. PaddleOCR-VL on larger structural regions

C. PP-OCR on identical crops

D. ensemble / disagreement workflow
```

Do not assume beforehand which configuration is best.

Measure it.

---

# Ensemble recognition

The architecture should support multiple independent recognizers.

Example:

```text
              source crop
             /           \
            /             \
    PaddleOCR-VL        PP-OCR
          │                 │
          ▼                 ▼
       張廷瓚              張廷瓚
            \             /
             \           /
               AGREEMENT
                   │
                 accept
```

Versus:

```text
    PaddleOCR-VL        PP-OCR
          │                 │
          ▼                 ▼
       張廷瓚              張廷讚
             \            /
              DISAGREEMENT
                   │
            validation stage
                   │
          human review if needed
```

Do not use naive majority voting if all models may share the same failure mode.

Keep every model's raw answer.

---

# Potential higher-level verifier

Allow an optional VLM verifier to inspect:

```text
source crop
candidate A
candidate B
neighboring characters
genealogy context
```

but the verifier must never silently overwrite the source transcription.

Its output should be:

```python
VerificationResult:
    preferred_candidate
    confidence
    reasoning_summary
    needs_human_review
```

The underlying competing OCR outputs must remain available.

---

# Phase 1 — Corpus inspection

Programmatically inspect `丙辰庶富教1.pdf`.

Determine:

- PDF page count
- embedded raster dimensions
- image encoding
- whether original images can be extracted directly
- resolution consistency
- color characteristics
- grayscale/chroma distribution
- page-frame geometry
- amount of scan displacement
- skew distribution
- perspective distortion
- recurring page layouts
- exceptional pages

Sample pages from:

```text
beginning
first quarter
middle
third quarter
end
```

and deliberately include damaged/outlier pages.

Output:

```text
artifacts/analysis/
```

plus:

```text
docs/corpus-analysis.md
```

---

# Phase 2 — Preserve original scans

Prefer extracting the actual embedded raster images rather than unnecessarily rasterizing PDF pages again.

Never modify source images.

Conceptual representation:

```python
PageImage:
    document_id
    page_index
    width
    height
    source_path
    checksum
```

All derivatives are separate assets.

---

# Phase 3 — Geometric normalization

Automatically recover the primary page frame.

Estimate:

- translation
- rotation
- scale
- mild perspective distortion

Transform normal genealogy pages into a canonical coordinate system.

Store both:

```text
forward transform
inverse transform
```

so any normalized coordinate maps exactly back to the original source image.

Do not hardcode original pixel coordinates.

Generate overlays showing the detected frame and transform.

---

# Phase 4 — Image restoration pipeline

Treat restoration as an experimental, reversible stage.

Potential operations:

```text
watermark suppression
contrast normalization
background flattening
bleed-through reduction
denoising
local sharpening
adaptive binarization
```

Do not destructively combine everything into one preprocessing operation.

Keep variants.

Example:

```text
original
normalized
watermark_suppressed
contrast_enhanced
binary
```

OCR backends may perform differently on different variants.

---

# Cyan watermark removal

The prominent library watermark should receive a dedicated experiment.

Because it has significant chroma while most historical ink is approximately neutral, investigate:

- per-channel reconstruction
- RGB channel minima/maxima
- HSV chroma masks
- LAB chroma channels
- color-distance-from-neutral masks
- inpainting only where justified
- channel fusion

The goal is:

```text
remove watermark pigment
while retaining dark historical ink beneath it
```

Do not simply erase the watermark's bounding rectangle.

Generate quantitative and visual comparisons.

---

# Phase 5 — Physical layout recovery

Physical structure comes before semantics.

Detect:

```text
outer page frame
major horizontal separators
generation bands
vertical entry streams
printed connector lines
special side columns
headers / marginal annotations
```

Represent these as geometry.

Example:

```python
PageLayout:
    page_index
    frame
    bands
    connectors
    exceptional_regions
```

and:

```python
Band:
    bbox
    entry_regions
```

---

# Template discovery

Do not manually declare every coordinate.

Learn the recurring page geometry from multiple normalized pages.

Collect:

```text
horizontal separator positions
vertical text centers
entry widths
entry spacing
line positions
```

and cluster them.

Useful techniques may include:

```text
DBSCAN
Gaussian mixtures
hierarchical clustering
projection profiles
RANSAC
robust median estimation
```

Build an empirical template such as:

```python
DocumentTemplate:
    canonical_dimensions
    recurring_band_positions
    recurring_entry_positions
    positional_variances
    known_layout_variants
```

It must remain possible to manually edit the learned template later.

---

# Layout variants

Do not assume the entire book has exactly one page template.

Automatically detect whether pages cluster into multiple layout families.

Conceptually:

```text
layout A
layout B
layout C
outlier
```

A page should first be assigned to a layout family.

Only then apply layout-specific expectations.

---

# Phase 6 — Text segmentation

Generate OCR regions based on the physical layout.

For vertical Chinese, preserve orientation metadata.

Possible region hierarchy:

```text
page
 └─ generation band
      └─ physical entry
           └─ vertical text segment
```

Do not crop so tightly that neighboring context needed to identify a glyph is lost.

Experiment with:

```text
tight crop
medium-context crop
full-entry crop
```

and benchmark OCR quality.

---

# Phase 7 — OCR benchmark framework

Before choosing a production recognizer, construct a benchmark.

Select representative crops containing:

- clear common characters
- faded print
- watermark overlap
- rare characters
- damaged characters
- bleed-through
- tightly packed vertical text
- page-edge text

Manually establish a small gold-standard transcription dataset.

Then benchmark models.

Metrics should include:

```text
character error rate
exact-field accuracy
rare-character accuracy
false substitution rate
unknown-character rate
latency
GPU memory
```

**Exact-field accuracy is especially important for names.**

---

# OCR result schema

Something like:

```python
OCRResult:
    transcription: str
    backend: str
    model_version: str
    confidence: float | None
    character_results: list[CharacterResult]
    input_variant: str
    raw_result: dict | None
```

Never simplify Traditional Chinese automatically.

Never apply Unicode normalization that could destroy meaningful historical variants without recording the transformation.

---

# Phase 8 — Character-level uncertainty

Where possible, represent OCR confidence at the character level.

Example:

```text
張  .998
廷  .991
瓚  .61
```

This lets the review system highlight only:

```text
瓚
```

instead of forcing a human to inspect the whole line.

---

# Phase 9 — Cross-page continuity

Treat this as a core structural problem.

Do not assume:

```text
page boundary = genealogy boundary
```

Build a resolver operating on adjacent pages.

Potential evidence:

```text
same horizontal/generation band
continuing horizontal position
column adjacency
connector lines
record labels
child-order markers
absence of a new-record marker
matching structural geometry
```

Geometry should carry more weight than linguistic plausibility.

---

# Virtual spreads

Also experiment with reconstructing **logical spreads**.

Instead of analyzing each page only independently:

```text
page N + page N+1
```

may be treated as adjacent portions of a larger coordinate space.

For certain analyses, create:

```text
right edge / left edge adjacency
bottom / top boundary strips
```

depending on original reading direction and scan organization.

Do not lose physical-page metadata.

---

# Cross-page result

Example:

```python
LogicalEntry:
    id
    regions: [
        SourceRegion(page=42, ...),
        SourceRegion(page=43, ...)
    ]
```

Cross-page relationships should have:

```text
confidence
evidence
status
```

with status such as:

```text
automatic
confirmed
rejected
needs_review
```

---

# Phase 10 — Genealogy parser

Only after reliable OCR/layout exists should the application infer:

```text
generation
person
parent
child
spouse
birth/death information
child order
長子
次子
三子
etc.
```

The genealogy parser consumes structured OCR/layout.

It should not operate directly on raw page images unless explicitly using a visual verifier.

Prefer deterministic geometry and grammar first.

Use an LLM/VLM only for cases that remain ambiguous.

---

# Genealogy-specific grammar

Create an extensible rule system for recurring terms.

Examples may include:

```text
長子
次子
三子
四子
子
女
配
妣
生
卒
葬
嗣
```

Do not assume this initial vocabulary is complete.

Derive actual conventions from the source corpus.

---

# Phase 11 — Human review application

The review experience should eventually be excellent.

For each uncertain result show:

```text
high-resolution original crop
enhanced crop
surrounding page context
OCR candidate A
OCR candidate B
confidence
logical genealogy context
```

Human action should generally be:

```text
accept
choose candidate
type correction
mark unreadable
```

Keep keyboard navigation efficient.

---

# Original-versus-corrected data

Never overwrite machine transcription.

Store:

```text
machine transcription
human transcription
who/when corrected
source crop
model versions
```

Human corrections should survive complete reprocessing of the document.

---

# Family/book-specific lexicon

Build a lexicon from confirmed results.

Useful categories:

```text
known family surnames
known given-name characters
generation-name characters
place names
historical terminology
previously confirmed rare characters
```

This lexicon can aid ranking and review.

It must not silently force OCR outputs to match known names.

---

# Persistence

Use SQLite initially unless scale demonstrates a need for something else.

Suggested conceptual entities:

```text
documents
pages
page_assets
transforms
layout_runs
page_layouts
bands
physical_entries
source_regions
ocr_runs
ocr_candidates
ocr_characters
logical_records
logical_fields
cross_page_links
human_corrections
models
processing_runs
```

---

# Reproducibility

Every generated result must record:

```text
pipeline version
git commit
model
model version
processing parameters
input checksum
timestamp
compute backend
```

We should be able to answer:

> Why does this character currently read as 瓚?

and reconstruct the full chain that produced it.

---

# Processing graph

Pipeline stages should be individually rerunnable.

Conceptually:

```bash
foresight-ocr inspect document.pdf
foresight-ocr extract DOCUMENT
foresight-ocr normalize DOCUMENT
foresight-ocr restore DOCUMENT
foresight-ocr layout DOCUMENT
foresight-ocr segment DOCUMENT
foresight-ocr ocr DOCUMENT
foresight-ocr resolve DOCUMENT
foresight-ocr parse DOCUMENT
```

and eventually:

```bash
foresight-ocr run DOCUMENT
```

Changing OCR models must not require repeating PDF extraction or layout analysis.

---

# Task scheduling

Treat the pipeline as a dependency graph.

Example:

```text
extract
   ↓
normalize
   ↓
restore ─────┐
   ↓         │
layout       │
   ↓         │
segment ◄────┘
   ↓
ocr:model-A
ocr:model-B
   ↓
ensemble
   ↓
cross-page resolve
   ↓
genealogy parse
```

Cache outputs by input/config hashes where practical.

---

# Local/cloud division

A reasonable eventual deployment may look like:

```text
Mac application
│
├─ PDF library
├─ page inspection
├─ correction UI
├─ local database
├─ light CV processing
│
└── compute scheduler
        │
        ├── local processing
        │
        └── encrypted remote jobs
                │
                ▼
             GPU worker
                │
                ▼
             results
```

But do not prematurely commit to this exact topology.

Benchmark first.

---

# Privacy architecture

Cloud processing should remain optional.

The architecture should permit:

```text
fully local mode
```

as well as:

```text
cloud-accelerated mode
```

Cloud jobs should upload only what is needed for the requested operation when practical, such as individual crops rather than entire books.

Do not make cloud connectivity mandatory for accessing previously processed records.

---

# Debugging outputs

Every CV/layout stage must generate inspectable overlays.

Examples:

```text
outer-frame overlay
normalized-page grid
watermark mask
band boundaries
text-stream bounding boxes
connector-line detection
page-to-page correspondence
cross-page links
OCR confidence heatmap
```

An algorithm that cannot explain its geometry visually is not production-ready.

---

# Tests

Begin with structural tests.

Examples:

```text
PDF pages extract without corruption
source checksums remain constant
coordinate transforms round-trip
frame detection stays within tolerance
normalized pages share stable frame geometry
band boundaries remain ordered
OCR crops stay in bounds
cross-page logical fields can have >1 SourceRegion
pipeline restart does not lose work
human corrections survive reprocessing
```

Create a representative fixture set instead of processing the whole book during every test.

---

# Repository structure

A reasonable starting structure:

```text
foresight-ocr/
├── README.md
├── pyproject.toml
├── src/
│   └── foresight_ocr/
│       ├── document/
│       ├── imaging/
│       ├── layout/
│       ├── segmentation/
│       ├── ocr/
│       ├── compute/
│       ├── genealogy/
│       ├── persistence/
│       ├── review/
│       └── cli/
├── tests/
├── configs/
├── scripts/
├── benchmarks/
├── artifacts/
└── docs/
```

Adjust it if experimentation suggests a better organization.

---

# Configuration

Use explicit processing profiles.

For example:

```yaml
document_profile: bingchen_shufujiao_1

compute:
  mode: auto

normalization:
  frame_detection: true
  perspective_correction: true

restoration:
  watermark_suppression: true

layout:
  automatic_template_discovery: true
  allow_multiple_templates: true

ocr:
  backends:
    - paddleocr_vl_1_6
    - ppocr_v5
  ensemble: true

cross_page:
  enabled: true
```

No important threshold should be hidden as an unexplained magic constant deep in source code.

---

# FIRST DELIVERABLE

Do not build the polished application yet.

The first goal is to understand and reliably normalize the corpus.

Create a **Corpus + Layout Proof of Concept**.

It must:

1. ingest `丙辰庶富教1.pdf`,
2. inspect the PDF structure,
3. extract original source scans where possible,
4. sample at least 15 representative pages across the entire document,
5. include deliberately difficult/outlier pages,
6. detect the main page frame,
7. normalize those pages,
8. experiment with watermark suppression,
9. detect recurring horizontal genealogy structures,
10. detect candidate vertical entry regions,
11. measure page-to-page positional variance,
12. cluster pages into layout variants if necessary,
13. infer a preliminary canonical template,
14. output annotated visualizations,
15. output machine-readable layout metadata.

Then write:

```text
docs/layout-poc-report.md
```

covering:

```text
corpus characteristics
frame-detection reliability
normalization accuracy
watermark suppression results
layout regularity
identified layout variants
automatic-template feasibility
problematic pages
recommended segmentation strategy
recommended next experiment
```

---

# SECOND DELIVERABLE

After the layout POC succeeds, build an **OCR benchmark**, not the final application.

Create a manually verified ground-truth sample covering:

```text
normal characters
names
rare characters
faded characters
watermark-obscured text
damaged printing
page-edge text
```

Benchmark at minimum:

```text
PaddleOCR-VL 1.6
PP-OCRv5
```

on several image preprocessing variants and crop sizes.

If promising additional OCR/VLM models are available, include them.

Generate:

```text
docs/ocr-benchmark.md
```

Do not choose a production OCR architecture until this benchmark exists.

---

# THIRD DELIVERABLE

Prototype cross-page reconstruction using several real adjacent page pairs.

Visualize:

```text
page N regions
page N+1 regions
candidate continuations
confidence/evidence
```

Determine whether continuity can primarily be recovered from geometry.

Only after these three experiments should substantial effort go into the final application UI.

---

# Engineering behavior

Operate empirically.

When unsure:

1. inspect the source data,
2. form a measurable hypothesis,
3. implement the smallest experiment,
4. visualize the result,
5. measure it,
6. document findings,
7. then make the architectural decision.

Do not optimize for the current workstation.

Do not optimize prematurely for cloud cost either.

Optimize first for:

1. transcription accuracy,
2. provenance,
3. recoverability,
4. structural correctness,
5. human-review efficiency.

Performance and compute cost come after we know what actually works.

The immediate question is:

> Can we automatically recover a stable physical genealogy structure from this corpus and create reliable regions from which multiple OCR systems can be objectively evaluated?

Start there.
