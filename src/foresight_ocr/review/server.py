"""Local review server.

Standard library only — no web framework. The review app is a tool for one
person on one machine, and adding a server dependency to a pipeline whose other
dependencies are all numeric would be a poor trade.

Serving crops straight off disk means the reviewer always sees the exact pixels
the recognizer saw, not a re-encoded copy.
"""

from __future__ import annotations

import io
import json
import mimetypes
import threading
import uuid
import webbrowser
import zipfile
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, cast
from urllib.parse import parse_qs, quote, urlparse

from PIL import Image

from foresight_ocr.ocr.fields import compose_entry, own_id_from_digits
from foresight_ocr.ocr.ondemand import recognize_regions
from foresight_ocr.persistence import connect, init_schema
from foresight_ocr.persistence.locks import StageBusy, stage_lock
from foresight_ocr.project import Project
from foresight_ocr.regions import store as region_store
from foresight_ocr.regions.crops import (
    CropUnavailable,
    ensure_crop,
    ensure_cross_page_previews,
)
from foresight_ocr.regions.recut import (
    PageNotSegmentable,
    apply_comb,
    comb_inputs,
    plan_comb,
)
from foresight_ocr.review.data import (
    delete_correction,
    export_document,
    export_verified,
    latest_ocr_tag,
    page_entries,
    page_image,
    page_is_ignored,
    page_summary,
    page_variant_image,
    progress,
    reviewable_pages,
    save_correction,
    set_page_ignored,
)
from foresight_ocr.review.protocol import PROTOCOL_VERSION

MAX_JSON_BODY_BYTES = 1_000_000
LOCAL_HTTP_HOSTS = {"127.0.0.1", "localhost", "::1"}

APP_HTML = Path(__file__).with_name("app.html")

# Keep the reviewed document path at source resolution.  Lower resolutions are
# useful benchmark configurations, but a watermark-crop pilot changed printed
# numerals and parent fields.  Keeping the options explicit ensures document
# jobs share one stable cache identity without accepting that quality loss.
DOCUMENT_OCR_OPTIONS: dict[str, Any] = {}


def _learning_snapshot(
    project: Project,
    conn,
    document_id: str,
    *,
    previous: dict | None = None,
) -> dict[str, Any]:
    """Return the latest durable learning scorecard and its freshness.

    GET stays read-only: only the explicit POST action rewrites the report.
    That distinction keeps opening the panel from looking like model training.
    """
    path = project.analysis_dir(document_id, "ocr-learning") / "corrections.json"
    if previous is None and path.exists():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = None
    reviewed = progress(conn, document_id)["reviewed"]
    if previous is None:
        return {
            "ok": True,
            "status": "missing",
            "document_id": document_id,
            "pending_corrections": reviewed,
            "report": None,
            "report_url": "/api/learn-ocr/report",
        }
    analyzed_at = previous.get("analyzed_at")
    if not analyzed_at and path.exists():
        from datetime import datetime, timezone

        analyzed_at = datetime.fromtimestamp(
            path.stat().st_mtime, tz=timezone.utc
        ).isoformat()
    return {
        "ok": True,
        "status": "ready",
        "document_id": document_id,
        "analyzed_at": analyzed_at,
        "pending_corrections": max(
            0, reviewed - int(previous.get("reviewed_entries") or 0)
        ),
        "report": previous,
        "report_url": "/api/learn-ocr/report",
    }


class PageIgnoredError(ValueError):
    """A mutating page action was attempted while the page is ignored."""


def _require_active_page(conn, document_id: str, page: int) -> None:
    if page_is_ignored(conn, document_id, page):
        raise PageIgnoredError(
            f"page {page} is ignored; restore it before editing or recognizing it"
        )


def _document_region_rows(conn, document_id: str):
    """Live OCR targets, excluding pages the reviewer explicitly ignored."""
    return conn.execute(
        """SELECT r.region_uid, r.page_index FROM regions r
           LEFT JOIN pages p
             ON p.document_id = r.document_id AND p.page_index = r.page_index
           WHERE r.document_id = ? AND r.deleted_at IS NULL
             AND r.state != 'rejected' AND COALESCE(p.ignored, 0) = 0
           ORDER BY r.page_index, r.band_ordinal, r.reading_order""",
        (document_id,),
    ).fetchall()


