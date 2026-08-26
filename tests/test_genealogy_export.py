"""Genealogy exports retain optional free-form person information."""

from __future__ import annotations

import sqlite3

from foresight_ocr.context import set_profile
from foresight_ocr.document.profile import DocumentProfile
from foresight_ocr.genealogy.export import write_gedcom, write_tsv


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE pages (
            document_id TEXT,
            page_index INTEGER,
            ignored INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (document_id, page_index)
        );
        CREATE TABLE parsed_entries (
            id INTEGER PRIMARY KEY,
            document_id TEXT,
            page_index INTEGER,
            leftover TEXT
        );
        CREATE TABLE persons (
            id INTEGER PRIMARY KEY,
            document_id TEXT,
            person_key TEXT,
            generation TEXT,
            own_id TEXT,
            own_value INTEGER,
            parsed_entry_id INTEGER,
            father_person_id INTEGER,
            father_key TEXT,
            father_name TEXT,
            birth_order TEXT,
            link_status TEXT
        );
        INSERT INTO pages VALUES ('doc', 1, 0);
        INSERT INTO parsed_entries VALUES
            (1, 'doc', 1, '生於光緒甲辰年\n二月初九日辰時');
        INSERT INTO persons VALUES
            (1, 'doc', '教:2115', '教', '教二千百十五', 2115, 1,
             NULL, NULL, NULL, '長子', 'root');
        """
    )
    return conn


def _sorting_db():
    conn = _db()
    conn.execute("DELETE FROM persons")
    conn.execute("DELETE FROM parsed_entries")
    conn.executemany(
        "INSERT INTO persons VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                32,
                "doc",
                "教:10",
                "教",
                "教十",
                10,
                None,
                None,
                "富:10",
                None,
                "次子",
                "resolved",
            ),
            (
                21,
                "doc",
                "富:2",
                "富",
                "富二",
                2,
                None,
                None,
                "庶:2",
                None,
                "長子",
                "resolved",
            ),
            (
                12,
                "doc",
                "庶:10",
                "庶",
                "庶十",
                10,
                None,
                None,
                None,
                None,
                "長子",
                "root",
            ),
            (
                22,
                "doc",
                "富:10",
                "富",
                "富十",
                10,
                None,
                None,
                "庶:10",
                None,
                "長子",
                "resolved",
            ),
            (
                11,
                "doc",
                "庶:2",
                "庶",
                "庶二",
                2,
                None,
                None,
                None,
                None,
                "長子",
                "root",
            ),
            (
                30,
                "doc",
                "教:1",
                "教",
                "教一",
                1,
                None,
                None,
                "富:10",
                None,
                "長子",
                "resolved",
            ),
            (
                10,
                "doc",
                "庶:1",
                "庶",
                "庶一",
                1,
                None,
                None,
                None,
                None,
                "長子",
                "root",
            ),
            (
                20,
                "doc",
                "富:1",
                "富",
                "富一",
                1,
                None,
                None,
                "庶:1",
                None,
                "長子",
                "resolved",
            ),
            (
                31,
                "doc",
                "教:2",
                "教",
                "教二",
                2,
                None,
                None,
                "富:2",
                None,
                "長子",
                "resolved",
            ),
        ],
    )
    conn.commit()
    return conn


def test_tsv_has_additional_information_column(tmp_path):
    out = tmp_path / "persons.tsv"
    write_tsv(_db(), "doc", out)

    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert "\tadditional_info\t" in lines[1]
    assert "生於光緒甲辰年 二月初九日辰時" in lines[2]


def test_gedcom_writes_additional_information_as_a_note(tmp_path):
    out = tmp_path / "doc.ged"
    write_gedcom(_db(), "doc", out)

    body = out.read_text(encoding="utf-8")
    assert "1 NOTE 生於光緒甲辰年 二月初九日辰時" in body


def test_genealogy_exports_keep_stepson_marker_as_birth_order(tmp_path):
    conn = _db()
    conn.execute("UPDATE persons SET birth_order = '繼子' WHERE id = 1")
    conn.commit()
    tsv = tmp_path / "persons.tsv"
    gedcom = tmp_path / "doc.ged"

    write_tsv(conn, "doc", tsv)
    write_gedcom(conn, "doc", gedcom)

    assert "\t繼子\t" in tsv.read_text(encoding="utf-8")
    assert "1 _ORDER 繼子" in gedcom.read_text(encoding="utf-8")


def test_genealogy_exports_follow_profile_and_numeric_order(tmp_path):
    set_profile(
        DocumentProfile(
            document_id="other",
            band_labels=["富", "教", "庶"],
            generation_chain=["富", "教", "庶"],
            bands_per_page=3,
        )
    )
    conn = _sorting_db()
    tsv = tmp_path / "persons.tsv"
    gedcom = tmp_path / "doc.ged"

    labels = ["庶", "富", "教"]
    write_tsv(conn, "doc", tsv, generation_labels=labels)
    write_gedcom(conn, "doc", gedcom, generation_labels=labels)

    rows = [
        line.split("\t") for line in tsv.read_text(encoding="utf-8").splitlines()[2:]
    ]
    expected = [
        "庶一",
        "庶二",
        "庶十",
        "富一",
        "富二",
        "富十",
        "教一",
        "教二",
        "教十",
    ]
    assert [row[2] for row in rows] == expected

    gedcom_lines = gedcom.read_text(encoding="utf-8").splitlines()
    assert [
        line.removeprefix("1 NAME ")
        for line in gedcom_lines
        if line.startswith("1 NAME ")
    ] == expected
    # Family records use the same semantic/numeric father ordering; text order
    # would incorrectly place :10 before :2.
    assert [
        line.removeprefix("1 HUSB ")
        for line in gedcom_lines
        if line.startswith("1 HUSB ")
    ] == ["@I10@", "@I11@", "@I12@", "@I21@", "@I22@"]


def test_genealogy_exports_exclude_ignored_pages_without_dangling_links(tmp_path):
    conn = _db()
    conn.execute("DELETE FROM persons")
    conn.execute("DELETE FROM parsed_entries")
    conn.execute("DELETE FROM pages")
    conn.executemany(
        "INSERT INTO pages VALUES (?,?,?)",
        [("doc", 1, 1), ("doc", 2, 0)],
    )
    # Page 99 intentionally has no pages row. Older/imported region data can
    # have that shape, and absence must not be mistaken for an ignored page.
    conn.executemany(
        "INSERT INTO parsed_entries VALUES (?,?,?,NULL)",
        [
            (1, "doc", 1),
            (2, "doc", 2),
            (3, "doc", 99),
            (4, "doc", 1),
        ],
    )
    conn.executemany(
        "INSERT INTO persons VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (1, "doc", "庶:1", "庶", "庶一", 1, 1, None, None, None, "長子", "root"),
            (2, "doc", "富:1", "富", "富一", 1, 2, 1, "庶:1", None, "長子", "resolved"),
            (3, "doc", "庶:2", "庶", "庶二", 2, 3, None, None, None, "次子", "root"),
            (4, "doc", "富:2", "富", "富二", 2, 4, 3, "庶:2", None, "次子", "resolved"),
        ],
    )
    conn.commit()

    tsv = tmp_path / "persons.tsv"
    gedcom = tmp_path / "doc.ged"
    labels = ["庶", "富"]
    assert write_tsv(conn, "doc", tsv, generation_labels=labels) == 2
    assert write_gedcom(conn, "doc", gedcom, generation_labels=labels) == (2, 1)

    tsv_rows = tsv.read_text(encoding="utf-8").splitlines()[2:]
    assert [row.split("\t")[0] for row in tsv_rows] == ["庶:2", "富:1"]

    lines = gedcom.read_text(encoding="utf-8").splitlines()
    individuals = {line.split()[1] for line in lines if line.endswith(" INDI")}
    assert individuals == {"@I2@", "@I3@"}
    assert not any(line.startswith("1 HUSB ") for line in lines)
    assert [line for line in lines if line.startswith("1 CHIL ")] == ["1 CHIL @I2@"]
    # The kept child still has a valid family, but never points at its ignored
    # father's omitted INDI record. The ignored child creates no family for I3.
    families = {line.split()[1] for line in lines if line.endswith(" FAM")}
    assert families == {"@F1@"}
    assert all(
        line.split()[-1] in individuals
        for line in lines
        if line.startswith(("1 HUSB ", "1 CHIL "))
    )
    assert all(
        line.split()[-1] in families
        for line in lines
        if line.startswith(("1 FAMC ", "1 FAMS "))
    )
