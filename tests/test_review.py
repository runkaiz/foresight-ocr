"""Review data layer.

The load-bearing test here is that a human correction survives reprocessing.
The spec requires it, the schema was designed for it (corrections key on the
stable document/page/band/entry tuple rather than on a candidate row), and until
now nothing exercised it.
"""

import json
import sqlite3
import threading
import time
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import urlencode

import numpy as np
import pytest

from foresight_ocr.context import set_profile
from foresight_ocr.document.profile import DocumentProfile
from foresight_ocr.imaging.io import read_image, write_image
from foresight_ocr.ocr.base import OCRResult, register_backend
from foresight_ocr.ocr.learning import analyze_corrections
from foresight_ocr.persistence.db import connect, init_schema
from foresight_ocr.project import Project
from foresight_ocr.regions import store
from foresight_ocr.regions.model import geometry_hash
from foresight_ocr.review.data import (
    delete_correction,
    export_document,
    export_verified,
    page_entries,
    page_is_ignored,
    page_summary,
    page_variant_image,
    progress,
    reviewable_pages,
    save_correction,
    set_page_ignored,
)
from foresight_ocr.review.server import (
    _export_bundle,
    _handler_factory,
    _reocr_document,
    _reocr_page,
)


class ReviewFakeRecognizer:
    name = "review_fake"
    model_version = "review-fake-1"
    calls = []

    def __init__(self, **options):
        self.options = options

    def available(self):
        return True, "fake"

    def recognize(self, requests):
        self.calls.append([request.crop_id for request in requests])
        return [
            OCRResult(
                crop_id=request.crop_id,
                transcription=f"fresh:{request.crop_id[:8]}",
                backend=self.name,
                model_version=self.model_version,
                confidence=1.0,
            )
            for request in requests
        ]


register_backend("review_fake")(ReviewFakeRecognizer)


def test_page_preview_preserves_scan_aspect_ratio_and_overlay_alignment():
    app_html = (
        Path(__file__).parents[1] / "src/foresight_ocr/review/app.html"
    ).read_text(encoding="utf-8")

    assert "object-fit:contain" in app_html
    assert "object-position:center center" in app_html
    assert 'preserveAspectRatio="xMidYMid meet"' in app_html
    assert "object-fit:fill" not in app_html
    assert 'preserveAspectRatio="none"' not in app_html


def test_reviewer_exposes_optional_additional_information_field():
    app_html = (
        Path(__file__).parents[1] / "src/foresight_ocr/review/app.html"
    ).read_text(encoding="utf-8")

    assert "'additional_info'" in app_html
    assert "附加信息（如生辰）" in app_html


def test_reviewer_edits_parent_and_birth_order_in_separate_fields():
    app_html = (
        Path(__file__).parents[1] / "src/foresight_ocr/review/app.html"
    ).read_text(encoding="utf-8")

    assert (
        "const FIELDS = ['own_id','parent','birth_order','additional_info']" in app_html
    )
    assert "父辈编号或姓名" in app_html
    assert "排行" in app_html


def test_reviewer_exposes_manual_boundaries_and_streamed_ocr_progress():
    app_html = (
        Path(__file__).parents[1] / "src/foresight_ocr/review/app.html"
    ).read_text(encoding="utf-8")

    assert "boundary_overrides" in app_html
    assert "comb-hit" in app_html
    assert "已手调" in app_html
    assert "application/x-ndjson" in app_html
    assert "recognition-progress" in app_html


def test_reviewer_exposes_reachable_grid_and_unconfirm_controls():
    app_html = (
        Path(__file__).parents[1] / "src/foresight_ocr/review/app.html"
    ).read_text(encoding="utf-8")

    # The opener is rendered in every visible sheet title, not inside the
    # layout bar whose default CSS state is hidden.
    layoutbar_markup = app_html.split('<div id="layoutbar">', 1)[1].split(
        "</div>\n\n<main>", 1
    )[0]
    assert 'class="layout-toggle"' not in layoutbar_markup
    assert 'class="layout-toggle" data-page="${s.page}"' in app_html
    assert 'aria-pressed="false"' in app_html
    assert 'onclick="toggleLayout(${s.page})"' in app_html
    assert ">版面网格</button>" in app_html
    assert "button.setAttribute('aria-pressed', pressed ? 'true' : 'false')" in app_html

    # Applying remains unavailable until a real preview has returned.
    assert '<button id="recut-button" onclick="applyComb()" disabled>' in app_html
    assert "$('#recut-button').disabled = false;" in app_html

    assert "CAPABILITIES.includes('correction_unconfirm')" in app_html
    assert 'class="unconfirm-entry"' in app_html
    assert "本条已确认" in app_html and "取消确认" in app_html
    assert "async function unconfirmSelected(button=null)" in app_html
    assert "action:'unconfirm'" in app_html
    assert "if(ev.key==='Enter' && ev.shiftKey)" in app_html
    assert "return unconfirmSelected();" in app_html
    assert "<kbd>⇧⏎</kbd> 取消确认" in app_html


def test_reviewer_exposes_whole_pdf_progress_and_folder_export():
    app_html = (
        Path(__file__).parents[1] / "src/foresight_ocr/review/app.html"
    ).read_text(encoding="utf-8")

    assert "识别整本 PDF" in app_html
    assert "/api/reocr-all" in app_html
    assert "current_page_position" in app_html
    assert "showDirectoryPicker" in app_html
    assert "getFileHandle" in app_html
    assert "/api/export.zip" in app_html
    assert "fetch('/api/export',{method:'POST'," in app_html
    assert "headers:{'Content-Type':'application/json'}, body:'{}'" in app_html
    assert app_html.count("method:'POST'") == app_html.count(
        "'Content-Type':'application/json'"
    )
    assert 'aria-label="整本 PDF 识别状态"' in app_html


def test_reviewer_exposes_non_modal_correction_learning_panel():
    app_html = (
        Path(__file__).parents[1] / "src/foresight_ocr/review/app.html"
    ).read_text(encoding="utf-8")

    assert ">校对学习</button>" in app_html
    assert 'id="learning-panel"' in app_html
    assert "/api/learn-ocr" in app_html
    assert "不会重跑 OCR、覆盖人工校对" in app_html
    assert "async function runLearningAnalysis()" in app_html
    assert 'aria-label="校对学习状态"' in app_html


def test_reviewer_exposes_restorable_page_ignore_state():
    app_html = (
        Path(__file__).parents[1] / "src/foresight_ocr/review/app.html"
    ).read_text(encoding="utf-8")

    assert "/api/page-ignore" in app_html
    assert "忽略此页" in app_html and "恢复此页" in app_html
    assert "本页已忽略" in app_html
    assert "保留扫描图像 · 不参与识别、校对进度或导出" in app_html
    assert "SHEETS.filter(s => !s.ignored)" in app_html
    assert "s && !s.ignored && s.flagged" in app_html
    assert "const showRaw = e.human!==''" in app_html