def _reocr_page(
    conn,
    project: Project,
    document_id: str,
    page: int,
    *,
    backend: str = "paddleocr_vl",
    variant: str = "watermark",
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    active_at_acceptance: bool = False,
) -> dict:
    """Force a fresh machine reading for every live region on one page."""
    if not active_at_acceptance:
        _require_active_page(conn, document_id, page)
    if variant != "watermark":
        raise ValueError(
            "page re-OCR only supports the watermark-suppressed watermark variant"
        )
    region_uids = [
        row["region_uid"]
        for row in conn.execute(
            "SELECT region_uid FROM regions "
            "WHERE document_id = ? AND page_index = ? AND deleted_at IS NULL "
            "AND state != 'rejected' "
            "ORDER BY band_ordinal, reading_order",
            (document_id, page),
        )
    ]
    if on_progress:
        on_progress({"stage": "queued", "completed": 0, "total": len(region_uids)})
    answers = recognize_regions(
        conn,
        project,
        document_id,
        region_uids,
        backend=backend,
        variant=variant,
        force=True,
        on_progress=on_progress,
    )
    conn.commit()
    errors = [
        {"region_uid": answer.region_uid, "error": answer.error}
        for answer in answers
        if answer.error
    ]
    return {
        "page": page,
        "requested": len(region_uids),
        "read": len(answers) - len(errors),
        "errors": errors,
        "backend": backend,
        "variant": variant,
    }


