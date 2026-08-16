"""PDF inspection and original-scan preservation.

This corpus stores exactly one embedded JPEG2000 raster per page. Re-rasterizing
the PDF page would resample data we already have losslessly, so the archival copy
is the raw embedded stream, byte-for-byte. Decoded PNGs are derivatives and are
regenerable; the .jp2 files are the ones that must never change.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterator

import pikepdf
from PIL import Image

from familyocr.provenance import sha256_bytes, sha256_file

# Pillow refuses very large images by default as a decompression-bomb guard.
# 2424x3744 is well inside sanity, but decoded page counts add up; set an
# explicit generous ceiling rather than disabling the check entirely.
Image.MAX_IMAGE_PIXELS = 200_000_000

FILTER_ENCODING = {
    "/JPXDecode": ("jpx", ".jp2"),
    "/DCTDecode": ("jpeg", ".jpg"),
    "/CCITTFaxDecode": ("ccitt", ".tif"),
    "/JBIG2Decode": ("jbig2", ".jbig2"),
    "/FlateDecode": ("flate", ".png"),
}


@dataclass
class PageImageInfo:
    page_index: int          # 1-based, matches human page numbering
    width: int
    height: int
    colorspace: str
    encoding: str
    bits_per_component: int
    stream_bytes: int
    x_ppi: float | None
    y_ppi: float | None
    image_count: int         # >1 means the page is not a single simple scan


@dataclass
class DocumentInfo:
    document_id: str
    source_path: str
    checksum: str
    page_count: int
    pdf_version: str
    creator: str | None
    producer: str | None
    pages: list[PageImageInfo]

    def uniformity(self) -> dict[str, list[tuple[str, int]]]:
        """How consistent the corpus is. Multiple entries per key = mixed corpus."""
        keys = {
            "geometry": lambda p: f"{p.width}x{p.height}",
            "colorspace": lambda p: p.colorspace,
            "encoding": lambda p: p.encoding,
            "ppi": lambda p: f"{p.x_ppi:.0f}" if p.x_ppi else "unknown",
            "images_per_page": lambda p: str(p.image_count),
        }
        return {
            name: Counter(fn(p) for p in self.pages).most_common()
            for name, fn in keys.items()
        }

    def to_json(self) -> str:
        d = asdict(self)
        return json.dumps(d, ensure_ascii=False, indent=2)


def _colorspace_name(obj: pikepdf.Object) -> str:
    cs = obj.get("/ColorSpace")
    if cs is None:
        # JPEG2000 carries colorspace and bit depth inside the codestream, so a
        # missing /ColorSpace entry is expected rather than a defect.
        if "/JPXDecode" in _filters(obj):
            return "jpx-internal"
        return "unknown"
    if isinstance(cs, pikepdf.Name):
        return str(cs).lstrip("/").replace("Device", "").lower()
    # Indexed / ICCBased / arrays: report the family, detail is in raw_json.
    try:
        return str(cs[0]).lstrip("/").lower()
    except (TypeError, IndexError):
        return "complex"


def _filters(obj: pikepdf.Object) -> list[str]:
    filt = obj.get("/Filter")
    if filt is None:
        return []
    if isinstance(filt, pikepdf.Name):
        return [str(filt)]
    return [str(f) for f in filt]


def _iter_page_images(page: pikepdf.Page) -> Iterator[tuple[str, pikepdf.Object]]:
    resources = page.obj.get("/Resources")
    if resources is None:
        return
    xobjects = resources.get("/XObject")
    if xobjects is None:
        return
    for name, xobj in xobjects.items():
        if xobj.get("/Subtype") == pikepdf.Name("/Image"):
            yield str(name), xobj


def inspect_pdf(path: Path, document_id: str | None = None) -> DocumentInfo:
    """Read structure without decoding pixels. Cheap enough to run on every page."""
    doc_id = document_id or path.stem
    pages: list[PageImageInfo] = []

    with pikepdf.open(path) as pdf:
        meta = pdf.docinfo if pdf.docinfo is not None else {}
        for idx, page in enumerate(pdf.pages, start=1):
            images = list(_iter_page_images(page))
            if not images:
                pages.append(
                    PageImageInfo(idx, 0, 0, "none", "none", 0, 0, None, None, 0)
                )
                continue
            # Largest image wins: page decoration (if any) is never the scan.
            _, obj = max(images, key=lambda kv: int(kv[1].get("/Width", 0)))
            width = int(obj.get("/Width", 0))
            height = int(obj.get("/Height", 0))
            filters = _filters(obj)
            encoding = "raw"
            for f in filters:
                if f in FILTER_ENCODING:
                    encoding = FILTER_ENCODING[f][0]
                    break

            mbox = page.mediabox
            pts_w = float(mbox[2]) - float(mbox[0])
            pts_h = float(mbox[3]) - float(mbox[1])
            x_ppi = width / pts_w * 72.0 if pts_w else None
            y_ppi = height / pts_h * 72.0 if pts_h else None

            pages.append(
                PageImageInfo(
                    page_index=idx,
                    width=width,
                    height=height,
                    colorspace=_colorspace_name(obj),
                    encoding=encoding,
                    bits_per_component=int(obj.get("/BitsPerComponent", 0)),
                    stream_bytes=len(obj.read_raw_bytes()),
                    x_ppi=x_ppi,
                    y_ppi=y_ppi,
                    image_count=len(images),
                )
            )

        return DocumentInfo(
            document_id=doc_id,
            source_path=str(path),
            checksum=sha256_file(path),
            page_count=len(pdf.pages),
            pdf_version=str(pdf.pdf_version),
            creator=str(meta.get("/Creator", "")) or None,
            producer=str(meta.get("/Producer", "")) or None,
            pages=pages,
        )


@dataclass
class ExtractedPage:
    page_index: int
    original_path: Path
    original_checksum: str
    decoded_path: Path
    decoded_checksum: str
    width: int
    height: int


def extract_originals(
    path: Path,
    raw_dir: Path,
    decoded_dir: Path,
    pages: list[int] | None = None,
    force: bool = False,
) -> list[ExtractedPage]:
    """Write the untouched embedded stream plus one lossless PNG derivative.

    Idempotent: an existing original with a matching checksum is left alone, so
    reruns cannot silently rewrite archival data.
    """
    raw_dir.mkdir(parents=True, exist_ok=True)
    decoded_dir.mkdir(parents=True, exist_ok=True)
    results: list[ExtractedPage] = []

    with pikepdf.open(path) as pdf:
        for idx, page in enumerate(pdf.pages, start=1):
            if pages and idx not in pages:
                continue
            images = list(_iter_page_images(page))
            if not images:
                continue
            _, obj = max(images, key=lambda kv: int(kv[1].get("/Width", 0)))
            filters = _filters(obj)
            ext = ".bin"
            for f in filters:
                if f in FILTER_ENCODING:
                    ext = FILTER_ENCODING[f][1]
                    break

            raw_path = raw_dir / f"p{idx:04d}{ext}"
            if ext in (".jp2", ".jpg"):
                # Single image-codec filter: the raw stream IS a valid image file.
                data = obj.read_raw_bytes()
                checksum = sha256_bytes(data)
                if force or not raw_path.exists():
                    raw_path.write_bytes(data)
                elif sha256_file(raw_path) != checksum:
                    raise RuntimeError(
                        f"{raw_path} exists with a different checksum; refusing to "
                        f"overwrite an original. Pass force=True to replace it."
                    )
            else:
                # Anything else has to go through pikepdf's normalization path.
                pdfimage = pikepdf.PdfImage(obj)
                if force or not raw_path.exists():
                    written = pdfimage.extract_to(fileprefix=str(raw_dir / f"p{idx:04d}"))
                    raw_path = Path(written)
                checksum = sha256_file(raw_path)

            decoded_path = decoded_dir / f"p{idx:04d}.png"
            if force or not decoded_path.exists():
                with Image.open(raw_path) as im:
                    im.load()
                    im.save(decoded_path, format="PNG", optimize=False, compress_level=1)

            with Image.open(decoded_path) as im:
                w, h = im.size

            results.append(
                ExtractedPage(
                    page_index=idx,
                    original_path=raw_path,
                    original_checksum=checksum,
                    decoded_path=decoded_path,
                    decoded_checksum=sha256_file(decoded_path),
                    width=w,
                    height=h,
                )
            )
    return results