def test_reviewer_shortcuts_are_control_only_and_ignore_option():
    app_html = (
        Path(__file__).parents[1] / "src/foresight_ocr/review/app.html"
    ).read_text(encoding="utf-8")

    assert "function shortcutLetter(ev)" in app_html
    assert "/^Key[A-Z]$/.test(ev.code || '')" in app_html
    assert "!ev.ctrlKey || ev.altKey || ev.metaKey" in app_html
    assert "String.fromCharCode(legacy).toLowerCase()" in app_html
    assert "ev.isComposing || ev.keyCode===229" in app_html
    assert "{capture:true}" in app_html
    assert "if(event.altKey) return;" in app_html
    assert "if(ev.altKey) return;" in app_html
    for letter in ("u", "e", "n", "p", "l"):
        assert f"if(shortcut==='{letter}')" in app_html
    assert "ev.altKey || ev.ctrlKey" not in app_html
    assert "⌥" not in app_html
    for letter in ("E", "N", "U", "L"):
        assert f"⌃{letter}" in app_html


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    conn.execute(
        "INSERT INTO documents (id, title, source_path, checksum, page_count, "
        "created_at) VALUES ('doc','t','p','c',1,'now')"
    )
    conn.execute(
        "INSERT INTO pages (document_id, page_index, width, height) "
        "VALUES ('doc',58,1400,1000)"
    )
    conn.execute(
        "INSERT INTO processing_runs (document_id, stage, params_json, "
        "params_hash, compute_backend, pipeline_version, git_commit, started_at) "
        "VALUES ('doc','segment','{}','h','local','0','x','now')"
    )
    return conn


def _segment(conn, crop_suffix="v1"):
    """Simulate a segmentation pass: layout, bands, regions and their crops.

    A pass re-cuts the crops and keeps the regions, which is what reconcile does
    and what the review path now reads: the region is the column, and the crop
    is one rendering of the pixels it currently names.
    """
    conn.execute("DELETE FROM page_layouts")
    cur = conn.execute(
        "INSERT INTO page_layouts (document_id, page_index) VALUES ('doc', 58)"
    )
    layout_id = cur.lastrowid
    conn.execute(
        "INSERT INTO bands (page_layout_id, band_index, label, bbox_json) "
        "VALUES (?,0,'庶','[]')",
        (layout_id,),
    )
    for entry in (0, 1):
        bbox = [1000.0 - 300 * entry, 0.0, 1300.0 - 300 * entry, 900.0]
        digest = geometry_hash("doc", 58, bbox)
        row = conn.execute(
            "SELECT id FROM regions WHERE document_id='doc' AND page_index=58 "
            "AND reading_order = ?",
            (entry,),
        ).fetchone()
        if row is None:
            region = store.create_region(
                conn,
                "doc",
                58,
                bbox,
                band_label="庶",
                band_ordinal=0,
                reading_order=entry,
                entry_index=entry,
            )
            region_id = region.id
        else:
            region_id = row["id"]
        conn.execute(
            "INSERT INTO region_crops (region_id, geometry_hash, context, pad_frac, "
            "variant, pixel_bbox_json, crop_key, path, created_at) "
            "VALUES (?,?, 'tight', 0.0, 'maxrgb', ?, ?, ?, 'now')",
            (
                region_id,
                digest,
                json.dumps([int(v) for v in bbox]),
                f"crop_{crop_suffix}_{entry}",
                f"/crops/{crop_suffix}_{entry}.png",
            ),
        )
    conn.commit()


def _add_ocr(conn, texts, tag="t"):
    conn.execute(
        "INSERT INTO models (id, name, version, backend) "
        "VALUES ('m','paddleocr_vl','1.6','paddleocr_vl') "
        "ON CONFLICT(id) DO NOTHING"
    )
    cur = conn.execute(
        "INSERT INTO ocr_runs (run_id, model_id, input_variant, tag) "
        "VALUES (1,'m','maxrgb',?)",
        (tag,),
    )
    run = cur.lastrowid
    # The newest crop of each region is what a pass would have read.
    crops = conn.execute(
        "SELECT region_id, crop_key, MAX(id) FROM region_crops GROUP BY region_id "
        "ORDER BY region_id"
    ).fetchall()
    for crop, text in zip(crops, texts, strict=True):
        conn.execute(
            "INSERT INTO ocr_candidates (region_id, ocr_run_id, crop_key, "
            "cache_key, transcription) VALUES (?,?,?,?,?)",
            (
                crop["region_id"],
                run,
                crop["crop_key"],
                f"{crop['crop_key']}:{run}",
                text,
            ),
        )
    conn.commit()


def test_page_entries_pairs_machine_text_with_crops():
    conn = _db()
    _segment(conn)
    _add_ocr(conn, ["庶一長子", "庶二次子"])
    entries = page_entries(conn, "doc", 58)
    assert [e.entry_index for e in entries] == [0, 1]
    assert [e.machine for e in entries] == ["庶一長子", "庶二次子"]
    assert entries[0].band_label == "庶"
    assert entries[0].crop_bbox == [1000, 0, 1300, 900]
    assert entries[0].human is None


def test_page_entries_splits_parent_and_birth_order_for_review():
    conn = _db()
    _segment(conn)
    _add_ocr(conn, ["庶一允一長子", "庶二允二次子"])
    entries = page_entries(conn, "doc", 58)
    assert entries[0].parent == "允一"
    assert entries[0].birth_order == "長子"
    # Kept only so an older browser can still read this API response.
    assert entries[0].parent_order == "允一長子"


def test_page_entries_splits_bare_son_marker_from_parent_id():
    conn = _db()
    _segment(conn)
    conn.execute("UPDATE bands SET label = '富'")
    conn.execute("UPDATE regions SET band_label = '富'")
    conn.commit()
    _add_ocr(conn, ["富三十庶四十一子", "富三十一庶四十二長子"])

    entry = page_entries(conn, "doc", 58)[0]
    assert entry.own_id == "富三十"
    assert entry.parent == "庶四十一"
    assert entry.birth_order == "子"


def test_correction_does_not_touch_the_machine_transcription():
    conn = _db()
    _segment(conn)
    _add_ocr(conn, ["庶一長子", "庶二次子"])
    save_correction(conn, "doc", 58, "庶", 0, "庶一次子")
    entries = page_entries(conn, "doc", 58)
    assert entries[0].human == "庶一次子"
    assert entries[0].machine == "庶一長子"  # untouched


