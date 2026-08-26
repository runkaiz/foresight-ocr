# Foresight OCR

Foresight OCR is a provenance-first command-line pipeline for transcribing
scanned Chinese genealogy records into reviewable text and structured family
data. It is designed for vertical Traditional Chinese, repeated genealogy
layouts, damaged pages, watermarks, and records that cross physical pages.

The pipeline keeps source extraction, normalization, layout analysis, OCR,
human correction, validation, and genealogy reconstruction separate. Raw
source rasters and model output are preserved, and derived records retain links
back to their source pixels.

> Foresight OCR is currently a research preview. New document families require
> an explicit profile and a human-checked layout template.

## Install

Python 3.12 is required. The recommended installation is an isolated tool
environment:

```bash
uv tool install --python 3.12 foresight-ocr
foresight-ocr --version
foresight-ocr doctor
```

pipx is also supported:

```bash
pipx install --python 3.12 foresight-ocr
```

Signed standalone archives for supported Linux, macOS, and Windows targets are
published with releases. OCR model environments remain optional and isolated
from the core installation. Verify the downloaded archive against the release's
`SHA256SUMS`, keep its executable and `_internal` directory together, and run
`foresight-ocr doctor` before processing a document.

## Pipeline

```text
PDF
  -> original raster extraction
  -> page normalization and restoration variants
  -> generation-band and entry segmentation
  -> OCR candidates and structural validation
  -> human review
  -> people, father links, TSV, and GEDCOM
```

Start a project in an empty directory:

```bash
foresight-ocr inspect /path/to/volume.pdf --id my-volume
foresight-ocr extract my-volume
foresight-ocr normalize my-volume
foresight-ocr layout my-volume
foresight-ocr segment my-volume
foresight-ocr review my-volume
```

Use `foresight-ocr COMMAND --help` before processing irreplaceable material.
Generated state lives under `artifacts/`; source documents are never modified.

## Safety boundary

The local review server binds to `127.0.0.1` by default and has no
authentication. Do not expose it directly to an untrusted network.

Foresight OCR assists transcription and review. It does not establish the
historical accuracy, identity, or relationships described by a source document.

## License

Apache-2.0. Standalone archives also include the license notices for bundled
third-party runtime dependencies.
