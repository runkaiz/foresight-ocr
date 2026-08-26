from __future__ import annotations

import importlib
import json
import sqlite3
from itertools import pairwise
from pathlib import Path

import pytest
import yaml
from PIL import Image, ImageDraw
from typer.testing import CliRunner

from foresight_ocr.cli.main import app
from foresight_ocr.persistence import connect_readonly
from foresight_ocr.provenance import sha256_file

cli_main = importlib.import_module("foresight_ocr.cli.main")


def _sample_pdf(path: Path) -> None:
    image = Image.new("RGB", (320, 480), "white")
    drawing = ImageDraw.Draw(image)
    drawing.rectangle((20, 20, 300, 460), outline="black", width=4)
    drawing.line((20, 170, 300, 170), fill="black", width=3)
    image.save(path, "PDF", resolution=150)


def _structured_pdf(path: Path) -> None:
    width, height, margin = 2424, 3744, 100
    image = Image.new("RGB", (width, height), (235, 235, 235))
    drawing = ImageDraw.Draw(image)
    x0, x1 = margin, width - margin
    y0, y1 = margin * 4, height - margin * 2
    band_edges = [y0, y0 + (y1 - y0) // 3, y0 + 2 * (y1 - y0) // 3, y1]
    for y in band_edges:
        drawing.line((x0, y, x1, y), fill=(20, 20, 20), width=3)
    drawing.line((x0, y0, x0, y1), fill=(20, 20, 20), width=3)
    drawing.line((x1, y0, x1, y1), fill=(20, 20, 20), width=3)
    for top, bottom in pairwise(band_edges):
        for x in range(x0 + 100, x1 - 50, 180):
            drawing.rectangle((x, top + 80, x + 24, bottom - 80), fill=(30, 30, 30))
    image.save(path, "PDF", resolution=300)


def test_inspect_and_extract_create_a_portable_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    pdf = tmp_path / "sample.pdf"
    _sample_pdf(pdf)

    inspected = runner.invoke(app, ["inspect", str(pdf), "--id", "demo", "--no-report"])
    assert inspected.exit_code == 0, inspected.output
    assert "320x480" in inspected.output
    assert (tmp_path / "configs" / "profile_demo.yaml").is_file()
    assert (
        tmp_path / "artifacts" / "analysis" / "demo" / "inspect" / "structure.json"
    ).is_file()

    extracted = runner.invoke(app, ["extract", "demo"])
    assert extracted.exit_code == 0, extracted.output
    original = tmp_path / "artifacts" / "pages" / "demo" / "original" / "p0001.jpg"
    decoded = tmp_path / "artifacts" / "pages" / "demo" / "decoded" / "p0001.png"
    assert original.is_file() and decoded.is_file()
    original_checksum = sha256_file(original)
    with Image.open(decoded) as image:
        assert image.size == (320, 480)

    repeated = runner.invoke(app, ["extract", "demo"])
    assert repeated.exit_code == 0, repeated.output
    assert sha256_file(original) == original_checksum

    with sqlite3.connect(tmp_path / "artifacts" / "foresight-ocr.db") as connection:
        assert connection.execute(
            "SELECT page_count FROM documents WHERE id = 'demo'"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM page_assets WHERE document_id = 'demo'"
        ).fetchone() == (2,)
        assert connection.execute(
            "SELECT COUNT(*) FROM processing_runs "
            "WHERE document_id = 'demo' AND status = 'completed'"
        ).fetchone() == (3,)


def test_documents_json_is_a_machine_readable_project_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    pdf = tmp_path / "宗譜.pdf"
    _sample_pdf(pdf)

    assert (
        runner.invoke(
            app,
            ["inspect", str(pdf), "--id", "宗譜", "--no-report"],
        ).exit_code
        == 0
    )
    assert runner.invoke(app, ["extract", "宗譜"]).exit_code == 0
    database = tmp_path / "artifacts" / "foresight-ocr.db"
    with sqlite3.connect(database) as connection:
        logical_before = "\n".join(connection.iterdump())

    readonly = connect_readonly(database)
    try:
        assert readonly.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            readonly.execute("UPDATE documents SET title = 'changed'")
    finally:
        readonly.close()

    result = runner.invoke(app, ["documents", "--json"])

    assert result.exit_code == 0, result.output
    with sqlite3.connect(database) as connection:
        assert "\n".join(connection.iterdump()) == logical_before
    manifest = json.loads(result.output)
    assert manifest == {
        "protocol_version": 1,
        "project_root": str(tmp_path),
        "documents": [
            {
                "id": "宗譜",
                "title": "宗譜",
                "page_count": 1,
                "reviewable": True,
                "entries": 0,
                "reviewed": 0,
                "tag": None,
            }
        ],
    }


def test_native_project_commands_create_and_import_without_a_checkout_venv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    project_root = tmp_path / "章氏宗譜"
    source = tmp_path / "待導入.pdf"
    _sample_pdf(source)

    initialized = runner.invoke(
        app,
        ["project", "init", str(project_root), "--name", "章氏宗譜", "--json"],
    )
    assert initialized.exit_code == 0, initialized.output
    assert json.loads(initialized.output)["project_root"] == str(project_root)

    monkeypatch.chdir(project_root)
    imported = runner.invoke(
        app,
        ["project", "import", str(source), "--no-report", "--json"],
    )
    assert imported.exit_code == 0, imported.output
    payload = json.loads(imported.output)
    assert payload["document_id"] == "待導入"
    assert payload["page_count"] == 1
    assert payload["copied"] is True
    assert (project_root / "source" / "待導入.pdf").read_bytes() == source.read_bytes()
    with sqlite3.connect(project_root / "artifacts" / "foresight-ocr.db") as connection:
        assert connection.execute(
            "SELECT id, page_count FROM documents"
        ).fetchone() == ("待導入", 1)


def test_project_prepare_emits_seven_runtime_driven_stages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    project_root = tmp_path / "project"
    source = tmp_path / "book.pdf"
    _sample_pdf(source)
    assert runner.invoke(app, ["project", "init", str(project_root)]).exit_code == 0
    monkeypatch.chdir(project_root)
    assert (
        runner.invoke(
            app,
            ["project", "import", str(source), "--no-report", "--json"],
        ).exit_code
        == 0
    )

    called: list[str] = []
    monkeypatch.setattr(
        cli_main, "extract", lambda *_args: called.append("extract_pages")
    )
    monkeypatch.setattr(
        cli_main, "normalize", lambda *_args: called.append("normalize_frames")
    )
    monkeypatch.setattr(
        cli_main, "layout", lambda *_args: called.append("detect_layout")
    )
    monkeypatch.setattr(
        cli_main, "segment", lambda *_args: called.append("segment_regions")
    )

    def fake_recognize(*_args):
        called.append("initial_ocr")
        return {
            "requested": 4,
            "read": 4,
            "recognized": 4,
            "reused": 0,
            "errors": [],
        }

    monkeypatch.setattr(cli_main, "recognize", fake_recognize)

    prepared = runner.invoke(
        app,
        ["project", "prepare", "book", "--backend", "ppocr_v5", "--events"],
    )

    assert prepared.exit_code == 0, prepared.output
    messages = [json.loads(line) for line in prepared.output.splitlines()]
    stage_events = [row for row in messages if row["type"] == "project_prepare"]
    assert [(row["stage"], row["status"]) for row in stage_events] == [
        (stage, status)
        for stage in (
            "preserve_pdf",
            "inspect_pdf",
            "extract_pages",
            "normalize_frames",
            "detect_layout",
            "segment_regions",
            "initial_ocr",
        )
        for status in ("started", "completed")
    ]
    assert [row["index"] for row in stage_events[::2]] == list(range(1, 8))
    assert all(row["total"] == 7 for row in stage_events)
    assert called == [
        "extract_pages",
        "normalize_frames",
        "detect_layout",
        "segment_regions",
        "initial_ocr",
    ]
    assert messages[-1]["type"] == "project_prepare_result"
    assert messages[-1]["ready"] is True


def test_recognize_events_are_valid_ndjson_when_no_regions_need_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    project_root = tmp_path / "project"
    source = tmp_path / "empty.pdf"
    _sample_pdf(source)
    assert runner.invoke(app, ["project", "init", str(project_root)]).exit_code == 0
    monkeypatch.chdir(project_root)
    assert (
        runner.invoke(
            app,
            ["project", "import", str(source), "--no-report", "--json"],
        ).exit_code
        == 0
    )

    result = runner.invoke(
        app,
        ["recognize", "empty", "--backend", "ppocr_v5", "--events"],
    )

    assert result.exit_code == 0, result.output
    messages = [json.loads(line) for line in result.output.splitlines()]
    assert messages == [
        {
            "type": "recognition",
            "document_id": "empty",
            "stage": "queued",
            "completed": 0,
            "total": 0,
        },
        {
            "type": "recognition_result",
            "ok": True,
            "document_id": "empty",
            "requested": 0,
            "read": 0,
            "recognized": 0,
            "reused": 0,
            "errors": [],
            "backend": "ppocr_v5",
            "variant": "watermark",
            "force": False,
        },
    ]


@pytest.mark.parametrize("document_id", ["../escape", r"..\\escape", "CON"])
def test_inspect_rejects_nonportable_document_ids_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, document_id: str
) -> None:
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    pdf = tmp_path / "sample.pdf"
    _sample_pdf(pdf)

    result = runner.invoke(
        app, ["inspect", str(pdf), "--id", document_id, "--no-report"]
    )

    assert result.exit_code == 2, result.output
    assert "document id" in result.output
    assert not (tmp_path / "artifacts").exists()
    assert not (tmp_path / "configs").exists()