def test_correction_learning_scores_fields_without_overwriting_evidence():
    set_profile(
        DocumentProfile(
            document_id="doc",
            band_labels=["庶", "富", "教"],
            generation_chain=["允", "庶", "富", "教"],
            bands_per_page=3,
        )
    )
    conn = _db()
    _segment(conn)
    _add_ocr(
        conn,
        [
            "允十一\n庚十二\n長子",
            "庶十三允十一長子",
        ],
    )
    save_correction(conn, "doc", 58, "庶", 0, "庶十二允十一長子")
    save_correction(conn, "doc", 58, "庶", 1, "庶十三允十一長子")

    report = analyze_corrections(conn, "doc", tag="t")

    assert report.reviewed_entries == report.eligible_entries == 2
    assert report.exact_core_entries == 2
    assert report.field_exact == {"own_id": 2, "parent": 2, "birth_order": 2}
    assert report.recoveries["own_label_from_geometry"] == 1
    assert page_entries(conn, "doc", 58, tag="t")[0].machine == ("允十一\n庚十二\n長子")


def test_review_http_generates_auditable_learning_snapshot(tmp_path):
    set_profile(
        DocumentProfile(
            document_id="doc",
            band_labels=["庶", "富", "教"],
            generation_chain=["允", "庶", "富", "教"],
            bands_per_page=3,
        )
    )
    project = Project(tmp_path)
    conn = connect(project.db_path)
    init_schema(conn)
    conn.execute(
        "INSERT INTO documents (id, title, source_path, checksum, page_count, "
        "created_at) VALUES ('doc','t','p','c',1,'now')"
    )
    conn.execute(
        "INSERT INTO pages (document_id, page_index, width, height) "
        "VALUES ('doc',58,1400,1000)"
    )
    conn.execute(
        "INSERT INTO processing_runs (document_id, stage, params_json, "
        "params_hash, compute_backend, pipeline_version, git_commit, started_at) "
        "VALUES ('doc','segment','{}','h','local','0','x','now')"
    )
    _segment(conn)
    _add_ocr(conn, ["庶一允一長子", "庶二允一子"], tag="t")
    save_correction(conn, "doc", 58, "庶", 0, "庶一允一長子")
    conn.close()

    handler = _handler_factory(project, "doc", "t", "test")
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def request(path, method="GET"):
        body = b"{}" if method == "POST" else None
        req = urllib.request.Request(
            base + path,
            data=body,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=3) as response:
            return response.status, response.read(), response.headers

    try:
        status, body, _headers = request("/api/learn-ocr")
        missing = json.loads(body)
        assert status == 200 and missing["status"] == "missing"
        assert missing["pending_corrections"] == 1

        status, body, _headers = request("/api/learn-ocr", "POST")
        learned = json.loads(body)
        assert status == 200 and learned["status"] == "ready"
        assert learned["pending_corrections"] == 0
        assert learned["report"]["eligible_entries"] == 1
        assert learned["report"]["exact_core_rate"] == 1.0
        assert learned["analyzed_at"]
        assert learned["comparison"]["status"] == "first"

        status, body, headers = request("/api/learn-ocr/report")
        assert status == 200
        assert headers.get_content_type() == "text/markdown"
        assert "人工校對學習報告" in body.decode()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_review_http_rejects_cross_site_rebinding_and_malformed_json(tmp_path):
    project = Project(tmp_path)
    handler = _handler_factory(project, "doc", None, "test")
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def request(path="/", *, method="GET", body=None, headers=None):
        req = urllib.request.Request(
            base + path,
            data=body,
            method=method,
            headers=headers or {},
        )
        try:
            response = urllib.request.urlopen(req, timeout=3)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status, json.loads(response.read()), response.headers

    try:
        req = urllib.request.Request(base + "/")
        with urllib.request.urlopen(req, timeout=3) as response:
            assert response.status == 200
            assert response.headers["X-Frame-Options"] == "DENY"
            assert response.headers["Cross-Origin-Resource-Policy"] == "same-origin"
            assert response.headers["X-Content-Type-Options"] == "nosniff"

        req = urllib.request.Request(base + "/", headers={"Sec-Fetch-Site": "none"})
        with urllib.request.urlopen(req, timeout=3) as response:
            assert response.status == 200

        status, result, _headers = request(
            "/api/not-a-route",
            method="POST",
            body=b"{}",
            headers={
                "Content-Type": "application/json",
                "Origin": "https://attacker.example",
                "Sec-Fetch-Site": "cross-site",
            },
        )
        assert status == 403
        assert result["error"] == "untrusted request origin or authority"

        status, result, _headers = request(
            method="GET", headers={"Host": "attacker.example"}
        )
        assert status == 403
        assert result["error"] == "untrusted request authority"

        status, result, _headers = request(
            "/api/not-a-route",
            method="POST",
            body=b"{}",
            headers={"Content-Type": "text/plain", "Origin": base},
        )
        assert status == 415
        assert result["error"] == "Content-Type must be application/json"

        status, result, _headers = request(
            "/api/not-a-route",
            method="POST",
            body=b"{",
            headers={"Content-Type": "application/json", "Origin": base},
        )
        assert status == 400
        assert result["error"] == "request body must be valid UTF-8 JSON"

        status, result, _headers = request(
            "/api/not-a-route",
            method="POST",
            body=b"[]",
            headers={"Content-Type": "application/json", "Origin": base},
        )
        assert status == 400
        assert result["error"] == "request body must be a JSON object"

        status, result, _headers = request(
            "/api/not-a-route",
            method="POST",
            body=b"{}",
            headers={
                "Content-Type": "application/json",
                "Content-Length": "1000001",
                "Origin": base,
            },
        )
        assert status == 413
        assert result["error"] == "JSON request body is too large"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    ("transcription", "unreadable", "expected_human"),
    [
        pytest.param("人工校对", False, "人工校对", id="text"),
        pytest.param("", False, "", id="blank"),
        pytest.param(None, True, None, id="unreadable"),
    ],
)
def test_delete_correction_restores_machine_fallback_and_progress(
    transcription,
    unreadable,
    expected_human,
):
    conn = _db()
    _segment(conn)
    _add_ocr(conn, ["庶一長子", "庶二次子"])
    save_correction(conn, "doc", 58, "庶", 0, transcription, unreadable=unreadable)

    before = page_entries(conn, "doc", 58)[0]
    assert before.human == expected_human
    assert before.unreadable is unreadable
    assert progress(conn, "doc") == {"entries": 2, "reviewed": 1}
    assert page_summary(conn, "doc")[0]["reviewed"] == 1

    assert delete_correction(conn, "doc", 58, "庶", 0, role="entry") is True
    assert delete_correction(conn, "doc", 58, "庶", 0, role="entry") is False

    restored = page_entries(conn, "doc", 58)[0]
    assert restored.human is None
    assert restored.unreadable is False
    assert restored.machine == "庶一長子"
    assert restored.own_id == "庶一"
    assert progress(conn, "doc") == {"entries": 2, "reviewed": 0}
    assert page_summary(conn, "doc")[0]["reviewed"] == 0


