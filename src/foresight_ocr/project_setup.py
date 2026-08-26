"""Safe project creation and source-document import for desktop clients."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from foresight_ocr import __version__
from foresight_ocr.persistence import connect, init_schema
from foresight_ocr.project import PROJECT_MARKER, Project, validate_document_id
from foresight_ocr.provenance import sha256_file

PROJECT_FORMAT_VERSION = 1


class ProjectSetupError(ValueError):
    """A requested project mutation is unsafe or incompatible."""


@dataclass(frozen=True)
class ImportedPDF:
    document_id: str
    source: Path
    checksum: str
    copied: bool


def _write_marker_atomic(marker: Path, payload: dict[str, object]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{marker.parent.name}.foresight-project-",
        suffix=".tmp",
        dir=marker.parent.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(marker)
    finally:
        temporary.unlink(missing_ok=True)


def _visible_children(root: Path) -> list[Path]:
    return sorted(
        (item for item in root.iterdir() if item.name not in {".DS_Store"}),
        key=lambda item: item.name,
    )


def create_project(root: Path, name: str | None = None) -> Project:
    """Create or reopen a portable foresight-ocr data project.

    A non-empty unrelated directory is never adopted implicitly. That guard is
    important for a native save panel: selecting the wrong folder must not
    scatter source, configuration, and database files through it.
    """

    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    marker = root / PROJECT_MARKER
    if marker.is_file():
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProjectSetupError(f"invalid project marker: {marker}") from exc
        version = payload.get("format_version")
        if version != PROJECT_FORMAT_VERSION:
            raise ProjectSetupError(
                f"unsupported project format {version!r}; expected "
                f"{PROJECT_FORMAT_VERSION}"
            )
    else:
        existing = _visible_children(root)
        if existing:
            rendered = ", ".join(item.name for item in existing[:3])
            if len(existing) > 3:
                rendered += ", …"
            raise ProjectSetupError(
                f"project directory is not empty or already recognizable: {rendered}"
            )
        project_name = (name or root.name).strip()
        if not project_name:
            raise ProjectSetupError("project name must not be empty")
        payload = {
            "format_version": PROJECT_FORMAT_VERSION,
            "name": project_name,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "created_with": __version__,
        }
        _write_marker_atomic(marker, payload)

    project = Project(root)
    for directory in (project.source, project.configs, project.artifacts, project.docs):
        directory.mkdir(parents=True, exist_ok=True)
    connection = connect(project.db_path)
    try:
        init_schema(connection)
    finally:
        connection.close()
    return project


def import_pdf_source(
    project: Project,
    source: Path,
    document_id: str | None = None,
) -> ImportedPDF:
    """Preserve a PDF inside the project without overwriting different bytes."""

    source = source.expanduser().resolve()
    if not source.is_file():
        raise ProjectSetupError(f"PDF does not exist: {source}")
    if source.suffix.casefold() != ".pdf":
        raise ProjectSetupError(f"source is not a PDF: {source.name}")
    normalized = unicodedata.normalize("NFC", document_id or source.stem)
    try:
        normalized = validate_document_id(normalized)
    except ValueError as exc:
        raise ProjectSetupError(str(exc)) from exc

    project.source.mkdir(parents=True, exist_ok=True)
    destination = project.source / f"{normalized}.pdf"
    checksum = sha256_file(source)
    if destination.exists():
        if sha256_file(destination) != checksum:
            raise ProjectSetupError(
                f"document {normalized!r} already exists with different PDF bytes"
            )
        return ImportedPDF(normalized, destination, checksum, copied=False)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{normalized}.", suffix=".importing", dir=project.source
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copy2(source, temporary)
        if sha256_file(temporary) != checksum:
            raise ProjectSetupError("copied PDF failed checksum verification")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return ImportedPDF(normalized, destination, checksum, copied=True)