def test_extract_refuses_a_tampered_archival_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    pdf = tmp_path / "sample.pdf"
    _sample_pdf(pdf)
    assert (
        runner.invoke(
            app, ["inspect", str(pdf), "--id", "demo", "--no-report"]
        ).exit_code
        == 0
    )
    assert runner.invoke(app, ["extract", "demo"]).exit_code == 0

    original = tmp_path / "artifacts" / "pages" / "demo" / "original" / "p0001.jpg"
    original.write_bytes(b"tampered")

    refused = runner.invoke(app, ["extract", "demo"])
    assert refused.exit_code == 1
    assert isinstance(refused.exception, RuntimeError)
    assert "different checksum" in str(refused.exception)
    with sqlite3.connect(tmp_path / "artifacts" / "foresight-ocr.db") as connection:
        assert (
            connection.execute(
                "SELECT status, finished_at FROM processing_runs "
                "WHERE document_id = 'demo' AND stage = 'extract' ORDER BY id DESC"
            ).fetchone()[0]
            == "failed"
        )

    forced = runner.invoke(app, ["extract", "demo", "--force"])
    assert forced.exit_code == 0, forced.output
    assert original.read_bytes() != b"tampered"


def test_restore_samples_page_one_and_writes_strict_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    pdf = tmp_path / "sample.pdf"
    _sample_pdf(pdf)
    assert (
        runner.invoke(
            app, ["inspect", str(pdf), "--id", "demo", "--no-report"]
        ).exit_code
        == 0
    )
    assert runner.invoke(app, ["extract", "demo"]).exit_code == 0

    restored = runner.invoke(app, ["restore", "demo", "--sample", "1"])
    assert restored.exit_code == 0, restored.output
    scores_path = tmp_path / "artifacts/analysis/demo/watermark/scores.json"
    raw = scores_path.read_text(encoding="utf-8")
    assert "NaN" not in raw
    scores = json.loads(raw)
    assert scores["pages"] == [1]
    assert scores["variants"]["gray"]["watermark_residual"] is None


