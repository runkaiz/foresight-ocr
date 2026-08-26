from pathlib import Path

from foresight_ocr.document.pdf import DocumentInfo, PageImageInfo
from foresight_ocr.document.report import write_corpus_analysis
from foresight_ocr.project import Project


def _info() -> DocumentInfo:
    return DocumentInfo(
        document_id="unrelated-volume",
        source_path="source/unrelated-volume.pdf",
        checksum="a" * 64,
        page_count=1,
        pdf_version="1.7",
        creator=None,
        producer=None,
        pages=[
            PageImageInfo(
                page_index=1,
                width=100,
                height=200,
                colorspace="gray",
                encoding="jpeg",
                bits_per_component=8,
                stream_bytes=1234,
                x_ppi=300,
                y_ppi=300,
                image_count=1,
            )
        ],
    )


def test_corpus_report_does_not_invent_document_content(tmp_path: Path) -> None:
    path = write_corpus_analysis(Project(tmp_path), _info())
    report = path.read_text(encoding="utf-8")

    assert "No manual content observations were supplied" in report
    assert "富陽" not in report
    assert "庶富教" not in report


def test_corpus_report_labels_supplied_observations_as_manual(tmp_path: Path) -> None:
    path = write_corpus_analysis(
        Project(tmp_path),
        _info(),
        manual_observations=["Header reads `Example`.", ""],
    )
    report = path.read_text(encoding="utf-8")

    assert "supplied by a person" in report
    assert "- Header reads `Example`." in report