def test_delete_correction_isolates_the_exact_entry_role():
    conn = _db()
    _segment(conn)
    _add_ocr(conn, ["庶一長子", "庶二次子"])
    save_correction(conn, "doc", 58, "庶", 0, "人物校对", role="entry")
    save_correction(conn, "doc", 58, "庶", 0, "页眉校对", role="header")

    assert delete_correction(conn, "doc", 58, "庶", 0, role="header") is True
    rows = conn.execute(
        "SELECT role, transcription FROM human_corrections "
        "WHERE document_id='doc' AND page_index=58 "
        "AND band_label='庶' AND entry_index=0 ORDER BY role"
    ).fetchall()
    assert [(row["role"], row["transcription"]) for row in rows] == [
        ("entry", "人物校对")
    ]
    assert page_entries(conn, "doc", 58)[0].human == "人物校对"
    assert progress(conn, "doc") == {"entries": 2, "reviewed": 1}


def test_blank_corrections_override_machine_on_reload_and_in_exports(tmp_path):
    conn = _db()
    _segment(conn)
    _add_ocr(conn, ["庶一長子", "庶二次子"])

    # Empty TEXT is the current representation. NULL + readable is how the
    # server historically stored the same decision, and existing books contain
    # those rows, so both must mask rather than resurrect the OCR candidate.
    save_correction(conn, "doc", 58, "庶", 0, "")
    save_correction(conn, "doc", 58, "庶", 1, None)

    entries = page_entries(conn, "doc", 58)
    assert [entry.human for entry in entries] == ["", ""]
    assert [entry.machine for entry in entries] == ["庶一長子", "庶二次子"]
    assert [entry.own_id for entry in entries] == [None, None]

    whole = tmp_path / "doc.tsv"
    counts = export_document(conn, "doc", whole)
    assert counts["entries"] == counts["human"] == 2
    body = whole.read_text(encoding="utf-8")
    assert "庶一長子" not in body and "庶二次子" not in body
    assert all(line.endswith("\thuman") for line in body.splitlines()[3:])

    verified = tmp_path / "verified.tsv"
    assert export_verified(conn, "doc", verified) == 1
    assert "58\t庶\t0\t" in verified.read_text(encoding="utf-8")


def test_correction_survives_reprocessing():
    conn = _db()
    _segment(conn, crop_suffix="v1")
    _add_ocr(conn, ["庶一長子", "庶二次子"])
    save_correction(conn, "doc", 58, "庶", 0, "庶一次子")

    # Re-segment: the page is cut again and read again, exactly as
    # `foresight-ocr segment` does when the pixels change.
    _segment(conn, crop_suffix="v2")
    _add_ocr(conn, ["庶一長子", "庶二次子"])

    entries = page_entries(conn, "doc", 58)
    assert entries[0].human == "庶一次子", "human correction lost on reprocessing"
    assert entries[0].crop_path.endswith("v2_0.png")


def test_marking_unreadable_records_no_transcription():
    conn = _db()
    _segment(conn)
    save_correction(conn, "doc", 58, "庶", 1, None, unreadable=True)
    entries = page_entries(conn, "doc", 58)
    assert entries[1].unreadable is True
    assert entries[1].human is None


def test_re_correcting_updates_in_place():
    conn = _db()
    _segment(conn)
    save_correction(conn, "doc", 58, "庶", 0, "first")
    save_correction(conn, "doc", 58, "庶", 0, "second")
    rows = conn.execute("SELECT COUNT(*) n FROM human_corrections").fetchone()["n"]
    assert rows == 1
    assert page_entries(conn, "doc", 58)[0].human == "second"


def test_progress_counts_reviewed_entries():
    conn = _db()
    _segment(conn)
    assert progress(conn, "doc") == {"entries": 2, "reviewed": 0}
    save_correction(conn, "doc", 58, "庶", 0, "x")
    assert progress(conn, "doc")["reviewed"] == 1


def test_ignored_page_stays_navigable_but_leaves_progress_and_exports(tmp_path):
    conn = _db()
    _segment(conn)
    _add_ocr(conn, ["庶一長子", "庶二次子"])
    save_correction(conn, "doc", 58, "庶", 0, "庶一次子")
    before = {
        table: conn.execute(f"SELECT COUNT(*) n FROM {table}").fetchone()["n"]
        for table in ("regions", "ocr_candidates", "human_corrections")
    }

    assert set_page_ignored(conn, "doc", 58, True) is True
    assert page_is_ignored(conn, "doc", 58) is True
    assert reviewable_pages(conn, "doc") == []
    assert reviewable_pages(conn, "doc", include_ignored=True) == [58]
    assert page_summary(conn, "doc") == [
        {
            "page": 58,
            "entries": 2,
            "flagged": 0,
            "reviewed": 1,
            "ignored": True,
        }
    ]
    assert progress(conn, "doc") == {"entries": 0, "reviewed": 0}

    whole = tmp_path / "ignored.tsv"
    verified = tmp_path / "ignored-verified.tsv"
    assert export_document(conn, "doc", whole)["entries"] == 0
    assert export_verified(conn, "doc", verified) == 0
    assert before == {
        table: conn.execute(f"SELECT COUNT(*) n FROM {table}").fetchone()["n"]
        for table in before
    }

    assert set_page_ignored(conn, "doc", 58, False) is False
    assert page_is_ignored(conn, "doc", 58) is False
    assert reviewable_pages(conn, "doc") == [58]
    assert progress(conn, "doc") == {"entries": 2, "reviewed": 1}
    assert export_document(conn, "doc", whole)["entries"] == 2
    assert export_verified(conn, "doc", verified) == 1


def test_missing_page_rows_remain_active_and_cannot_be_toggled():
    conn = _db()
    _segment(conn)
    conn.execute("DELETE FROM pages WHERE document_id = 'doc' AND page_index = 58")
    conn.commit()

    assert page_is_ignored(conn, "doc", 58) is False
    assert reviewable_pages(conn, "doc") == [58]
    assert page_summary(conn, "doc") == [
        {
            "page": 58,
            "entries": 2,
            "flagged": 0,
            "reviewed": 0,
            "ignored": False,
        }
    ]
    assert progress(conn, "doc") == {"entries": 2, "reviewed": 0}
    with pytest.raises(ValueError, match="unknown page 58"):
        set_page_ignored(conn, "doc", 58, True)


def test_zero_region_cover_stays_navigable_for_ignore_and_restore():
    conn = _db()

    assert reviewable_pages(conn, "doc") == [58]
    assert page_summary(conn, "doc") == [
        {
            "page": 58,
            "entries": 0,
            "flagged": 0,
            "reviewed": 0,
            "ignored": False,
        }
    ]

    set_page_ignored(conn, "doc", 58, True)
    assert reviewable_pages(conn, "doc") == []
    assert reviewable_pages(conn, "doc", include_ignored=True) == [58]
    assert page_summary(conn, "doc")[0]["ignored"] is True

    set_page_ignored(conn, "doc", 58, False)
    assert reviewable_pages(conn, "doc") == [58]


