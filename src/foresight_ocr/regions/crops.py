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
import numpy as np
import yaml
from PIL import Image

from ..imaging.io import read_image, write_image
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


def _adopt_reusable_crop(
    conn: sqlite3.Connection, crop_key_value: str, region: Region
) -> None:
    """Re-aim cached pixels when their former region has moved or retired.

    Region identity describes a logical column, while a crop key describes
    immutable pixels. After layout reconciliation a moved region can still own
    the crop for its old box, and a newly created region can name those exact
    pixels. The unique crop/cache keys then reuse the artifact but would leave
    both the crop and its OCR answers attached to the wrong region.
    """
    row = conn.execute(
        """SELECT rc.id, rc.region_id, rc.geometry_hash,
                  r.geometry_hash AS owner_geometry_hash, r.deleted_at
           FROM region_crops rc
           LEFT JOIN regions r ON r.id = rc.region_id
           WHERE rc.crop_key = ?""",
        (crop_key_value,),
    ).fetchone()
    if row is None or row["region_id"] == region.id:
        return
    owner_no_longer_names_pixels = (
        row["deleted_at"] is not None
        or row["owner_geometry_hash"] != row["geometry_hash"]
    )
    if not owner_no_longer_names_pixels:
        return
    conn.execute(
        "UPDATE region_crops SET region_id = ? WHERE id = ?",
        (region.id, row["id"]),
    )
    conn.execute(
        "UPDATE ocr_candidates SET region_id = ? WHERE crop_key = ? AND region_id = ?",
        (region.id, crop_key_value, row["region_id"]),
    )


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
            "`foresight-ocr normalize` before editing its regions"
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
    variant: str = "watermark",
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

    # A chart continues horizontally across scans. After the comb is aligned
    # to complete entries, the first region on a page can be only the own-id
    # half at the physical right edge; its parent/order half is the unassigned
    # strip at the physical left edge of the preceding scan. Address both pages
    # in the crop key so normal cache invalidation still follows the pixels.
    # The PNG header supplies the normalized width without decoding the whole
    # 535-ppi page.  The content key and any reusable crop can therefore be
    # resolved before paying for page decoding and variant construction.
    try:
        with Image.open(page_path) as probe:
            page_width = probe.width
    except OSError as exc:
        raise CropUnavailable(
            f"cannot read {page_path} — it is missing or truncated. "
            "Re-run `foresight-ocr normalize` for this page."
        ) from exc

    stitch = _previous_page_fragment(conn, project, region, page_width)
    rendered_bbox = list(pixel_bbox)
    checksum_material = page_checksum
    if stitch is not None:
        _previous_path, previous_checksum, previous_bbox, stitched_width = stitch
        region_width = pixel_bbox[2] - pixel_bbox[0]
        if region_width > stitched_width * 0.70:
            rendered_bbox[0] = pixel_bbox[2] - max(region_width // 2, 1)
        checksum_material = (
            f"{page_checksum}|cross-page-v5|{previous_checksum}|"
            + ",".join(str(v) for v in previous_bbox)
            + f"|width={stitched_width}"
        )

    key = crop_key(
        region.document_id, region.page_index, pixel_bbox, variant, checksum_material
    )
    existing = conn.execute(
        "SELECT path, pixel_bbox_json FROM region_crops WHERE crop_key = ?", (key,)
    ).fetchone()
    if existing and Path(existing["path"]).exists():
        _adopt_reusable_crop(conn, key, region)
        try:
            stored_bbox = [int(v) for v in json.loads(existing["pixel_bbox_json"])]
        except (TypeError, ValueError):
            stored_bbox = rendered_bbox
        return Crop(
            region.region_uid,
            key,
            Path(existing["path"]),
            stored_bbox,
            variant,
            context,
            reused=True,
        )

    out = project.crops_dir(region.document_id) / variant / f"{key}.png"
    if out.exists() and read_image(out, cv2.IMREAD_UNCHANGED) is not None:
        # A killed document job can leave a complete content-addressed crop on
        # disk while its surrounding SQLite transaction rolls back. Recover
        # that artifact instead of rebuilding the same page once per region.
        conn.execute(
            "INSERT INTO region_crops (region_id, geometry_hash, context, pad_frac, "
            "variant, pixel_bbox_json, crop_key, path, checksum, created_at) "
            "VALUES (?,?,?,0.0,?,?,?,?,?,?) ON CONFLICT(crop_key) DO NOTHING",
            (
                region.id,
                region.geometry_hash,
                context,
                variant,
                json.dumps(rendered_bbox),
                key,
                str(out),
                sha256_bytes(out.read_bytes()),
                _now(),
            ),
        )
        _adopt_reusable_crop(conn, key, region)
        return Crop(
            region.region_uid,
            key,
            out,
            rendered_bbox,
            variant,
            context,
            reused=True,
        )

    page = read_image(page_path, cv2.IMREAD_COLOR)
    if page is None:
        # The failure mode is a truncated page left by a killed run, and it
        # otherwise surfaces as an attribute error on None far from the cause.
        raise CropUnavailable(
            f"cannot read {page_path} — it is missing or truncated. "
            "Re-run `foresight-ocr normalize` for this page."
        )
    source = page if variant == "original" else build_variant(page, variant)

    px0, py0, px1, py1 = pixel_bbox
    height, width = source.shape[:2]
    crop = source[max(py0, 0) : min(py1, height), max(px0, 0) : min(px1, width)]
    if crop.size == 0:
        raise CropUnavailable(f"region {region.region_uid} falls outside the page")

    if stitch is not None:
        (
            previous_path,
            _previous_checksum,
            (sx0, sy0, sx1, sy1),
            stitched_width,
        ) = stitch
        previous_page = read_image(previous_path, cv2.IMREAD_COLOR)
        if previous_page is None:
            raise CropUnavailable(f"cannot read preceding page {previous_path}")
        previous_source = (
            previous_page
            if variant == "original"
            else build_variant(previous_page, variant)
        )
        ph, pw = previous_source.shape[:2]
        previous_crop = previous_source[
            max(sy0, 0) : min(sy1, ph), max(sx0, 0) : min(sx1, pw)
        ]
        if previous_crop.size:
            # An ordinary full-width edge box contains two printed sub-columns.
            # Only its right one continues the preceding page; the left one is
            # the next piece on the current page (page 4's 九三／繼子 in the
            # observed failure). Narrow edge boxes are already partial, as on
            # page 3, and must remain intact.
            current_width = rendered_bbox[2] - rendered_bbox[0]
            if crop.shape[1] > current_width:
                crop = crop[:, -current_width:]
            previous_crop, crop = _same_height(previous_crop, crop)
            # The continuation is previewed in reading sequence: the current
            # page's first entry on the left, followed by the preceding page's
            # final fragment on the right.
            crop = np.concatenate([crop, previous_crop], axis=1)

    out.parent.mkdir(parents=True, exist_ok=True)
    if not write_image(out, crop):
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
            json.dumps(rendered_bbox),
            key,
            str(out),
            sha256_bytes(out.read_bytes()),
            _now(),
        ),
    )
    _adopt_reusable_crop(conn, key, region)
    return Crop(
        region.region_uid, key, out, rendered_bbox, variant, context, reused=False
    )


