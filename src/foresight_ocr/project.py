"""Project paths. One place that knows where artifacts live."""

from __future__ import annotations

import tomllib
import unicodedata
from dataclasses import dataclass
from pathlib import Path

PROJECT_MARKER = "foresight-ocr.project.json"

_WINDOWS_DEVICES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_INVALID_FILENAME_CHARACTERS = set('<>:"/\\|?*')


class InvalidDocumentId(ValueError):
    """A document identifier cannot be represented as a portable filename."""


def validate_document_id(value: str) -> str:
    """Return a portable, collision-resistant document filename component."""
    if not value:
        raise InvalidDocumentId("document id must not be empty")
    if value != value.strip() or value.startswith(".") or value.endswith((".", " ")):
        raise InvalidDocumentId("document id must not start or end with whitespace/dot")
    if unicodedata.normalize("NFC", value) != value:
        raise InvalidDocumentId("document id must use NFC-normalized Unicode")
    if len(value.encode("utf-8")) > 200:
        raise InvalidDocumentId("document id is too long (maximum 200 UTF-8 bytes)")
    if value in {".", ".."} or any(
        character in _INVALID_FILENAME_CHARACTERS or ord(character) < 32
        for character in value
    ):
        raise InvalidDocumentId(
            "document id contains a path separator or non-portable filename character"
        )
    if value.split(".", 1)[0].upper() in _WINDOWS_DEVICES:
        raise InvalidDocumentId("document id is a reserved Windows device name")
    return value


@dataclass(frozen=True)
class Project:
    root: Path

    @classmethod
    def discover(cls, start: Path | None = None) -> "Project":
        p = (start or Path.cwd()).resolve()
        for candidate in [p, *p.parents]:
            if (candidate / PROJECT_MARKER).is_file():
                return cls(candidate)
            artifacts = candidate / "artifacts"
            if (artifacts / "foresight-ocr.db").is_file() or (
                artifacts / "familyocr.db"
            ).is_file():
                return cls(candidate)
            pyproject = candidate / "pyproject.toml"
            if pyproject.is_file():
                try:
                    document = tomllib.loads(pyproject.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
                    continue
                if document.get("project", {}).get("name") == "foresight-ocr":
                    return cls(candidate)
        return cls(p)

    @property
    def artifacts(self) -> Path:
        return self.root / "artifacts"

    @property
    def marker_path(self) -> Path:
        return self.root / PROJECT_MARKER

    @property
    def source(self) -> Path:
        return self.root / "source"

    @property
    def docs(self) -> Path:
        return self.root / "docs"

    @property
    def configs(self) -> Path:
        return self.root / "configs"

    @property
    def db_path(self) -> Path:
        preferred = self.artifacts / "foresight-ocr.db"
        legacy = self.artifacts / "familyocr.db"
        if not preferred.exists() and legacy.exists():
            return legacy
        return preferred

    def pages_dir(self, document_id: str, role: str) -> Path:
        return self.artifacts / "pages" / validate_document_id(document_id) / role

    def analysis_dir(self, document_id: str, name: str) -> Path:
        return self.artifacts / "analysis" / validate_document_id(document_id) / name

    def crops_dir(self, document_id: str) -> Path:
        return self.artifacts / "crops" / validate_document_id(document_id)