def test_page_summary_keeps_a_header_only_page_navigable():
    conn = _db()
    _segment(conn)
    conn.execute("UPDATE regions SET role = 'header'")
    conn.commit()

    assert page_summary(conn, "doc") == [
        {
            "page": 58,
            "entries": 0,
            "flagged": 0,
            "reviewed": 0,
            "ignored": False,
        }
    ]


def test_header_corrections_do_not_advance_person_progress():
    conn = _db()
    _segment(conn)
    region = store.for_page(conn, "doc", 58)[0]
    conn.execute("UPDATE regions SET role='header' WHERE id=?", (region.id,))
    save_correction(conn, "doc", 58, "庶", 0, "庶字第", role="header")
    assert progress(conn, "doc") == {"entries": 1, "reviewed": 0}


def test_export_writes_only_readable_verified_entries(tmp_path):
    conn = _db()
    _segment(conn)
    save_correction(conn, "doc", 58, "庶", 0, "庶一長子")
    save_correction(conn, "doc", 58, "庶", 1, None, unreadable=True)
    out = tmp_path / "verified.tsv"
    n = export_verified(conn, "doc", out)
    assert n == 1
    body = out.read_text(encoding="utf-8")
    assert "# page\tband\tentry\town_id\tparent\tbirth_order\tadditional_info" in body
    assert "58\t庶\t0\t庶一\t\t長子\t" in body
    # An unreadable entry must not be exported as if it were verified text.
    assert body.count("\n58\t") == 0 or body.strip().count("58\t") == 1


def test_verified_export_uses_generation_and_numeric_id_order(tmp_path):
    set_profile(
        DocumentProfile(
            document_id="other",
            band_labels=["富", "教", "庶"],
            # Keep the real parent chain while making the active band order wrong;
            # the explicit export labels must still determine row order.
            generation_chain=["允", "庶", "富", "教"],
            bands_per_page=3,
        )
    )
    conn = _db()
    save_correction(conn, "doc", 2, "富", 0, "富一庶一長子")
    save_correction(conn, "doc", 3, "庶", 1, "庶二允一長子")
    save_correction(conn, "doc", 4, "庶", 0, "庶一允一長子")
    save_correction(conn, "doc", 1, "教", 0, "教一富一長子")
    save_correction(conn, "doc", 0, "庶", 9, "無法辨識")

    out = tmp_path / "verified.tsv"
    export_verified(conn, "doc", out, generation_labels=["庶", "富", "教"])
    data = [
        line.split("\t")
        for line in out.read_text(encoding="utf-8").splitlines()
        if not line.startswith("#")
    ]

    assert [(row[3], row[4], row[5]) for row in data] == [
        ("庶一", "允一", "長子"),
        ("庶二", "允一", "長子"),
        ("", "無法辨識", ""),
        ("富一", "庶一", "長子"),
        ("教一", "富一", "長子"),
    ]
    assert [row[0] for row in data] == ["4", "3", "0", "2", "1"]


def test_verified_export_splits_parent_order_and_additional_information(tmp_path):
    conn = _db()
    save_correction(
        conn,
        "doc",
        2,
        "庶",
        0,
        "庶一允一長子\n生於光緒甲辰年二月初九日辰時",
    )

    out = tmp_path / "verified.tsv"
    export_verified(conn, "doc", out)

    data = [
        line.split("\t")
        for line in out.read_text(encoding="utf-8").splitlines()
        if not line.startswith("#")
    ]
    assert data == [
        [
            "2",
            "庶",
            "0",
            "庶一",
            "允一",
            "長子",
            "生於光緒甲辰年二月初九日辰時",
        ]
    ]


def test_findings_are_attached_to_their_entry():
    conn = _db()
    _segment(conn)
    conn.execute(
        "INSERT INTO validation_findings (document_id, band_label, kind, "
        "page_index, entry_index, expected, observed) "
        "VALUES ('doc','庶','gap',58,1,'二','三')"
    )
    conn.commit()
    entries = page_entries(conn, "doc", 58)
    assert entries[0].findings == []
    assert entries[1].findings[0]["kind"] == "gap"


def test_tag_filter_selects_one_ocr_configuration():
    conn = _db()
    _segment(conn)
    _add_ocr(conn, ["庶一長子", "庶二次子"])
    assert page_entries(conn, "doc", 58, tag="t")[0].machine == "庶一長子"
    assert page_entries(conn, "doc", 58, tag="absent")[0].machine is None


def test_page_variant_suppresses_cyan_without_changing_source(tmp_path):
    project = Project(tmp_path)
    conn = _db()
    pages = project.pages_dir("doc", "normalized")
    pages.mkdir(parents=True)
    source = np.full((40, 50, 3), 240, dtype=np.uint8)
    source[5:15, 5:15] = (255, 255, 0)  # cyan in BGR
    source[20:30, 20:30] = (30, 30, 30)  # neutral ink
    source[30:35, 5:15] = (180, 180, 180)  # neutral-gray logo
    source_path = pages / "p0058.png"
    assert write_image(source_path, source)

    rendered = page_variant_image(conn, "doc", 58, project)
    pixels = read_image(Path(rendered.path), 0)

    assert (rendered.width, rendered.height) == (50, 40)
    assert pixels[8, 8] == 255
    assert pixels[25, 25] == 0
    assert pixels[32, 8] == 255
    original = read_image(source_path)
    assert original is not None
    assert original[8, 8].tolist() == [255, 255, 0]
    assert page_variant_image(conn, "doc", 58, project).path == rendered.path


def test_page_reocr_uses_watermark_variant_and_preserves_human_correction(tmp_path):
    project = Project(tmp_path)
    conn = _db()
    _segment(conn)
    pages = project.pages_dir("doc", "normalized")
    pages.mkdir(parents=True)
    image = np.full((1000, 1400, 3), 240, dtype=np.uint8)
    image[:, 700:1300] = 30
    assert write_image(pages / "p0058.png", image)
    save_correction(conn, "doc", 58, "庶", 0, "人工校对")
    ReviewFakeRecognizer.calls = []

    events = []
    report = _reocr_page(
        conn,
        project,
        "doc",
        58,
        backend="review_fake",
        variant="watermark",
        on_progress=events.append,
    )

    assert report["requested"] == report["read"] == 2
    assert report["errors"] == []
    assert len(ReviewFakeRecognizer.calls) == 1
    assert (
        conn.execute("SELECT DISTINCT input_variant FROM ocr_runs").fetchone()[0]
        == "watermark"
    )
    assert page_entries(conn, "doc", 58)[0].human == "人工校对"
    assert events[0] == {"stage": "queued", "completed": 0, "total": 2}
    assert {event["stage"] for event in events} == {
        "queued",
        "preparing",
        "recognizing",
    }
    assert {key: events[-1][key] for key in ("stage", "completed", "total")} == {
        "stage": "recognizing",
        "completed": 2,
        "total": 2,
    }
    assert events[-1]["region_uid"]