def test_structural_pipeline_reaches_idempotent_segmented_crops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    pdf = tmp_path / "ruled.pdf"
    _structured_pdf(pdf)

    commands = [
        ["inspect", str(pdf), "--id", "ruled", "--no-report"],
        ["extract", "ruled"],
        ["normalize", "ruled"],
        ["layout", "ruled"],
        ["segment", "ruled", "--variants", "original", "--contexts", "tight"],
    ]
    for command in commands:
        result = runner.invoke(app, command)
        assert result.exit_code == 0, f"{command}: {result.output}"

    canonical = json.loads(
        (tmp_path / "artifacts/pages/ruled/normalized/canonical_space.json").read_text()
    )
    assert canonical == {"width": 2224, "height": 3144}
    template = yaml.safe_load((tmp_path / "configs/template_ruled.yaml").read_text())
    assert template["band_count"] == 3
    assert template["column_pitch"] == pytest.approx(180, abs=2)

    crops = sorted((tmp_path / "artifacts/crops/ruled/original").glob("*_tight.png"))
    assert len(crops) == 36
    with sqlite3.connect(tmp_path / "artifacts/foresight-ocr.db") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM regions WHERE document_id = 'ruled'"
        ).fetchone() == (36,)
        assert connection.execute(
            "SELECT COUNT(*) FROM bands b JOIN page_layouts p ON p.id = b.page_layout_id "
            "WHERE p.document_id = 'ruled'"
        ).fetchone() == (3,)

    normalized = tmp_path / "artifacts/pages/ruled/normalized/p0001.png"
    normalized_bytes = normalized.read_bytes()
    template_path = tmp_path / "configs/template_ruled.yaml"
    template_bytes = template_path.read_bytes()
    Image.new("RGB", (2224, 3144), "white").save(normalized)

    failed_layout = runner.invoke(app, ["layout", "ruled"])
    assert failed_layout.exit_code == 1
    assert isinstance(failed_layout.exception, RuntimeError)
    assert "no measurable entry-column pitch" in str(failed_layout.exception)
    assert template_path.read_bytes() == template_bytes
    with sqlite3.connect(tmp_path / "artifacts/foresight-ocr.db") as connection:
        assert connection.execute(
            "SELECT status FROM processing_runs "
            "WHERE document_id = 'ruled' AND stage = 'layout' ORDER BY id DESC LIMIT 1"
        ).fetchone() == ("failed",)

    normalized.write_bytes(normalized_bytes)
    after_failed_layout = runner.invoke(
        app,
        ["segment", "ruled", "--variants", "original", "--contexts", "tight"],
    )
    assert after_failed_layout.exit_code == 0, after_failed_layout.output
    assert "all 36 matched" in after_failed_layout.output
    with sqlite3.connect(tmp_path / "artifacts/foresight-ocr.db") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM regions WHERE document_id = 'ruled'"
        ).fetchone() == (36,)
