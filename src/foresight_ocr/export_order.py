"""Document-aware ordering shared by every exported record format.

Generation labels are semantic document data.  Sorting their glyphs as text
puts ``富`` before ``庶`` on SQLite/Python code-point order, even though this
volume charts ``庶`` first.  The active document profile is the authority; any
unexpected label is kept, deterministically, after the configured generations.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from foresight_ocr.context import get_profile
from foresight_ocr.validation.numerals import parse_entry_id


def generation_sort_key(
    label: str | None,
    *within_generation: Any,
    labels: Iterable[str] | None = None,
) -> tuple[Any, ...]:
    """Sort by the charted generation, then caller-supplied stable position.

    ``within_generation`` deliberately belongs to the caller.  A transcription
    export retains source page/column order so unreadable or misread identifiers
    stay traceable, while a reconstructed-person export can use its parsed
    numeric identifier.
    """
    ordered = tuple(
        dict.fromkeys(labels if labels is not None else get_profile().band_labels)
    )
    rank = {generation: index for index, generation in enumerate(ordered)}
    text = (label or "").strip()
    known = text in rank
    return (
        rank.get(text, len(rank)),
        "" if known else text,
        *within_generation,
    )


def entry_sort_key(
    label: str | None,
    own_id: str | None,
    *source_position: Any,
    labels: Iterable[str] | None = None,
) -> tuple[Any, ...]:
    """Order one entry numerically, with unparseable ids stably at the end.

    This uses the project's existing numeral parser and never repairs an id from
    neighbouring rows.  A missing, rejected, or differently labelled identifier
    uses its page/column position only as a deterministic fallback after numbered
    entries in the same generation.
    """
    parsed = parse_entry_id(own_id or "")
    value = parsed.value if parsed.ok and parsed.band in (None, label) else None
    return generation_sort_key(
        label,
        value is None,
        value if value is not None else 0,
        *source_position,
        labels=labels,
    )