def test_document_reocr_reports_page_progress_and_preserves_corrections(tmp_path):
    project = Project(tmp_path)
    conn = _db()
    _segment(conn)
    pages = project.pages_dir("doc", "normalized")
    pages.mkdir(parents=True)
    image = np.full((1000, 1400, 3), 240, dtype=np.uint8)
    image[:, 700:1300] = 30
    assert write_image(pages / "p0058.png", image)
    save_correction(conn, "doc", 58, "庶", 0, "人工校对")
    ReviewFakeRecognizer.calls = []

    events = []
    report = _reocr_document(
        conn,
        project,
        "doc",
        backend="review_fake",
        variant="watermark",
        on_progress=events.append,
    )

    assert report["pages"] == 1
    assert report["requested"] == report["read"] == 2
    assert report["recognized"] == 2
    assert report["reused"] == 0
    assert report["options"] == {}
    assert report["force"] is False
    assert report["errors"] == []
    assert page_entries(conn, "doc", 58)[0].human == "人工校对"
    assert events[0]["stage"] == "queued"
    assert events[0]["total_pages"] == 1
    assert events[-1]["stage"] == "recognizing"
    assert events[-1]["current_page_position"] == 1
    assert events[-1]["completed_pages"] == 1

    ReviewFakeRecognizer.calls = []
    second = _reocr_document(
        conn,
        project,
        "doc",
        backend="review_fake",
        variant="watermark",
    )
    assert second["recognized"] == 0
    assert second["reused"] == 2
    assert ReviewFakeRecognizer.calls == []


def test_export_bundle_returns_browser_writable_files(tmp_path):
    project = Project(tmp_path)
    conn = _db()
    _segment(conn)
    _add_ocr(conn, ["庶一長子", "庶二次子"])
    save_correction(conn, "doc", 58, "庶", 0, "庶一次子")

    counts, files = _export_bundle(conn, project, "doc", "t")

    assert counts["entries"] == 2
    assert counts["human"] == 1
    assert [file["name"] for file in files] == [
        "doc.tsv",
        "doc_verified.tsv",
    ]
    assert "庶一次子" in files[0]["content"]
    assert "58\t庶\t0\t庶一\t\t次子\t" in files[1]["content"]


