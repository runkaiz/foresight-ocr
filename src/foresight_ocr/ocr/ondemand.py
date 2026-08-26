"""Recognizing individual regions.

`benchmark` reads a whole book: it globs every crop on disk and hands the lot to
a recognizer. That is the right shape for measuring models against each other
and the wrong shape for an editor, where the unit of work is one region a person
just moved.

The difference is not the loop — it is what decides the work. Here the answer
already on record is compared against the address of the pixels as they now
stand, so a region that has not changed is skipped whatever ran over it before,
and a region that has changed is read again even if its neighbours have not.
Nothing is deleted to make room: a new answer is stored at a new address, and
the old one remains attached to the pixels it actually described.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

from ..project import Project
from ..provenance import ProcessingRun
from ..regions import store
from ..regions.crops import Crop, CropUnavailable, ensure_crop
from ..regions.model import Region
from .base import OCRRequest, get_backend
from .cache import cache_key, model_key
from .watermarks import filter_watermark_text

#: What an interactive re-read is recorded under, so it is distinguishable from
#: a benchmark sweep without being confused with one either.
INTERACTIVE_TAG = "interactive"
ProgressCallback = Callable[[dict[str, Any]], None]


@dataclass
class RegionAnswer:
    region_uid: str
    transcription: str | None
    cache_key: str
    reused: bool
    error: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def recognize_regions(
    conn: sqlite3.Connection,
    project: Project,
    document_id: str,
    region_uids: Sequence[str],
    *,
    backend: str = "paddleocr_vl",
    variant: str = "watermark",
    context: str = "tight",
    tag: str = INTERACTIVE_TAG,
    options: dict[str, Any] | None = None,
    force: bool = False,
    on_progress: ProgressCallback | None = None,
) -> list[RegionAnswer]:
    """Read these regions, skipping any whose answer is already current.

    Answers come back in the order asked for, including the failures, so a
    caller can pair them with its own list without matching on uid.

    `force` re-reads pixels that already have an answer. It is also how a new
    model version is taken up: which weights a backend loads is not knowable
    until it has run (see `last_seen_version`), so upgrading is a deliberate
    reprocessing pass rather than something that happens by surprise in the
    middle of an edit. The new answers land beside the old ones, never on them.
    """
    regions = store.get_many(conn, region_uids)
    if not regions:
        return []

    # Which weights a backend actually loads is only known once it has run —
    # PaddleOCR-VL resolves a fallback if the requested repository fails, and
    # reporting the requested one would make the cache claim an answer came from
    # a model that never ran. So the skip decision uses the version last seen
    # from this backend, and what is stored uses the version that just ran. They
    # differ exactly once per upgrade, which costs one re-read and is the point.
    model = model_key(backend, last_seen_version(conn, backend), options or {})
    answers: list[RegionAnswer] = []
    todo: list[tuple[Region, Crop]] = []

    for index, region in enumerate(regions, start=1):
        try:
            crop = ensure_crop(conn, project, region, variant=variant, context=context)
        except CropUnavailable as exc:
            answers.append(RegionAnswer(region.region_uid, None, "", False, str(exc)))
            if on_progress:
                on_progress(
                    {
                        "stage": "preparing",
                        "completed": index,
                        "total": len(regions),
                        "region_uid": region.region_uid,
                    }
                )
            continue

        key = cache_key(crop.crop_key, model, tag)
        if not force:
            row = conn.execute(
                "SELECT transcription FROM ocr_candidates WHERE cache_key = ?", (key,)
            ).fetchone()
            if row is not None:
                answers.append(
                    RegionAnswer(
                        region.region_uid,
                        filter_watermark_text(row["transcription"]).transcription,
                        key,
                        True,
                    )
                )
                if on_progress:
                    on_progress(
                        {
                            "stage": "preparing",
                            "completed": index,
                            "total": len(regions),
                            "region_uid": region.region_uid,
                        }
                    )
                continue
        todo.append((region, crop))
        if on_progress:
            on_progress(
                {
                    "stage": "preparing",
                    "completed": index,
                    "total": len(regions),
                    "region_uid": region.region_uid,
                }
            )

    if todo:
        # Nothing is started when there is nothing to read: a recognizer that is
        # missing or broken must not stop a reviewer from opening a page whose
        # answers are already on record.
        engine = get_backend(backend, **(options or {}))
        available, why = engine.available()
        if not available:
            raise RuntimeError(f"{backend} is unavailable: {why}")
        answers.extend(
            _read(
                conn,
                document_id,
                engine,
                backend,
                options or {},
                tag,
                variant,
                context,
                todo,
                on_progress=on_progress,
            )
        )

    order = {uid: i for i, uid in enumerate(region_uids)}
    return sorted(answers, key=lambda a: order.get(a.region_uid, len(order)))


def last_seen_version(conn: sqlite3.Connection, backend: str) -> str:
    """The newest model version this backend has produced answers with.

    Used only to decide whether an answer already on record is current. It
    cannot notice that a *newer* model is installed, because no backend here can
    say which weights it will load without loading them — PaddleOCR-VL resolves
    a fallback repository at load time. Taking up a new version is therefore an
    explicit pass (`force=True`), which is the right shape for it anyway: it is
    a decision about the document, not a side effect of dragging a box.
    """
    row = conn.execute(
        "SELECT version FROM models WHERE backend = ? ORDER BY rowid DESC LIMIT 1",
        (backend,),
    ).fetchone()
    return row["version"] if row else "unknown"


def _read(
    conn,
    document_id,
    engine,
    backend,
    options,
    tag,
    variant,
    context,
    todo,
    *,
    on_progress: ProgressCallback | None = None,
):
    """Run the recognizer over the crops that need it, and store what it says."""
    if on_progress:
        on_progress({"stage": "recognizing", "completed": 0, "total": len(todo)})
    requests = [
        OCRRequest(
            crop_id=crop.crop_key,  # the wire field; its value is the address
            path=crop.path,
            variant=variant,
            context=context,
        )
        for _, crop in todo
    ]
    results = []
    streamed_batches = hasattr(engine, "recognize_batches")
    if streamed_batches:
        completed = 0
        for batch in engine.recognize_batches(requests):
            results.extend(batch)
            completed = min(len(todo), completed + len(batch))
            if on_progress and completed:
                on_progress(
                    {
                        "stage": "recognizing",
                        "completed": completed,
                        "total": len(todo),
                        "region_uid": todo[completed - 1][0].region_uid,
                    }
                )
    else:
        results = engine.recognize(requests)
    by_key = {r.crop_id: r for r in results}

    # Now that it has run, the backend knows which weights it loaded.
    model = model_key(backend, engine.model_version, options)
    ocr_run_id = _open_run(conn, document_id, engine, backend, model, tag, variant)
    answers = []
    for index, (region, crop) in enumerate(todo, start=1):
        result = by_key.get(crop.crop_key)
        if result is None:
            answers.append(
                RegionAnswer(
                    region.region_uid, None, "", False, "recognizer returned no row"
                )
            )
            if on_progress and not streamed_batches:
                on_progress(
                    {
                        "stage": "recognizing",
                        "completed": index,
                        "total": len(todo),
                        "region_uid": region.region_uid,
                    }
                )
            continue

        key = cache_key(crop.crop_key, model, tag)
        conn.execute(
            """INSERT INTO ocr_candidates
                   (region_id, source_region_id, ocr_run_id, crop_key, model_key,
                    cache_key, transcription, confidence, raw_json, created_at)
               VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
               -- The uniqueness of cache_key is a partial index, so the
               -- conflict target has to repeat its predicate to match it.
               ON CONFLICT(cache_key) WHERE cache_key IS NOT NULL DO UPDATE SET
                   region_id      = excluded.region_id,
                   source_region_id = excluded.source_region_id,
                   transcription = excluded.transcription,
                   confidence    = excluded.confidence,
                   raw_json      = excluded.raw_json,
                   ocr_run_id    = excluded.ocr_run_id,
                   created_at    = excluded.created_at""",
            (
                region.id,
                ocr_run_id,
                crop.crop_key,
                model,
                key,
                result.transcription,
                result.confidence,
                json.dumps(
                    {
                        "raw": result.raw,
                        "error": result.error,
                        "latency_ms": result.latency_ms,
                    },
                    ensure_ascii=False,
                ),
                _now(),
            ),
        )
        answers.append(
            RegionAnswer(
                region.region_uid, result.transcription, key, False, result.error
            )
        )
        if on_progress and not streamed_batches:
            on_progress(
                {
                    "stage": "recognizing",
                    "completed": index,
                    "total": len(todo),
                    "region_uid": region.region_uid,
                }
            )
    return answers


def _open_run(conn, document_id, engine, backend, model, tag, variant) -> int:
    """Provenance for this read: which model, which settings, which commit."""
    model_id = f"{backend}:{engine.model_version}"
    conn.execute(
        "INSERT INTO models (id, name, version, backend) VALUES (?,?,?,?) "
        "ON CONFLICT(id) DO NOTHING",
        (model_id, backend, engine.model_version, backend),
    )
    run = ProcessingRun(
        stage="ocr",
        params={"backend": backend, "variant": variant, "tag": tag, "model_key": model},
        compute_backend="local",
    )
    row = run.as_row()
    processing_run_id = conn.execute(
        "INSERT INTO processing_runs (document_id, stage, params_json, params_hash, "
        "input_checksum, compute_backend, pipeline_version, git_commit, started_at, "
        "finished_at, status) VALUES (?,?,?,?,?,?,?,?,?,?,'completed')",
        (
            document_id,
            row["stage"],
            row["params_json"],
            row["params_hash"],
            None,
            row["compute_backend"],
            row["pipeline_version"],
            row["git_commit"],
            row["started_at"],
            _now(),
        ),
    ).lastrowid
    return int(
        conn.execute(
            "INSERT INTO ocr_runs (run_id, model_id, input_variant, tag) "
            "VALUES (?,?,?,?)",
            (processing_run_id, model_id, variant, tag),
        ).lastrowid
    )
