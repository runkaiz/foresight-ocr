"""Cache identity for crops and recognizer answers.

The one place that knows these formulas. Everything the product needs from
invalidation falls out of three properties of the keys below:

    * `crop_key` depends only on the pixels a recognizer would see, so moving
      one region invalidates one crop and re-segmenting an unchanged page
      invalidates nothing.
    * `cache_key` folds in the model, so a new recognizer version produces new
      answers *beside* the old ones rather than replacing them.
    * Neither depends on the transcription or the reading order, so correcting
      text and reordering entries cost no model time at all.

Before this existed, `segment` deleted regions and `ocr_candidates` cascaded
away with them: every re-segment destroyed the OCR for those pages, and there
was no way to notice that the geometry had not actually changed.
"""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Any, Iterable, Sequence

from ..provenance import config_hash

#: Bump when a variant's pixels change for the same input, so that cached crops
#: cut under the old definition stop matching. `imaging/variants.py` builds
#: these in memory and never persists them, so there is nothing else to compare.
VARIANT_VERSION = "1"

#: Bump when the cropping itself changes — padding rules, rounding, resampling.
CUTTER_VERSION = "1"


def crop_key(
    document_id: str,
    page_index: int,
    pixel_bbox: Sequence[int],
    variant: str,
    page_checksum: str,
) -> str:
    """Content address of a rendered crop.

    Keyed on the *integer* box actually cut rather than the float region bbox:
    crops are sliced with `int(y0):int(y1)`, so two boxes differing by a
    hundredth of a pixel produce identical pixels and must produce identical
    keys. For the same reason the context name (`tight`/`medium`/`full`) is
    absent — padding is `pitch * pad_frac` with a per-page pitch, so the name
    says nothing reliable about the pixels while the box says everything.

    `page_checksum` is the normalized page the crop was cut from. Without it a
    re-warped page would silently reuse crops of the old geometry.
    """
    x0, y0, x1, y1 = (int(v) for v in pixel_bbox)
    raw = "|".join(
        [
            document_id,
            str(page_index),
            f"{x0}",
            f"{y0}",
            f"{x1}",
            f"{y1}",
            variant,
            VARIANT_VERSION,
            page_checksum or "",
            CUTTER_VERSION,
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def model_key(backend: str, model_version: str, options: dict[str, Any] | None = None) -> str:
    """Identity of a recognizer *configuration*.

    The options hash is what makes prompt, image scale and batching structural.
    They were previously distinguished only by the free-text `tag` and only by
    convention — the stored runs literally use `scale-0.6`, `scale-0.4` and
    `batched` as tags to do this job by hand.
    """
    return f"{backend}:{model_version}:{config_hash(options or {})[:8]}"


def cache_key(crop: str, model: str, tag: str = "") -> str:
    """Identity of one answer: these pixels, read by this configuration."""
    return hashlib.sha256(f"{crop}|{model}|{tag or ''}".encode("utf-8")).hexdigest()[:24]


def pending(
    conn: sqlite3.Connection,
    document_id: str,
    *,
    variant: str,
    context: str,
    model: str,
    tag: str = "",
    pages: Iterable[int] | None = None,
    roles: Sequence[str] = ("entry",),
) -> list[sqlite3.Row]:
    """Crops with no answer yet from this configuration.

    Replaces `harness.discover_crops`, which globbed the crop directory and
    parsed page/band/entry back out of the filename. Reading the database
    instead makes OCR incremental for free — the same query answers "OCR this
    page lazily", "re-OCR the one region I just moved", and "run the new model
    over the book, keeping everything else" — and it stops orphaned PNGs from a
    previous segmentation being picked up as live work.
    """
    where = [
        "r.document_id = ?",
        "r.deleted_at IS NULL",
        "rc.context = ?",
        "rc.variant = ?",
    ]
    params: list[Any] = [document_id, context, variant]

    if roles:
        where.append(f"r.role IN ({','.join('?' * len(roles))})")
        params.extend(roles)

    page_list = list(pages) if pages is not None else None
    if page_list is not None:
        if not page_list:
            return []
        where.append(f"r.page_index IN ({','.join('?' * len(page_list))})")
        params.extend(page_list)

    rows = conn.execute(
        f"""
        SELECT r.region_uid, r.page_index, r.band_label, r.reading_order,
               rc.crop_key, rc.path, rc.context, rc.variant
          FROM regions r
          JOIN region_crops rc ON rc.region_id = r.id
         WHERE {' AND '.join(where)}
         ORDER BY r.page_index, r.band_ordinal, r.reading_order
        """,
        params,
    ).fetchall()

    have = {
        row[0]
        for row in conn.execute(
            "SELECT cache_key FROM ocr_candidates WHERE cache_key IS NOT NULL"
        )
    }
    return [r for r in rows if cache_key(r["crop_key"], model, tag) not in have]
