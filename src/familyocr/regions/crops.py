"""Cutting the pixels a region names.

`segment` cuts crops for a whole document; this cuts one, for a region whose box
a person has just changed. The two must agree exactly, because a crop the editor
produced and a crop the pipeline produced have to be the same file when the
geometry is the same — otherwise every re-segment would look like a change.

So the slice is `page[int(y0):int(y1), int(x0):int(x1)]`, as it is there, and
the resulting file is named by `crop_key` rather than by position. The old names
encoded the entry's index, which meant inserting a column renamed its neighbours'
crops; a content address cannot go stale, and a stale one on disk is inert
rather than misleading.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import cv2

from ..imaging.variants import VARIANTS, build_variant
from ..ocr.cache import crop_key
from ..project import Project
from ..provenance import sha256_bytes
from .model import Region


@dataclass(frozen=True)
class Crop:
    region_uid: str
    crop_key: str
    path: Path
    pixel_bbox: list[int]
    variant: str
    context: str
    reused: bool


class CropUnavailable(RuntimeError):
    """The pixels cannot be produced — no normalized page, or an empty box."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalized_page(
    conn: sqlite3.Connection, project: Project, document_id: str, page_index: int
) -> tuple[Path, str]:
    """The normalized page for a region, and the checksum of its bytes.

    Registered lazily. `normalize` writes these files without recording them, so
    a page first touched by the editor has no asset row yet; without the
    checksum a crop cannot be addressed, because "the same box on a re-warped
    page" is not the same pixels.
    """
    row = conn.execute(
        "SELECT path, checksum FROM page_assets "
        "WHERE document_id = ? AND page_index = ? AND role = 'normalized'",
        (document_id, page_index),
    ).fetchone()
    if row and Path(row["path"]).exists():
        return Path(row["path"]), row["checksum"]

    path = project.pages_dir(document_id, "normalized") / f"p{page_index:04d}.png"
    if not path.exists():
        raise CropUnavailable(
            f"{document_id} p{page_index} has not been normalized; run "
            "`familyocr normalize` before editing its regions"
        )
    checksum = sha256_bytes(path.read_bytes())
    conn.execute(
        "INSERT INTO page_assets (document_id, page_index, role, path, checksum, "
        "created_at) VALUES (?,?,'normalized',?,?,?) "
        "ON CONFLICT(document_id, page_index, role) DO UPDATE SET "
        "path = excluded.path, checksum = excluded.checksum",
        (document_id, page_index, str(path), checksum, _now()),
    )
    return path, checksum


def ensure_crop(
    conn: sqlite3.Connection,
    project: Project,
    region: Region,
    *,
    variant: str = "maxrgb",
    context: str = "tight",
) -> Crop:
    """The crop for this region's current box, cutting it only if it is new.

    A region that has not moved addresses a crop that already exists, so
    re-segmenting an unchanged page writes no files and a repeated edit that
    lands back on the original box costs nothing.
    """
    if variant != "original" and variant not in VARIANTS:
        raise CropUnavailable(f"unknown variant {variant!r}")

    page_path, page_checksum = normalized_page(
        conn, project, region.document_id, region.page_index
    )
    x0, y0, x1, y1 = region.bbox
    pixel_bbox = [int(x0), int(y0), int(x1), int(y1)]
    if pixel_bbox[2] <= pixel_bbox[0] or pixel_bbox[3] <= pixel_bbox[1]:
        raise CropUnavailable(f"region {region.region_uid} has an empty box")

    key = crop_key(
        region.document_id, region.page_index, pixel_bbox, variant, page_checksum
    )
    existing = conn.execute(
        "SELECT path FROM region_crops WHERE crop_key = ?", (key,)
    ).fetchone()
    if existing and Path(existing["path"]).exists():
        return Crop(
            region.region_uid, key, Path(existing["path"]), pixel_bbox, variant,
            context, reused=True,
        )

    page = cv2.imread(str(page_path), cv2.IMREAD_COLOR)
    if page is None:
        # The failure mode is a truncated page left by a killed run, and it
        # otherwise surfaces as an attribute error on None far from the cause.
        raise CropUnavailable(
            f"cannot read {page_path} — it is missing or truncated. "
            "Re-run `familyocr normalize` for this page."
        )
    source = page if variant == "original" else build_variant(page, variant)

    px0, py0, px1, py1 = pixel_bbox
    height, width = source.shape[:2]
    crop = source[max(py0, 0):min(py1, height), max(px0, 0):min(px1, width)]
    if crop.size == 0:
        raise CropUnavailable(f"region {region.region_uid} falls outside the page")

    out = project.crops_dir(region.document_id) / variant / f"{key}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(out), crop):
        raise CropUnavailable(f"cannot write {out}")

    conn.execute(
        "INSERT INTO region_crops (region_id, geometry_hash, context, pad_frac, "
        "variant, pixel_bbox_json, crop_key, path, checksum, created_at) "
        "VALUES (?,?,?,0.0,?,?,?,?,?,?) ON CONFLICT(crop_key) DO NOTHING",
        (
            region.id,
            region.geometry_hash,
            context,
            variant,
            json.dumps(pixel_bbox),
            key,
            str(out),
            sha256_bytes(out.read_bytes()),
            _now(),
        ),
    )
    return Crop(region.region_uid, key, out, pixel_bbox, variant, context, reused=False)
