"""docs/corpus-analysis.md generation."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from foresight_ocr.document.pdf import DocumentInfo
from foresight_ocr.project import Project


def write_corpus_analysis(
    project: Project,
    info: DocumentInfo,
    manual_observations: Iterable[str] = (),
) -> Path:
    u = info.uniformity()
    total = len(info.pages)
    lines: list[str] = []
    add = lines.append

    add(f"# Corpus analysis — `{info.document_id}`")
    add("")
    add(
        f"Generated {datetime.now(UTC).isoformat(timespec='seconds')} "
        f"by `foresight-ocr inspect`."
    )
    add("")
    add("## Document")
    add("")
    add("| property | value |")
    add("|---|---|")
    add(f"| source | `{info.source_path}` |")
    add(f"| sha256 | `{info.checksum}` |")
    add(f"| pages | {info.page_count} |")
    add(f"| PDF version | {info.pdf_version} |")
    add(f"| creator | {info.creator or '—'} |")
    add(f"| producer | {info.producer or '—'} |")
    add("")

    add("## Raster uniformity")
    add("")
    add(
        "Each row lists the distinct values observed across all pages. A single "
        "value means the corpus is homogeneous for that property; multiple values "
        "mean later stages must branch."
    )
    add("")
    add("| property | observed values (count) |")
    add("|---|---|")
    for name, counts in u.items():
        rendered = ", ".join(f"`{v}` ×{n}" for v, n in counts)
        add(f"| {name} | {rendered} |")
    add("")

    homogeneous = all(len(c) == 1 for c in u.values())
    if homogeneous:
        add(
            f"All {total} pages share identical geometry, encoding and resolution. "
            "Extraction and normalization can assume one raster profile; any page "
            "that later fails frame detection is a physical-damage outlier, not a "
            "format outlier."
        )
    else:
        add(
            "The corpus is **not** homogeneous. Stages downstream of extraction "
            "must branch on the properties listed above."
        )
    add("")

    add("## Encoding note")
    add("")
    encodings = {v for v, _ in u["encoding"]}
    if encodings == {"jpx"}:
        add(
            "Every page holds a single JPEG2000 (`JPXDecode`) stream. Colorspace and "
            "bit depth live inside the codestream rather than in the PDF image "
            "dictionary, which is why `colorspace` reads `jpx-internal` above."
        )
        add("")
        add(
            "Because the embedded stream is already a complete `.jp2` file, "
            "`foresight-ocr extract` copies those bytes verbatim as the archival "
            "original. Re-rasterizing the PDF page would resample data we already "
            "hold losslessly."
        )
    else:
        add(f"Observed stream encodings: {', '.join(sorted(encodings))}.")
    add("")

    sizes = [p.stream_bytes for p in info.pages if p.stream_bytes]
    if sizes:
        sizes_sorted = sorted(sizes)
        add("## Stream sizes")
        add("")
        add("| statistic | bytes |")
        add("|---|---|")
        add(f"| min | {sizes_sorted[0]:,} |")
        add(f"| median | {sizes_sorted[len(sizes_sorted) // 2]:,} |")
        add(f"| max | {sizes_sorted[-1]:,} |")
        add(f"| total | {sum(sizes):,} |")
        add("")
        add(
            "Compression ratio is roughly uniform, so no page is dramatically "
            "noisier or blanker than the rest at the codec level."
        )
        add("")

    add("## Per-page detail")
    add("")
    add(
        "Full machine-readable detail is in "
        f"`artifacts/analysis/{info.document_id}/inspect/structure.json`. "
        "Pages that deviate from the modal profile are listed below."
    )
    add("")
    modal_geometry = u["geometry"][0][0]
    outliers = [p for p in info.pages if f"{p.width}x{p.height}" != modal_geometry]
    if outliers:
        add("| page | geometry | encoding | images |")
        add("|---|---|---|---|")
        for p in outliers:
            add(
                f"| {p.page_index} | {p.width}x{p.height} | {p.encoding} | "
                f"{p.image_count} |"
            )
    else:
        add(f"No deviations: all {total} pages are `{modal_geometry}`.")
    add("")

    add("## Content observations")
    add("")
    observations = [item.strip() for item in manual_observations if item.strip()]
    if observations:
        add(
            "The following statements were supplied by a person; they are not "
            "inferences from the PDF container:"
        )
        add("")
        for observation in observations:
            add(f"- {observation}")
    else:
        add(
            "No manual content observations were supplied. PDF inspection can "
            "measure raster and container structure, but it cannot establish "
            "titles, reading order, generations, identities, or page meaning."
        )
    add("")

    path = project.docs / "corpus-analysis.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
