"""The ruled first chart page is structure, not a periodic entry lattice."""

import sqlite3

from foresight_ocr.persistence.db import init_schema
from foresight_ocr.regions import store
from foresight_ocr.regions.reconcile import Proposal, reconcile_page
from foresight_ocr.review.data import page_entries, progress, save_correction
from foresight_ocr.segmentation.entries import segment_structured_page

OVERRIDE = {
    "entry_boundaries": [1761.0, 1482.5, 1106.5, 730.5, 437.5, 14.5],
    "band_headers": [
        {"kind": "section_title", "x0": 1761.0, "x1": 1872.0},
        {"kind": "generation_title", "x0": 1872.0, "x1": 2044.5},
    ],
    "page_headers": [
        {
            "kind": "document_title",
            "band_index": 0,
            "x0": 2044.5,
            "x1": 2237.5,
            "y0": 0.0,
            "y1": 2002.0,
        }
    ],
}


def test_structured_page_separates_people_section_generation_and_book_title():
    regions = segment_structured_page(
        2,
        [(0, 0.0, 1022.0), (1, 1022.0, 2002.0), (2, 2002.0, 3025.0)],
        OVERRIDE,
        2300,
        {"tight": 0.0},
    )

    people = [r for r in regions if r.role == "entry"]
    headers = [r for r in regions if r.role == "header"]
    assert len(people) == 15
    assert len(headers) == 7
    assert people[0].bbox == [1482.5, 0.0, 1761.0, 1022.0]
    assert any(r.bbox == [1761.0, 0.0, 1872.0, 1022.0] for r in headers)
    assert any(r.bbox == [1872.0, 0.0, 2044.5, 1022.0] for r in headers)
    assert any(r.bbox == [2044.5, 0.0, 2237.5, 2002.0] for r in headers)


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    conn.execute("INSERT INTO documents VALUES ('doc','doc','p','c',1,'now')")
    return conn


def test_reconcile_can_split_a_mixed_header_back_into_a_person_and_a_header():
    conn = _db()
    mixed = store.create_region(
        conn,
        "doc",
        2,
        [1482.5, 0.0, 1872.0, 1022.0],
        band_label="庶",
        band_ordinal=0,
        reading_order=1,
        entry_index=1,
        role="header",
    )
    report = reconcile_page(
        conn,
        "doc",
        2,
        [
            Proposal(0, "庶", 0, [1482.5, 0.0, 1761.0, 1022.0], role="entry"),
            Proposal(0, "庶", -1, [1761.0, 0.0, 1872.0, 1022.0], role="header"),
        ],
    )

    assert report.moved == 1 and report.created == 1
    assert store.get(conn, mixed.region_uid).role == "entry"
    live = store.for_page(conn, "doc", 2)
    assert [(r.role, r.reading_order) for r in live] == [
        ("header", -1),
        ("entry", 0),
    ]


def test_headers_are_not_parsed_or_counted_as_people_and_keep_their_role():
    conn = _db()
    person = store.create_region(
        conn,
        "doc",
        2,
        [1482.5, 0.0, 1761.0, 1022.0],
        band_label="庶",
        band_ordinal=0,
        reading_order=0,
        entry_index=0,
    )
    header = store.create_region(
        conn,
        "doc",
        2,
        [1761.0, 0.0, 1872.0, 1022.0],
        band_label="庶",
        band_ordinal=0,
        reading_order=-1,
        entry_index=-1,
        role="header",
    )
    save_correction(conn, "doc", 2, "庶", -1, "庶字第", role="header")

    rows = page_entries(conn, "doc", 2)
    title = next(r for r in rows if r.region_uid == header.region_uid)
    assert title.header_kind == "section_title"
    assert title.own_id == "庶字第"
    assert title.parent is None and title.birth_order is None
    assert progress(conn, "doc") == {"entries": 1, "reviewed": 0}

    save_correction(conn, "doc", 2, "庶", 0, "庶一允一長子")
    assert progress(conn, "doc") == {"entries": 1, "reviewed": 1}
    assert person.id is not None