def ensure_cross_page_previews(
    conn: sqlite3.Connection,
    project: Project,
    document_id: str,
    page_index: int,
    page_width: int,
    *,
    variant: str = "watermark",
) -> list[Crop]:
    """Materialize current-version stitched crops before review displays them.

    Page review normally reads existing crop rows and does not run OCR.  A
    stitch-order change therefore needs this small derived-artifact refresh or
    the browser will keep selecting the latest crop made by the old stitcher.
    """
    from .store import for_page

    out: list[Crop] = []
    for region in for_page(conn, document_id, page_index):
        if (
            region.state == "rejected"
            or _previous_page_fragment(conn, project, region, page_width) is None
        ):
            continue
        try:
            out.append(ensure_crop(conn, project, region, variant=variant))
        except CropUnavailable:
            # Review can still show an older crop when one source page is
            # temporarily unavailable; page loading should not fail with it.
            continue
    return out


def _previous_page_fragment(
    conn: sqlite3.Connection,
    project: Project,
    region: Region,
    page_width: int,
) -> tuple[Path, str, tuple[int, int, int, int], int] | None:
    """The left-edge continuation belonging to a right-edge entry, if any."""
    if region.role != "entry" or region.reading_order != 0 or region.page_index <= 1:
        return None

    # A snapped comb ends at the page's last detected gutter, not necessarily
    # at the canonical image boundary.  In this book that leaves a 30--84 px
    # paper margin on almost every ordinary page; requiring x1 == page_width
    # therefore happened to stitch page 3 and little else.  A rightmost box is
    # still an edge fragment when that margin is less than half of the box's
    # own width.  The much larger gap on a structured/title page remains safely
    # outside the allowance.
    x0, _y0, x1, _y1 = (float(v) for v in region.bbox)
    region_width = max(x1 - x0, 0.0)
    edge_gap = max(float(page_width) - x1, 0.0)
    if region_width <= 0 or edge_gap > max(1.0, region_width * 0.5):
        return None

    rows = conn.execute(
        "SELECT bbox_json FROM regions WHERE document_id = ? AND page_index = ? "
        "AND band_ordinal IS ? AND role = 'entry' AND deleted_at IS NULL "
        "AND state != 'rejected'",
        (region.document_id, region.page_index - 1, region.band_ordinal),
    ).fetchall()
    boxes: list[list[float]] = []
    for row in rows:
        try:
            box = [float(v) for v in json.loads(row["bbox_json"])]
        except (TypeError, ValueError):
            continue
        boxes.append(box)
    if not boxes:
        return None

    # The leftmost complete region begins after the fragment that could not be
    # assigned on the preceding scan. Its own vertical band is authoritative;
    # page-to-page band edges can differ by several pixels after normalization.
    # If it already reaches x=0 there is no unassigned fragment.  Do not skip
    # that box and mistake the whole leftmost entry for continuation material.
    leftmost = min(boxes, key=lambda box: box[0])
    cutoff = int(leftmost[0])
    y0, y1 = int(leftmost[1]), int(leftmost[3])
    if cutoff <= 0 or y1 <= y0:
        return None
    path, checksum = normalized_page(
        conn, project, region.document_id, region.page_index - 1
    )
    return (
        path,
        checksum,
        (0, y0, cutoff, y1),
        _stitched_entry_width(project, region.document_id, boxes),
    )