def test_review_http_document_job_is_single_and_zip_is_downloadable(
    tmp_path,
    monkeypatch,
):
    project = Project(tmp_path)
    conn = connect(project.db_path)
    init_schema(conn)
    conn.execute(
        "INSERT INTO documents (id, title, source_path, checksum, page_count, "
        "created_at) VALUES ('doc','t','p','c',1,'now')"
    )
    conn.execute(
        "INSERT INTO pages (document_id, page_index, width, height) "
        "VALUES ('doc',58,1400,1000)"
    )
    conn.execute(
        "INSERT INTO processing_runs (document_id, stage, params_json, "
        "params_hash, compute_backend, pipeline_version, git_commit, started_at) "
        "VALUES ('doc','segment','{}','h','local','0','x','now')"
    )
    _segment(conn)
    _add_ocr(conn, ["庶一長子", "庶二次子"], tag="t")
    conn.close()

    release = threading.Event()
    calls = []

    def fake_document_ocr(conn, project, document_id, **kwargs):
        calls.append(document_id)
        callback = kwargs["on_progress"]
        callback(
            {
                "stage": "recognizing",
                "completed": 1,
                "total": 2,
                "completed_pages": 0,
                "current_page_position": 1,
                "total_pages": 1,
                "page": 58,
            }
        )
        assert release.wait(2)
        return {
            "pages": 1,
            "requested": 2,
            "read": 2,
            "errors": [],
            "backend": "review_fake",
            "variant": "watermark",
        }

    monkeypatch.setattr(
        "foresight_ocr.review.server._reocr_document", fake_document_ocr
    )
    handler = _handler_factory(project, "doc", "t", "test")
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def request(path, method="GET"):
        body = b"{}" if method == "POST" else None
        req = urllib.request.Request(
            base + path,
            data=body,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=3) as response:
            return response.status, response.read(), response.headers

    try:
        gate = threading.Barrier(3)
        responses = []
        failures = []

        def start_job():
            try:
                gate.wait()
                responses.append(request("/api/reocr-all", "POST"))
            except Exception as exc:  # noqa: BLE001
                failures.append(exc)

        starters = [threading.Thread(target=start_job) for _ in range(2)]
        for starter in starters:
            starter.start()
        gate.wait()
        for starter in starters:
            starter.join(timeout=3)

        assert failures == [] and len(responses) == 2
        jobs = [(status, json.loads(body)) for status, body, _ in responses]
        first = next(result for _status, result in jobs if result["started"])
        duplicate = next(result for _status, result in jobs if not result["started"])
        assert sorted(status for status, _result in jobs) == [200, 202]
        assert first["started"] is True
        assert duplicate["started"] is False
        assert duplicate["job"]["status"] in {"queued", "running"}
        assert len(calls) <= 1

        from foresight_ocr.persistence.locks import StageBusy, stage_lock

        with pytest.raises(StageBusy, match="cannot start `segment`"):
            with stage_lock(project.artifacts, "doc", "segment"):
                pass

        release.set()
        deadline = time.monotonic() + 2
        while True:
            _status, body, _headers = request("/api/reocr-all")
            job = json.loads(body)["job"]
            if job["status"] == "complete":
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert job["percent"] == 100.0
        assert job["completed_pages"] == job["total_pages"] == 1
        assert calls == ["doc"]

        status, body, headers = request("/api/export.zip")
        assert status == 200
        assert headers.get_content_type() == "application/zip"
        with zipfile.ZipFile(BytesIO(body)) as archive:
            assert archive.namelist() == ["doc.tsv", "doc_verified.tsv"]
            assert "庶一長子" in archive.read("doc.tsv").decode()
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_review_http_persists_blank_and_enforces_ignored_page_contract(tmp_path):
    project = Project(tmp_path)
    conn = connect(project.db_path)
    init_schema(conn)
    conn.execute(
        "INSERT INTO documents (id, title, source_path, checksum, page_count, "
        "created_at) VALUES ('doc','t','p','c',1,'now')"
    )
    conn.execute(
        "INSERT INTO pages (document_id, page_index, width, height) "
        "VALUES ('doc',58,1400,1000)"
    )
    conn.execute(
        "INSERT INTO processing_runs (document_id, stage, params_json, "
        "params_hash, compute_backend, pipeline_version, git_commit, started_at) "
        "VALUES ('doc','segment','{}','h','local','0','x','now')"
    )
    _segment(conn)
    _add_ocr(conn, ["卷十", "下"], tag="t")
    conn.close()

    handler = _handler_factory(project, "doc", "t", "test")
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def request(path, method="GET", payload=None):
        data = None
        if method == "POST":
            data = json.dumps(payload or {}).encode()
        req = urllib.request.Request(
            base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            response = urllib.request.urlopen(req, timeout=3)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status, response.read(), response.headers

    try:
        status, body, _headers = request(
            "/api/correction",
            "POST",
            {
                "page_index": 58,
                "band_label": "庶",
                "entry_index": 0,
                "role": "entry",
                "fields": {
                    "own_id": "庶一",
                    "parent": "允一",
                    "birth_order": "子",
                    "additional_info": None,
                },
                "unreadable": False,
            },
        )
        correction = json.loads(body)
        assert status == 200
        assert correction["transcription"] == "庶一允一子"

        status, body, _headers = request(
            "/api/correction",
            "POST",
            {
                "page_index": 58,
                "band_label": "庶",
                "entry_index": 0,
                "role": "entry",
                "fields": {
                    "own_id": None,
                    "parent": None,
                    "birth_order": None,
                    "additional_info": None,
                },
                "unreadable": False,
            },
        )
        correction = json.loads(body)
        assert status == 200
        assert correction["transcription"] == ""

        check = connect(project.db_path)
        stored = check.execute(
            "SELECT transcription, typeof(transcription) kind "
            "FROM human_corrections WHERE document_id='doc' AND page_index=58 "
            "AND band_label='庶' AND entry_index=0 AND role='entry'"
        ).fetchone()
        assert stored["transcription"] == "" and stored["kind"] == "text"
        check.close()

        status, body, _headers = request("/api/page?page=58&spread=1")
        page = json.loads(body)
        assert status == 200 and page["pages"][0]["ignored"] is False
        first = page["pages"][0]["entries"][0]
        assert first["machine"] == "卷十"
        assert first["human"] == "" and first["own_id"] is None

        status, body, _headers = request("/api/export.zip")
        assert status == 200
        with zipfile.ZipFile(BytesIO(body)) as archive:
            full = archive.read("doc.tsv").decode()
            verified = archive.read("doc_verified.tsv").decode()
        assert "卷十" not in full
        assert "58\t庶\t0\t" in verified

        for _ in range(2):
            status, body, _headers = request(
                "/api/page-ignore",
                "POST",
                {"page_index": 58, "ignored": True},
            )
            ignored = json.loads(body)
            assert status == 200 and ignored["ignored"] is True
            assert ignored["progress"] == {"entries": 0, "reviewed": 0}

        status, body, _headers = request("/api/pages")
        pages = json.loads(body)
        assert status == 200 and pages["pages"] == [58]
        assert pages["summary"][0]["ignored"] is True
        assert "page_ignore" in pages["capabilities"]

        status, body, _headers = request("/api/page?page=58&spread=1")
        ignored_page = json.loads(body)
        assert status == 200
        assert ignored_page["pages"][0]["ignored"] is True
        assert ignored_page["pages"][0]["entries"][0]["human"] == ""

        rejected_actions = (
            (
                "/api/correction",
                {
                    "page_index": 58,
                    "band_label": "庶",
                    "entry_index": 0,
                    "transcription": "不应保存",
                    "unreadable": False,
                },
            ),
            ("/api/reocr", {"page": 58, "stream": True}),
            ("/api/recut", {"page": 58, "stream": True}),
        )
        for path, payload in rejected_actions:
            status, body, headers = request(path, "POST", payload)
            assert status == 409
            assert "ignored" in json.loads(body)["error"]
            assert headers.get_content_type() == "application/json"

        status, body, _headers = request("/api/reocr-all", "POST", {})
        job = json.loads(body)["job"]
        assert status == 202
        assert job["total_pages"] == job["total_regions"] == 0

        deadline = time.monotonic() + 2
        while True:
            _status, body, _headers = request("/api/reocr-all")
            job = json.loads(body)["job"]
            if job["status"] == "complete":
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert job["report"]["requested"] == 0

        status, body, _headers = request("/api/export.zip")
        assert status == 200
        with zipfile.ZipFile(BytesIO(body)) as archive:
            full = archive.read("doc.tsv").decode()
            verified = archive.read("doc_verified.tsv").decode()
        assert "0 entries" in full
        assert "卷十" not in full and "\n58\t" not in verified

        # A newly constructed handler reads the same persisted ignore state.
        restarted_handler = _handler_factory(project, "doc", "t", "test")
        restarted = ThreadingHTTPServer(("127.0.0.1", 0), restarted_handler)
        restarted_thread = threading.Thread(target=restarted.serve_forever, daemon=True)
        restarted_thread.start()
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{restarted.server_port}/api/pages", timeout=3
            ) as response:
                after_restart = json.loads(response.read())
            assert after_restart["summary"][0]["ignored"] is True
        finally:
            restarted.shutdown()
            restarted.server_close()
            restarted_thread.join(timeout=2)

        status, body, _headers = request(
            "/api/page-ignore",
            "POST",
            {"page_index": 58, "ignored": False},
        )
        restored = json.loads(body)
        assert status == 200 and restored["ignored"] is False
        assert restored["progress"] == {"entries": 2, "reviewed": 1}

        status, body, _headers = request("/api/page?page=58&spread=1")
        first = json.loads(body)["pages"][0]["entries"][0]
        assert first["human"] == "" and first["machine"] == "卷十"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_review_http_unconfirm_is_idempotent_exact_and_rejects_ignored_pages(
    tmp_path,
):
    project = Project(tmp_path)
    conn = connect(project.db_path)
    init_schema(conn)
    conn.execute(
        "INSERT INTO documents (id, title, source_path, checksum, page_count, "
        "created_at) VALUES ('doc','t','p','c',1,'now')"
    )
    conn.execute(
        "INSERT INTO pages (document_id, page_index, width, height) "
        "VALUES ('doc',58,1400,1000)"
    )
    conn.execute(
        "INSERT INTO processing_runs (document_id, stage, params_json, "
        "params_hash, compute_backend, pipeline_version, git_commit, started_at) "
        "VALUES ('doc','segment','{}','h','local','0','x','now')"
    )
    _segment(conn)
    _add_ocr(conn, ["庶一長子", "庶二次子"], tag="t")
    save_correction(conn, "doc", 58, "庶", 0, "人物校对", role="entry")
    save_correction(conn, "doc", 58, "庶", 0, "页眉校对", role="header")
    save_correction(conn, "doc", 58, "庶", 1, None, unreadable=True, role="entry")
    conn.close()

    handler = _handler_factory(project, "doc", "t", "test")
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def request(path, method="GET", payload=None):
        data = json.dumps(payload or {}).encode() if method == "POST" else None
        req = urllib.request.Request(
            base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            response = urllib.request.urlopen(req, timeout=3)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status, json.loads(response.read()), response.headers

    unconfirm = {
        "action": "unconfirm",
        "page_index": 58,
        "band_label": "庶",
        "entry_index": 0,
        "role": "entry",
    }
    try:
        status, pages, _headers = request("/api/pages")
        assert status == 200
        assert "correction_unconfirm" in pages["capabilities"]
        assert pages["progress"] == {"entries": 2, "reviewed": 2}

        status, result, _headers = request("/api/correction", "POST", unconfirm)
        assert status == 200
        assert result == {
            "ok": True,
            "confirmed": False,
            "removed": True,
            "progress": {"entries": 2, "reviewed": 1},
        }

        status, repeated, _headers = request("/api/correction", "POST", unconfirm)
        assert status == 200
        assert repeated == {
            "ok": True,
            "confirmed": False,
            "removed": False,
            "progress": {"entries": 2, "reviewed": 1},
        }

        status, page, _headers = request("/api/page?page=58&spread=1")
        assert status == 200
        first, second = page["pages"][0]["entries"]
        assert first["human"] is None and first["machine"] == "庶一長子"
        assert first["unreadable"] is False
        assert second["unreadable"] is True

        check = connect(project.db_path)
        same_position = check.execute(
            "SELECT role, transcription FROM human_corrections "
            "WHERE document_id='doc' AND page_index=58 "
            "AND band_label='庶' AND entry_index=0 ORDER BY role"
        ).fetchall()
        assert [(row["role"], row["transcription"]) for row in same_position] == [
            ("header", "页眉校对")
        ]
        check.close()

        status, ignored, _headers = request(
            "/api/page-ignore", "POST", {"page_index": 58, "ignored": True}
        )
        assert status == 200 and ignored["ignored"] is True
        blocked = {**unconfirm, "entry_index": 1}
        status, rejection, headers = request("/api/correction", "POST", blocked)
        assert status == 409
        assert "ignored" in rejection["error"]
        assert headers.get_content_type() == "application/json"

        check = connect(project.db_path)
        assert (
            check.execute(
                "SELECT unreadable FROM human_corrections "
                "WHERE document_id='doc' AND page_index=58 AND band_label='庶' "
                "AND entry_index=1 AND role='entry'"
            ).fetchone()["unreadable"]
            == 1
        )
        check.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_review_http_grid_preview_and_apply_share_the_same_parameters(
    tmp_path,
    monkeypatch,
):
    project = Project(tmp_path)
    conn = connect(project.db_path)
    init_schema(conn)
    conn.execute(
        "INSERT INTO documents (id, title, source_path, checksum, page_count, "
        "created_at) VALUES ('doc','t','p','c',1,'now')"
    )
    conn.execute(
        "INSERT INTO pages (document_id, page_index, width, height) "
        "VALUES ('doc',58,1400,1000)"
    )
    conn.commit()
    conn.close()

    inputs = SimpleNamespace(
        page_index=58,
        document_id="doc",
        pitch=100.0,
        corpus_pitch=101.0,
        used_corpus_pitch=True,
        pitch_confidence=0.91,
        gutters=[320.0, 640.0],
        text_left=100.0,
        text_right=1300.0,
        bands=[],
    )
    plans = []
    applications = []

    class FakePlan:
        def __init__(self, options):
            self.options = options

        def to_dict(self):
            return {
                "page": 58,
                "pitch": self.options["pitch"],
                "phase_offset": self.options["phase_offset"],
                "phase_adjustment": self.options["phase_offset"],
                "base_phase_offset": 0.0,
                "snap": self.options["snap"],
                "text_left": self.options["text_left"],
                "text_right": self.options["text_right"],
                "boundaries": [999.0, 456.0, 111.0],
                "snapped": [False, False, False],
                "manual": [False, True, False],
                "entries_per_band": 2,
                "entries": 0,
            }

    class FakeReport:
        errors = []

        def to_dict(self):
            return {
                "page": 58,
                "entries_per_band": 2,
                "moved": 0,
                "unchanged": 0,
                "created": 0,
                "retired": 0,
                "crops_cut": 0,
                "read": 0,
                "reused": 0,
                "findings_cleared": 0,
                "corrections_rekeyed": 0,
                "corrections_stranded": 0,
                "errors": [],
            }

        def summary(self):
            return "grid applied"

    def fake_plan(_inputs, **options):
        plan = FakePlan(options)
        plans.append(plan)
        return plan

    def fake_apply(_conn, _project, plan, passed_inputs, **options):
        applications.append((plan, passed_inputs, options))
        return FakeReport()

    monkeypatch.setattr(
        "foresight_ocr.review.server.comb_inputs",
        lambda _conn, _project, _document_id, _page: inputs,
    )
    monkeypatch.setattr("foresight_ocr.review.server.plan_comb", fake_plan)
    monkeypatch.setattr("foresight_ocr.review.server.apply_comb", fake_apply)

    handler = _handler_factory(project, "doc", "t", "test")
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def request(path, method="GET", payload=None):
        data = json.dumps(payload or {}).encode() if method == "POST" else None
        req = urllib.request.Request(
            base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=3) as response:
            return response.status, json.loads(response.read())

    expected = {
        "phase_offset": 7.0,
        "pitch": 123.5,
        "snap": False,
        "text_left": 111.0,
        "text_right": 999.0,
        "boundary_overrides": {1: 456.0},
    }
    query = urlencode(
        {
            "page": 58,
            "phase": 7,
            "pitch": 123.5,
            "left": 111,
            "right": 999,
            "snap": 0,
            "manual": json.dumps({"1": 456.0}),
        }
    )
    payload = {
        "page": 58,
        "phase_offset": 7,
        "pitch": 123.5,
        "text_left": 111,
        "text_right": 999,
        "snap": False,
        "boundary_overrides": {"1": 456.0},
        "reocr": False,
    }
    try:
        status, preview = request("/api/comb?" + query)
        assert status == 200
        assert preview["manual"] == [False, True, False]
        assert preview["boundaries"] == [999.0, 456.0, 111.0]

        status, applied = request("/api/recut", "POST", payload)
        assert status == 200
        assert applied["ok"] is True
        assert applied["summary"] == "grid applied"

        assert [plan.options for plan in plans] == [expected, expected]
        assert len(applications) == 1
        applied_plan, applied_inputs, apply_options = applications[0]
        assert applied_plan is plans[1]
        assert applied_inputs is inputs
        assert apply_options["reocr"] is False
        assert apply_options["actor"] == "test"

        check = connect(project.db_path)
        assert check.execute("SELECT COUNT(*) FROM regions").fetchone()[0] == 0
        assert (
            check.execute("SELECT COUNT(*) FROM human_corrections").fetchone()[0] == 0
        )
        check.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