def _reocr_document(
    conn,
    project: Project,
    document_id: str,
    *,
    backend: str = "paddleocr_vl",
    variant: str = "watermark",
    options: dict[str, Any] | None = None,
    force: bool = False,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict:
    """Read every live region whose current pixels/config lack an answer.

    Whole-document work is incremental by default.  `force=True` remains an
    explicit escape hatch for deliberately taking up changed weights or
    comparing a fresh run; it is not the expensive default behind the review
    button.
    """
    if variant != "watermark":
        raise ValueError(
            "document re-OCR only supports the watermark-suppressed watermark variant"
        )
    rows = _document_region_rows(conn, document_id)
    region_uids = [row["region_uid"] for row in rows]
    pages = list(dict.fromkeys(int(row["page_index"]) for row in rows))
    page_position = {page: index for index, page in enumerate(pages, start=1)}
    region_page = {row["region_uid"]: int(row["page_index"]) for row in rows}

    if on_progress:
        on_progress(
            {
                "stage": "queued",
                "completed": 0,
                "total": len(region_uids),
                "completed_pages": 0,
                "current_page_position": 0,
                "total_pages": len(pages),
            }
        )

    def relay(event: dict[str, Any]) -> None:
        if not on_progress:
            return
        uid = event.get("region_uid")
        page = region_page.get(uid)
        position = page_position.get(page, 0) if page is not None else 0
        completed = max(0, int(event.get("completed") or 0))
        total = max(0, int(event.get("total") or 0))
        completed_pages = (
            len(pages) if total and completed >= total else max(0, position - 1)
        )
        on_progress(
            {
                **event,
                "page": page,
                "completed_pages": completed_pages,
                "current_page_position": position,
                "total_pages": len(pages),
            }
        )

    answers = recognize_regions(
        conn,
        project,
        document_id,
        region_uids,
        backend=backend,
        variant=variant,
        options=options if options is not None else DOCUMENT_OCR_OPTIONS,
        force=force,
        on_progress=relay,
    )
    conn.commit()
    errors = [
        {"region_uid": answer.region_uid, "error": answer.error}
        for answer in answers
        if answer.error
    ]
    return {
        "pages": len(pages),
        "requested": len(region_uids),
        "read": len(answers) - len(errors),
        "recognized": sum(not answer.reused and not answer.error for answer in answers),
        "reused": sum(answer.reused for answer in answers),
        "errors": errors,
        "backend": backend,
        "variant": variant,
        "options": options if options is not None else DOCUMENT_OCR_OPTIONS,
        "force": force,
    }


def _export_bundle(conn, project: Project, document_id: str, tag: str | None):
    """Write canonical exports and return browser-writable file contents."""
    from foresight_ocr.document.profile import load_profile

    generation_labels = load_profile(project.configs, document_id).band_labels
    out = project.analysis_dir(document_id, "transcription") / f"{document_id}.tsv"
    counts = export_document(
        conn,
        document_id,
        out,
        tag,
        generation_labels=generation_labels,
    )
    gold = project.root / "benchmarks" / "gold" / f"{document_id}_verified.tsv"
    counts["verified"] = export_verified(
        conn,
        document_id,
        gold,
        generation_labels=generation_labels,
    )
    counts["verified_path"] = str(gold)
    files = [
        {"name": out.name, "content": out.read_text(encoding="utf-8")},
        {"name": gold.name, "content": gold.read_text(encoding="utf-8")},
    ]
    return counts, files


def _boundary_overrides(raw) -> dict[int, float]:
    """Parse the small index-to-x map shared by preview and apply requests."""
    if raw in (None, ""):
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PageNotSegmentable("manual boundaries are not valid JSON") from exc
    if not isinstance(raw, dict):
        raise PageNotSegmentable("manual boundaries must be an index-to-position map")
    try:
        return {int(index): float(x) for index, x in raw.items()}
    except (TypeError, ValueError) as exc:
        raise PageNotSegmentable("manual boundary positions must be numbers") from exc


def _comb_preview(conn, project: Project, document_id: str, page: int, q) -> dict:
    """The lattice at a given phase, plus what it was fitted from.

    The measurements come back with the plan because they are the reviewer's
    evidence: a page that detected ten gutters where its neighbours detect
    thirteen is a page whose phase was voted on by too few witnesses, and that
    is worth seeing before deciding how far to move it.
    """
    inputs = comb_inputs(conn, project, document_id, page)

    def number(name: str) -> float | None:
        raw = q.get(name, [""])[0]
        return float(raw) if raw not in ("", None) else None

    plan = plan_comb(
        inputs,
        phase_offset=float(q.get("phase", ["0"])[0] or 0.0),
        pitch=number("pitch"),
        snap=q.get("snap", ["1"])[0] not in ("0", "false"),
        text_left=number("left"),
        text_right=number("right"),
        boundary_overrides=_boundary_overrides(q.get("manual", [""])[0]),
    )
    return {
        **plan.to_dict(),
        "fitted_pitch": inputs.pitch,
        "corpus_pitch": inputs.corpus_pitch,
        "used_corpus_pitch": inputs.used_corpus_pitch,
        "pitch_confidence": inputs.pitch_confidence,
        "gutters": inputs.gutters,
        # Named apart from the plan's own extent, which this dict already
        # carries: one is what the lattice was fitted to, the other is what the
        # reviewer chose, and collapsing them made the echo contradict the
        # boundaries it came with.
        "fitted_text_left": inputs.text_left,
        "fitted_text_right": inputs.text_right,
        "bands": [
            {"ordinal": b.ordinal, "label": b.label, "top": b.top, "bottom": b.bottom}
            for b in inputs.bands
        ],
    }


def _handler_factory(
    project: Project, document_id: str, tag: str | None, reviewer: str
):
    db_path = project.db_path

    # Each request opens its own connection: ThreadingHTTPServer handles
    # requests on separate threads and SQLite connections are not shareable
    # across them.
    def _conn():
        conn = connect(db_path)
        init_schema(conn)
        return conn

    task_lock = threading.Lock()
    mutation_lock = threading.Lock()
    document_task: dict[str, Any] = {
        "id": None,
        "status": "idle",
        "stage": "idle",
        "completed_pages": 0,
        "current_page_position": 0,
        "total_pages": 0,
        "completed_regions": 0,
        "total_regions": 0,
        "percent": 0.0,
    }

    def task_snapshot() -> dict[str, Any]:
        with task_lock:
            return dict(document_task)

    def task_update(job_id: str, **changes: Any) -> None:
        with task_lock:
            if document_task.get("id") == job_id:
                document_task.update(changes)

    @contextmanager
    def exclusive_pipeline_mutation(stage: str):
        if not mutation_lock.acquire(blocking=False):
            raise StageBusy(
                f"another review mutation is already writing {document_id}; "
                f"cannot start `{stage}`"
            )
        try:
            with stage_lock(project.artifacts, document_id, stage):
                yield
        finally:
            mutation_lock.release()

    def run_document_task(
        job_id: str,
        backend: str,
        variant: str,
        force: bool,
        mutation_guard,
    ) -> None:
        conn = None
        try:
            conn = _conn()

            def record(event: dict[str, Any]) -> None:
                stage = event.get("stage") or "preparing"
                completed = max(0, int(event.get("completed") or 0))
                total = max(0, int(event.get("total") or 0))
                fraction = completed / total if total else 0.0
                percent = (
                    10.0 * fraction
                    if stage == "preparing"
                    else 10.0 + 90.0 * fraction
                    if stage == "recognizing"
                    else 0.0
                )
                task_update(
                    job_id,
                    status="running",
                    stage=stage,
                    completed_pages=int(event.get("completed_pages") or 0),
                    current_page_position=int(event.get("current_page_position") or 0),
                    total_pages=int(event.get("total_pages") or 0),
                    completed_regions=completed,
                    total_regions=total,
                    percent=round(min(99.9, percent), 1),
                    page=event.get("page"),
                )

            report = _reocr_document(
                conn,
                project,
                document_id,
                backend=backend,
                variant=variant,
                force=force,
                on_progress=record,
            )
            task_update(
                job_id,
                status="complete",
                stage="complete",
                completed_pages=report["pages"],
                current_page_position=report["pages"],
                total_pages=report["pages"],
                completed_regions=report["requested"],
                total_regions=report["requested"],
                percent=100.0,
                report=report,
                review_progress=progress(conn, document_id),
                error=None,
            )
        except Exception as exc:  # noqa: BLE001
            if conn is not None:
                conn.rollback()
            task_update(
                job_id,
                status="error",
                stage="error",
                error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            if conn is not None:
                conn.close()
            mutation_guard.__exit__(None, None, None)

    allowed_roots = [
        project.crops_dir(document_id).resolve(),
        project.pages_dir(document_id, "normalized").resolve(),
        project.pages_dir(document_id, "review-watermark").resolve(),
    ]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # noqa: A003 - silence per-request logging
            pass

        def end_headers(self) -> None:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("Referrer-Policy", "no-referrer")
            super().end_headers()

        def _local_authority(self, value: str, *, origin: bool = False) -> bool:
            try:
                parsed = urlparse(value if origin else f"//{value}")
                port = parsed.port
            except ValueError:
                return False
            server_address = cast(tuple[str, int], self.server.server_address)
            server_port = int(server_address[1])
            return (
                (not origin or parsed.scheme == "http")
                and parsed.hostname in LOCAL_HTTP_HOSTS
                and port == server_port
                and parsed.username is None
                and parsed.password is None
            )

        def _trusted_browser_request(self, *, mutation: bool = False) -> bool:
            host = self.headers.get("Host")
            if host is None or not self._local_authority(host):
                return False
            site = self.headers.get("Sec-Fetch-Site")
            allowed_sites = {None, "same-origin"}
            if not mutation:
                # Browser-launched top-level navigation uses `none` rather than
                # `same-origin`; it is safe because Host is still loopback-only.
                allowed_sites.add("none")
            if site not in allowed_sites:
                return False
            origin = self.headers.get("Origin")
            return origin is None or self._local_authority(origin, origin=True)

        def _read_json_object(self) -> dict[str, Any] | None:
            if self.headers.get_content_type() != "application/json":
                self._json({"error": "Content-Type must be application/json"}, 415)
                return None
            raw_length = self.headers.get("Content-Length")
            if raw_length is None:
                self._json({"error": "Content-Length is required"}, 411)
                return None
            try:
                length = int(raw_length)
            except ValueError:
                self._json({"error": "invalid Content-Length"}, 400)
                return None
            if length < 0 or length > MAX_JSON_BODY_BYTES:
                self._json({"error": "JSON request body is too large"}, 413)
                return None
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._json({"error": "request body must be valid UTF-8 JSON"}, 400)
                return None
            if not isinstance(payload, dict):
                self._json({"error": "request body must be a JSON object"}, 400)
                return None
            return payload

        def _send(
            self, code: int, body: bytes, content_type: str, cache: bool = False
        ) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            if not cache:
                # The app and its data change under the reviewer: re-running
                # `graph` rewrites the findings, and editing app.html changes the
                # page. Chrome served both from cache until a hard reload, so
                # findings for entries already corrected stayed on screen.
                # Crops and page images are content-addressed by path and never
                # change, so those are still cached.
                self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload, code: int = 200) -> None:
            self._send(
                code,
                json.dumps(payload, ensure_ascii=False).encode(),
                "application/json; charset=utf-8",
            )

        def _ndjson(self, work: Callable[[Callable[[dict], None]], dict]) -> None:
            """Stream progress events, followed by exactly one result or error."""
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            connected = True

            def send(payload: dict) -> None:
                nonlocal connected
                if not connected:
                    return
                try:
                    self.wfile.write(
                        json.dumps(payload, ensure_ascii=False).encode() + b"\n"
                    )
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    # Recognition must finish and commit even if the tab closes.
                    connected = False

            try:
                result = work(lambda event: send({"type": "progress", **event}))
                send({"type": "result", **result})
            except Exception as exc:  # noqa: BLE001
                send({"type": "error", "error": f"{type(exc).__name__}: {exc}"})

        def do_GET(self) -> None:  # noqa: N802
            if not self._trusted_browser_request():
                self._json({"error": "untrusted request authority"}, 403)
                return
            url = urlparse(self.path)
            q = parse_qs(url.query)

            if url.path in ("/", "/index.html"):
                self._send(200, APP_HTML.read_bytes(), "text/html; charset=utf-8")
                return

            if url.path == "/api/pages":
                conn = _conn()
                try:
                    summary = page_summary(conn, document_id)
                    self._json(
                        {
                            "protocol_version": PROTOCOL_VERSION,
                            "document_id": document_id,
                            "pages": [s["page"] for s in summary],
                            "summary": summary,
                            "progress": progress(conn, document_id),
                            "tag": tag,
                            "capabilities": [
                                "page_image_watermark",
                                "crop_image_variants",
                                "page_reocr",
                                "streaming_reocr",
                                "manual_boundaries",
                                "document_reocr",
                                "document_reocr_progress",
                                "folder_export",
                                "zip_export",
                                "page_ignore",
                                "correction_unconfirm",
                                "correction_learning",
                            ],
                        }
                    )
                finally:
                    conn.close()
                return

            if url.path == "/api/reocr-all":
                self._json({"ok": True, "job": task_snapshot()})
                return

            if url.path == "/api/learn-ocr":
                conn = _conn()
                try:
                    self._json(_learning_snapshot(project, conn, document_id))
                finally:
                    conn.close()
                return

            if url.path == "/api/learn-ocr/report":
                report_path = (
                    project.analysis_dir(document_id, "ocr-learning") / "REPORT.md"
                )
                if not report_path.exists():
                    self._json({"error": "尚未生成校对学习报告"}, 404)
                    return
                self._send(
                    200,
                    report_path.read_bytes(),
                    "text/markdown; charset=utf-8",
                )
                return

            if url.path == "/api/export.zip":
                conn = _conn()
                try:
                    _counts, files = _export_bundle(conn, project, document_id, tag)
                    buffer = io.BytesIO()
                    with zipfile.ZipFile(
                        buffer, "w", compression=zipfile.ZIP_DEFLATED
                    ) as archive:
                        for file in files:
                            archive.writestr(file["name"], file["content"])
                    body = buffer.getvalue()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/zip")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header(
                        "Content-Disposition",
                        "attachment; filename=foresight-ocr-export.zip; "
                        f"filename*=UTF-8''{quote(document_id + '_export.zip')}",
                    )
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(body)
                finally:
                    conn.close()
                return

            if url.path == "/api/comb":
                # What the lattice would be at this phase. Read-only on purpose:
                # a person needs to see where the boundaries land before paying
                # for a re-cut and a re-read of the page.
                page = int(q.get("page", ["0"])[0])
                conn = _conn()
                try:
                    self._json(_comb_preview(conn, project, document_id, page, q))
                except PageNotSegmentable as exc:
                    self._json({"error": str(exc)}, 400)
                finally:
                    conn.close()
                return

            if url.path == "/api/numeral":
                # A reviewer types 343; the page says 三百四十三. Converting here
                # rather than in the browser keeps one implementation of how this
                # book writes numbers, which is also the one the parser reads.
                label = q.get("label", [""])[0]
                digits = q.get("n", [""])[0]
                self._json({"text": own_id_from_digits(label, digits)})
                return

            if url.path == "/api/page":
                page = int(q.get("page", ["0"])[0])
                # A spread shows consecutive pages together: a band's sequence
                # runs straight across the page break, so a boundary error is
                # only visible when both sides are on screen at once.
                span = max(1, min(3, int(q.get("spread", ["1"])[0])))
                conn = _conn()
                try:
                    # Ignored pages stay navigable so the reviewer can restore
                    # them; only processing and export treat them as absent.
                    available = reviewable_pages(
                        conn, document_id, include_ignored=True
                    )
                    wanted = [p for p in available if page <= p < page + span]
                    sheets = []
                    for p in wanted:
                        ignored = page_is_ignored(conn, document_id, p)
                        img = page_image(conn, document_id, p, project)
                        # Crop previews are derived files. Refresh only the
                        # cross-page ones here so a stitch-version change is
                        # visible on reload without spending an OCR call.
                        if not ignored:
                            ensure_cross_page_previews(
                                conn, project, document_id, p, img.width
                            )
                        sheets.append(
                            {
                                "page": p,
                                "ignored": ignored,
                                "image": img.path,
                                "width": img.width,
                                "height": img.height,
                                "frame_status": img.frame_status,
                                "entries": [
                                    e.to_dict()
                                    for e in page_entries(conn, document_id, p, tag)
                                ],
                            }
                        )
                    conn.commit()
                    self._json(
                        {
                            "page": page,
                            "pages": sheets,
                            "progress": progress(conn, document_id),
                        }
                    )
                finally:
                    conn.close()
                return

            if url.path == "/api/page-image":
                page = int(q.get("page", ["0"])[0])
                variant = q.get("variant", ["watermark"])[0]
                conn = _conn()
                try:
                    image = page_variant_image(
                        conn, document_id, page, project, variant=variant
                    )
                    conn.commit()
                    self._json(
                        {
                            "page": image.page_index,
                            "path": image.path,
                            "width": image.width,
                            "height": image.height,
                            "variant": variant,
                        }
                    )
                except (ValueError, RuntimeError) as exc:
                    self._json({"error": str(exc)}, 400)
                finally:
                    conn.close()
                return

            if url.path == "/api/crop-image":
                region_uid = q.get("region_uid", [""])[0]
                variant = q.get("variant", ["watermark"])[0]
                if not region_uid:
                    self._json({"error": "region_uid is required"}, 400)
                    return
                if variant not in {"original", "watermark"}:
                    self._json(
                        {"error": "variant must be 'original' or 'watermark'"},
                        400,
                    )
                    return
                conn = _conn()
                try:
                    region = region_store.get(conn, region_uid)
                    if (
                        region is None
                        or region.document_id != document_id
                        or region.deleted_at is not None
                        or region.state == "rejected"
                    ):
                        self._json({"error": "region not found"}, 404)
                        return
                    crop = ensure_crop(
                        conn,
                        project,
                        region,
                        variant=variant,
                        context="tight",
                    )
                    conn.commit()
                    with Image.open(crop.path) as crop_image_file:
                        width, height = crop_image_file.size
                    self._json(
                        {
                            "region_uid": crop.region_uid,
                            "variant": crop.variant,
                            "path": str(crop.path),
                            "width": width,
                            "height": height,
                            "pixel_bbox": crop.pixel_bbox,
                        }
                    )
                except CropUnavailable as exc:
                    self._json({"error": str(exc)}, 400)
                finally:
                    conn.close()
                return

            if url.path == "/img":
                raw = q.get("path", [""])[0]
                if not raw:
                    self._json({"error": "missing path"}, 400)
                    return
                target = Path(raw).resolve()
                # Only ever serve files from this document's own directories.
                if (
                    not any(target.is_relative_to(root) for root in allowed_roots)
                    or not target.is_file()
                ):
                    self._json({"error": "forbidden"}, 403)
                    return
                ctype = (
                    mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                )
                self._send(200, target.read_bytes(), ctype, cache=True)
                return

            self._json({"error": "not found"}, 404)

        def do_POST(self) -> None:  # noqa: N802
            if not self._trusted_browser_request(mutation=True):
                self._json({"error": "untrusted request origin or authority"}, 403)
                return
            url = urlparse(self.path)
            payload = self._read_json_object()
            if payload is None:
                return

            if url.path == "/api/page-ignore":
                ignored = payload.get("ignored")
                if not isinstance(ignored, bool):
                    self._json({"error": "ignored must be true or false"}, 400)
                    return
                try:
                    page = int(payload["page_index"])
                except (KeyError, TypeError, ValueError):
                    self._json({"error": "page_index must be an integer"}, 400)
                    return
                conn = _conn()
                try:
                    try:
                        state = set_page_ignored(conn, document_id, page, ignored)
                    except ValueError as exc:
                        self._json({"error": str(exc)}, 404)
                        return
                    summary = page_summary(conn, document_id)
                    self._json(
                        {
                            "ok": True,
                            "page_index": page,
                            "ignored": state,
                            "summary": summary,
                            "progress": progress(conn, document_id),
                        }
                    )
                finally:
                    conn.close()
                return

            if url.path == "/api/correction":
                conn = _conn()
                try:
                    page = int(payload["page_index"])
                    try:
                        _require_active_page(conn, document_id, page)
                    except PageIgnoredError as exc:
                        self._json({"error": str(exc)}, 409)
                        return
                    role = payload.get("role") or "entry"
                    if payload.get("action") == "unconfirm":
                        removed = delete_correction(
                            conn,
                            document_id,
                            page_index=page,
                            band_label=payload["band_label"],
                            entry_index=int(payload["entry_index"]),
                            role=role,
                        )
                        self._json(
                            {
                                "ok": True,
                                "confirmed": False,
                                "removed": removed,
                                "progress": progress(conn, document_id),
                            }
                        )
                        return
                    # Fields are what the reviewer edits; a transcription is what
                    # gets stored. Composing here means the record stays a line
                    # of the page rather than becoming a private format.
                    text = payload.get("transcription")
                    if text is None and "fields" in payload:
                        f = payload["fields"] or {}
                        if "parent_order" in f:
                            text = compose_entry(
                                f.get("own_id"),
                                f.get("parent_order"),
                                None,
                                f.get("additional_info"),
                            )
                        else:
                            text = compose_entry(
                                f.get("own_id"),
                                f.get("parent"),
                                f.get("birth_order"),
                                f.get("additional_info"),
                            )
                    unreadable = bool(payload.get("unreadable"))
                    if text is None and not unreadable:
                        # A readable correction with no remaining characters is
                        # an explicit blank, not permission to show OCR again.
                        text = ""
                    save_correction(
                        conn,
                        document_id,
                        page_index=page,
                        band_label=payload["band_label"],
                        entry_index=int(payload["entry_index"]),
                        # Empty string is a real human decision: the machine put
                        # text in a column where the reviewer confirms none is
                        # printed.  Do not collapse it into "no correction".
                        transcription=text,
                        unreadable=unreadable,
                        reviewer=reviewer,
                        note=payload.get("note") or None,
                        role=role,
                    )
                    self._json(
                        {
                            "ok": True,
                            "transcription": text,
                            "progress": progress(conn, document_id),
                        }
                    )
                finally:
                    conn.close()
                return

            if url.path == "/api/learn-ocr":
                from foresight_ocr.ocr.learning import write_learning_report

                conn = _conn()
                try:
                    before = _learning_snapshot(project, conn, document_id)
                    json_path, _md_path, _report = write_learning_report(
                        project, conn, document_id, tag
                    )
                    fresh = json.loads(json_path.read_text(encoding="utf-8"))
                    result = _learning_snapshot(
                        project, conn, document_id, previous=fresh
                    )
                    old = before.get("report") or {}
                    old_rate = old.get("exact_core_rate")
                    new_rate = fresh.get("exact_core_rate")
                    delta = (
                        float(new_rate) - float(old_rate)
                        if old_rate is not None and new_rate is not None
                        else None
                    )
                    result["comparison"] = {
                        "previous_exact_core_rate": old_rate,
                        "delta": delta,
                        "status": (
                            "lower"
                            if delta is not None and delta < 0
                            else "higher"
                            if delta is not None and delta > 0
                            else "same"
                            if delta is not None
                            else "first"
                        ),
                    }
                    self._json(result)
                finally:
                    conn.close()
                return

            if url.path == "/api/recut":
                # The one edit that repairs a whole page: the columns are moved
                # onto the lattice the reviewer chose, then cut and read again.
                conn = _conn()
                try:
                    page = int(payload["page"])
                    # Check before opening an NDJSON response so ignored pages
                    # receive an actual HTTP 409, even for streaming clients.
                    _require_active_page(conn, document_id, page)
                    inputs = comb_inputs(conn, project, document_id, page)

                    def _num(name: str) -> float | None:
                        value = payload.get(name)
                        return float(str(value)) if value not in (None, "") else None

                    plan = plan_comb(
                        inputs,
                        phase_offset=float(payload.get("phase_offset") or 0.0),
                        pitch=_num("pitch"),
                        snap=bool(payload.get("snap", True)),
                        text_left=_num("text_left"),
                        text_right=_num("text_right"),
                        boundary_overrides=_boundary_overrides(
                            payload.get("boundary_overrides")
                        ),
                    )

                    def run(on_progress=None):
                        report = apply_comb(
                            conn,
                            project,
                            plan,
                            inputs,
                            actor=reviewer,
                            reocr=bool(payload.get("reocr", True)),
                            backend=payload.get("backend") or "paddleocr_vl",
                            on_progress=on_progress,
                        )
                        return {
                            "ok": not report.errors,
                            "report": report.to_dict(),
                            "summary": report.summary(),
                            "progress": progress(conn, document_id),
                        }

                    with exclusive_pipeline_mutation("review-recut"):
                        if payload.get("stream"):
                            self._ndjson(run)
                        else:
                            self._json(run())
                except PageIgnoredError as exc:
                    self._json({"error": str(exc)}, 409)
                except PageNotSegmentable as exc:
                    self._json({"error": str(exc)}, 400)
                except StageBusy as exc:
                    self._json({"error": str(exc)}, 409)
                except Exception as exc:  # noqa: BLE001
                    # A missing recognizer or an unreadable page must reach the
                    # reviewer as a sentence, not as a dead request; the geometry
                    # is already committed and is what they asked for.
                    self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
                finally:
                    conn.close()
                return

            if url.path == "/api/reocr":
                conn = _conn()
                try:
                    page = int(payload["page"])
                    # As above, reject before the streaming headers are sent.
                    _require_active_page(conn, document_id, page)

                    def run(on_progress=None):
                        report = _reocr_page(
                            conn,
                            project,
                            document_id,
                            page,
                            backend=payload.get("backend") or "paddleocr_vl",
                            variant=payload.get("variant") or "watermark",
                            on_progress=on_progress,
                            # The endpoint already accepted this page while it
                            # was active. A later ignore affects future jobs,
                            # not this in-flight snapshot.
                            active_at_acceptance=True,
                        )
                        return {
                            "ok": not report["errors"],
                            "report": report,
                            "progress": progress(conn, document_id),
                        }

                    with exclusive_pipeline_mutation("review-reocr"):
                        if payload.get("stream"):
                            self._ndjson(run)
                        else:
                            self._json(run())
                except PageIgnoredError as exc:
                    self._json({"error": str(exc)}, 409)
                except StageBusy as exc:
                    self._json({"error": str(exc)}, 409)
                except Exception as exc:  # noqa: BLE001
                    self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
                finally:
                    conn.close()
                return

            if url.path == "/api/reocr-all":
                job_id = uuid.uuid4().hex
                # Check and reserve atomically. Without this critical section,
                # two simultaneous clicks can both observe idle and launch two
                # expensive document recognizers.
                with task_lock:
                    current = dict(document_task)
                    if current.get("status") in {"queued", "running"}:
                        started = False
                    else:
                        document_task.clear()
                        document_task.update(
                            {
                                "id": job_id,
                                "status": "queued",
                                "stage": "queued",
                                "completed_pages": 0,
                                "current_page_position": 0,
                                "total_pages": 0,
                                "completed_regions": 0,
                                "total_regions": 0,
                                "percent": 0.0,
                                "page": None,
                                "error": None,
                            }
                        )
                        current = dict(document_task)
                        started = True
                if not started:
                    self._json({"ok": True, "started": False, "job": current})
                    return

                mutation_guard = exclusive_pipeline_mutation("review-reocr-all")
                try:
                    mutation_guard.__enter__()
                except StageBusy as exc:
                    task_update(
                        job_id,
                        status="error",
                        stage="error",
                        error=str(exc),
                    )
                    self._json({"error": str(exc)}, 409)
                    return

                conn = None
                try:
                    conn = _conn()
                    rows = _document_region_rows(conn, document_id)
                    pages = list(dict.fromkeys(int(row["page_index"]) for row in rows))
                    total_regions = len(rows)
                except Exception as exc:  # noqa: BLE001
                    task_update(
                        job_id,
                        status="error",
                        stage="error",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
                    mutation_guard.__exit__(None, None, None)
                    return
                finally:
                    if conn is not None:
                        conn.close()
                task_update(
                    job_id,
                    total_pages=len(pages),
                    total_regions=total_regions,
                )
                worker = threading.Thread(
                    target=run_document_task,
                    args=(
                        job_id,
                        payload.get("backend") or "paddleocr_vl",
                        payload.get("variant") or "watermark",
                        bool(payload.get("force", False)),
                        mutation_guard,
                    ),
                    name=f"foresight-ocr-document-ocr-{job_id[:8]}",
                    daemon=True,
                )
                try:
                    worker.start()
                except Exception as exc:  # noqa: BLE001
                    mutation_guard.__exit__(None, None, None)
                    detail = f"{type(exc).__name__}: {exc}"
                    task_update(
                        job_id,
                        status="error",
                        stage="error",
                        error=detail,
                    )
                    self._json({"error": detail}, 500)
                    return
                self._json({"ok": True, "started": True, "job": task_snapshot()}, 202)
                return

            if url.path == "/api/export":
                conn = _conn()
                try:
                    counts, files = _export_bundle(conn, project, document_id, tag)
                    self._json({"ok": True, **counts, "files": files})
                finally:
                    conn.close()
                return

            self._json({"error": "not found"}, 404)

    return Handler


def serve(
    project: Project,
    document_id: str,
    host: str = "127.0.0.1",
    port: int = 8765,
    tag: str | None = None,
    reviewer: str = "local",
    open_browser: bool = True,
    ready_json: bool = False,
) -> None:
    # Band labels are document data. Without this the server reads them from the
    # fallback profile, which is right for the first volume and silently wrong
    # for every other one.
    from foresight_ocr.context import set_profile
    from foresight_ocr.document.profile import load_profile

    set_profile(load_profile(project.configs, document_id))

    conn = connect(project.db_path)
    init_schema(conn)
    if tag is None:
        tag = latest_ocr_tag(conn, document_id)
    # Even an all-ignored document must open: restoring a page is part of the
    # review surface, while processing/export sees only active pages.
    pages = reviewable_pages(conn, document_id, include_ignored=True)
    stats = progress(conn, document_id)
    conn.close()

    if not pages:
        raise RuntimeError("nothing to review; run segment and benchmark first")

    handler = _handler_factory(project, document_id, tag, reviewer)
    server = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{server.server_port}/"
    if ready_json:
        print(
            json.dumps(
                {
                    "type": "ready",
                    "protocol_version": PROTOCOL_VERSION,
                    "url": url.rstrip("/"),
                    "document_id": document_id,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    print(f"review server: {url}")
    print(
        f"  document {document_id}: {len(pages)} pages, "
        f"{stats['entries']} entries, {stats['reviewed']} already reviewed"
    )
    print(f"  pre-filling from OCR configuration: {tag or 'default'}")
    print("  ctrl-c to stop")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
