"""Local review server.

Standard library only — no web framework. The review app is a tool for one
person on one machine, and adding a server dependency to a pipeline whose other
dependencies are all numeric would be a poor trade.

Serving crops straight off disk means the reviewer always sees the exact pixels
the recognizer saw, not a re-encoded copy.
"""

from __future__ import annotations

import json
import mimetypes
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from familyocr.ocr.fields import compose_entry, own_id_from_digits
from familyocr.persistence import connect, init_schema
from familyocr.project import Project
from familyocr.regions.recut import (
    PageNotSegmentable,
    apply_comb,
    comb_inputs,
    plan_comb,
)
from familyocr.review.data import (
    export_document,
    export_verified,
    page_image,
    latest_ocr_tag,
    page_entries,
    page_summary,
    progress,
    reviewable_pages,
    save_correction,
)

APP_HTML = Path(__file__).with_name("app.html")


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


def _handler_factory(project: Project, document_id: str, tag: str | None,
                     reviewer: str):
    db_path = project.db_path
    # Each request opens its own connection: ThreadingHTTPServer handles
    # requests on separate threads and SQLite connections are not shareable
    # across them.
    def _conn():
        conn = connect(db_path)
        init_schema(conn)
        return conn

    allowed_roots = [
        project.crops_dir(document_id).resolve(),
        project.pages_dir(document_id, "normalized").resolve(),
    ]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # noqa: A003 - silence per-request logging
            pass

        def _send(self, code: int, body: bytes, content_type: str,
                  cache: bool = False) -> None:
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
            self._send(code, json.dumps(payload, ensure_ascii=False).encode(),
                       "application/json; charset=utf-8")

        def do_GET(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            q = parse_qs(url.query)

            if url.path in ("/", "/index.html"):
                self._send(200, APP_HTML.read_bytes(), "text/html; charset=utf-8")
                return

            if url.path == "/api/pages":
                conn = _conn()
                try:
                    summary = page_summary(conn, document_id)
                    self._json({
                        "document_id": document_id,
                        "pages": [s["page"] for s in summary],
                        "summary": summary,
                        "progress": progress(conn, document_id),
                        "tag": tag,
                    })
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
                    available = reviewable_pages(conn, document_id)
                    wanted = [p for p in available if page <= p < page + span]
                    sheets = []
                    for p in wanted:
                        img = page_image(conn, document_id, p, project)
                        sheets.append({
                            "page": p,
                            "image": img.path,
                            "width": img.width,
                            "height": img.height,
                            "frame_status": img.frame_status,
                            "entries": [e.to_dict() for e in
                                        page_entries(conn, document_id, p, tag)],
                        })
                    self._json({"page": page, "pages": sheets,
                                "progress": progress(conn, document_id)})
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
                if not any(
                    str(target).startswith(str(root)) for root in allowed_roots
                ) or not target.is_file():
                    self._json({"error": "forbidden"}, 403)
                    return
                ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                self._send(200, target.read_bytes(), ctype, cache=True)
                return

            self._json({"error": "not found"}, 404)

        def do_POST(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")

            if url.path == "/api/correction":
                conn = _conn()
                try:
                    # Fields are what the reviewer edits; a transcription is what
                    # gets stored. Composing here means the record stays a line
                    # of the page rather than becoming a private format.
                    text = payload.get("transcription")
                    if text is None and "fields" in payload:
                        f = payload["fields"] or {}
                        text = compose_entry(
                            f.get("own_id"), f.get("parent"), f.get("birth_order")
                        )
                    save_correction(
                        conn, document_id,
                        page_index=int(payload["page_index"]),
                        band_label=payload["band_label"],
                        entry_index=int(payload["entry_index"]),
                        transcription=text or None,
                        unreadable=bool(payload.get("unreadable")),
                        reviewer=reviewer,
                        note=payload.get("note") or None,
                    )
                    self._json({
                        "ok": True,
                        "transcription": text or None,
                        "progress": progress(conn, document_id),
                    })
                finally:
                    conn.close()
                return

            if url.path == "/api/recut":
                # The one edit that repairs a whole page: the columns are moved
                # onto the lattice the reviewer chose, then cut and read again.
                conn = _conn()
                try:
                    page = int(payload["page"])
                    inputs = comb_inputs(conn, project, document_id, page)
                    def _num(name):
                        value = payload.get(name)
                        return float(value) if value not in (None, "") else None

                    plan = plan_comb(
                        inputs,
                        phase_offset=float(payload.get("phase_offset") or 0.0),
                        pitch=_num("pitch"),
                        snap=bool(payload.get("snap", True)),
                        text_left=_num("text_left"),
                        text_right=_num("text_right"),
                    )
                    report = apply_comb(
                        conn, project, plan, inputs,
                        actor=reviewer,
                        reocr=bool(payload.get("reocr", True)),
                        backend=payload.get("backend") or "paddleocr_vl",
                    )
                    self._json({
                        "ok": not report.errors,
                        "report": report.to_dict(),
                        "summary": report.summary(),
                        "progress": progress(conn, document_id),
                    })
                except PageNotSegmentable as exc:
                    self._json({"error": str(exc)}, 400)
                except Exception as exc:                     # noqa: BLE001
                    # A missing recognizer or an unreadable page must reach the
                    # reviewer as a sentence, not as a dead request; the geometry
                    # is already committed and is what they asked for.
                    self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
                finally:
                    conn.close()
                return

            if url.path == "/api/export":
                conn = _conn()
                try:
                    out = (project.analysis_dir(document_id, "transcription")
                           / f"{document_id}.tsv")
                    counts = export_document(conn, document_id, out, tag)
                    gold = (project.root / "benchmarks" / "gold"
                            / f"{document_id}_verified.tsv")
                    counts["verified"] = export_verified(conn, document_id, gold)
                    counts["verified_path"] = str(gold)
                    self._json({"ok": True, **counts})
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
) -> None:
    # Band labels are document data. Without this the server reads them from the
    # fallback profile, which is right for the first volume and silently wrong
    # for every other one.
    from familyocr.context import set_profile
    from familyocr.document.profile import load_profile

    set_profile(load_profile(project.configs, document_id))

    conn = connect(project.db_path)
    init_schema(conn)
    if tag is None:
        tag = latest_ocr_tag(conn, document_id)
    pages = reviewable_pages(conn, document_id)
    stats = progress(conn, document_id)
    conn.close()

    if not pages:
        raise RuntimeError("nothing to review; run segment and benchmark first")

    handler = _handler_factory(project, document_id, tag, reviewer)
    server = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{port}/"
    print(f"review server: {url}")
    print(f"  document {document_id}: {len(pages)} pages, "
          f"{stats['entries']} entries, {stats['reviewed']} already reviewed")
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
