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

from familyocr.persistence import connect, init_schema
from familyocr.project import Project
from familyocr.review.data import (
    export_verified,
    latest_ocr_tag,
    page_entries,
    progress,
    reviewable_pages,
    save_correction,
)

APP_HTML = Path(__file__).with_name("app.html")


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

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
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
                    self._json({
                        "document_id": document_id,
                        "pages": reviewable_pages(conn, document_id),
                        "progress": progress(conn, document_id),
                        "tag": tag,
                    })
                finally:
                    conn.close()
                return

            if url.path == "/api/page":
                page = int(q.get("page", ["0"])[0])
                conn = _conn()
                try:
                    entries = [e.to_dict() for e in
                               page_entries(conn, document_id, page, tag)]
                    self._json({"page": page, "entries": entries,
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
                self._send(200, target.read_bytes(), ctype)
                return

            self._json({"error": "not found"}, 404)

        def do_POST(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")

            if url.path == "/api/correction":
                conn = _conn()
                try:
                    save_correction(
                        conn, document_id,
                        page_index=int(payload["page_index"]),
                        band_label=payload["band_label"],
                        entry_index=int(payload["entry_index"]),
                        transcription=payload.get("transcription") or None,
                        unreadable=bool(payload.get("unreadable")),
                        reviewer=reviewer,
                        note=payload.get("note") or None,
                    )
                    self._json({"ok": True, "progress": progress(conn, document_id)})
                finally:
                    conn.close()
                return

            if url.path == "/api/export":
                conn = _conn()
                try:
                    out = (project.root / "benchmarks" / "gold"
                           / f"{document_id}_verified.tsv")
                    n = export_verified(conn, document_id, out)
                    self._json({"ok": True, "written": n, "path": str(out)})
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