def _stitched_entry_width(
    project: Project, document_id: str, previous_boxes: list[list[float]]
) -> int:
    """Expected width of one complete entry across a page seam."""
    template_path = project.configs / f"template_{document_id}.yaml"
    if template_path.exists():
        try:
            template = yaml.safe_load(template_path.read_text(encoding="utf-8")) or {}
            pitch = float(template.get("column_pitch", 0.0))
            if pitch > 0:
                return max(int(round(pitch)), 1)
        except (OSError, TypeError, ValueError, yaml.YAMLError):
            pass

    # Small isolated projects and tests may not have learned a template yet.
    # The median resists the partial boxes at either physical page edge.
    widths = [box[2] - box[0] for box in previous_boxes if box[2] > box[0]]
    return max(int(round(float(np.median(widths)))) if widths else 1, 1)


def _same_height(a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Pad two page fragments with paper white before horizontal stitching."""
    target = max(a.shape[0], b.shape[0])

    def pad(image: np.ndarray) -> np.ndarray:
        missing = target - image.shape[0]
        if missing <= 0:
            return image
        top = missing // 2
        bottom = missing - top
        value = 255 if image.ndim == 2 else (255, 255, 255)
        return cv2.copyMakeBorder(
            image, top, bottom, 0, 0, cv2.BORDER_CONSTANT, value=value
        )

    return pad(a), pad(b)
