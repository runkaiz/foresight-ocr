"""The graph CLI selects only live, non-ignored human-approved sources."""

from __future__ import annotations

import json

from foresight_ocr.cli.main import graph
from foresight_ocr.document.profile import DocumentProfile, save_profile
from foresight_ocr.persistence import connect, init_schema
from foresight_ocr.project import Project

DOC = "test-doc"


def _add_region_with_ocr(conn, page_index: int, uid: str, text: str) -> None:
    cur = conn.execute(
        """INSERT INTO regions
           (region_uid, document_id, page_index, bbox_json, geometry_hash,
            band_label, band_ordinal, reading_order, entry_index,
            created_at, updated_at)
           VALUES (?,?,?,?,'geometry','庶',0,0,0,'now','now')""",
        (uid, DOC, page_index, json.dumps([0, 0, 10, 10])),
    )
    conn.execute(
        "INSERT INTO ocr_candidates (region_id, ocr_run_id, transcription) "
        "VALUES (?,?,?)",
        (cur.lastrowid, 1, text),
    )


def test_graph_ignores_pages_and_honours_an_explicit_blank(tmp_path, monkeypatch):
    project = Project(tmp_path)
    conn = connect(project.db_path)
    init_schema(conn)
    save_profile(
        project.configs,
        DocumentProfile(
            document_id=DOC,
            band_labels=["庶"],
            generation_chain=["允", "庶"],
            bands_per_page=1,
        ),
    )
    conn.execute(
        "INSERT INTO documents VALUES (?,?,?,?,?,?)",
        (DOC, DOC, "source.pdf", "checksum", 3, "now"),
    )
    conn.executemany(
        "INSERT INTO pages (document_id, page_index, width, height, ignored) "
        "VALUES (?,?,10,10,?)",
        [(DOC, 1, 1), (DOC, 2, 0)],
    )
    conn.execute("INSERT INTO models VALUES ('test-model','test','1','test')")
    conn.execute(
        "INSERT INTO ocr_runs (id, model_id, input_variant, tag) "
        "VALUES (1,'test-model','original','')"
    )
    _add_region_with_ocr(conn, 1, "ignored-page", "庶一允一長子")
    _add_region_with_ocr(conn, 2, "explicit-blank", "庶二允二次子")
    # A missing pages row is not an ignore instruction. This guards imported
    # or partially inspected data while the LEFT JOIN keeps it eligible.
    _add_region_with_ocr(conn, 99, "missing-page-row", "庶三允三三子")
    # Simulate a readable legacy NULL that escaped migration. Membership in
    # human_corrections still means the reviewer deliberately cleared the OCR.
    conn.execute(
        """INSERT INTO human_corrections
           (document_id, page_index, band_label, entry_index, role,
            transcription, unreadable, corrected_by, corrected_at)
           VALUES (?,2,'庶',0,'entry',NULL,0,'test','now')""",
        (DOC,),
    )
    conn.commit()

    monkeypatch.chdir(tmp_path)
    graph(DOC, tag=None, export=False)

    entries = conn.execute(
        "SELECT region_uid, source, text, own_id FROM parsed_entries "
        "WHERE document_id = ? ORDER BY page_index",
        (DOC,),
    ).fetchall()
    assert [row["region_uid"] for row in entries] == [
        "explicit-blank",
        "missing-page-row",
    ]
    assert dict(entries[0]) == {
        "region_uid": "explicit-blank",
        "source": "human",
        "text": "",
        "own_id": None,
    }
    assert [
        row["person_key"]
        for row in conn.execute(
            "SELECT person_key FROM persons WHERE document_id = ?", (DOC,)
        )
    ] == ["庶:3"]
