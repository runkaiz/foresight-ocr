"""Project paths. One place that knows where artifacts live."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Project:
    root: Path

    @classmethod
    def discover(cls, start: Path | None = None) -> "Project":
        p = (start or Path.cwd()).resolve()
        for candidate in [p, *p.parents]:
            if (candidate / "pyproject.toml").exists():
                return cls(candidate)
        return cls(p)

    @property
    def artifacts(self) -> Path:
        return self.root / "artifacts"

    @property
    def docs(self) -> Path:
        return self.root / "docs"

    @property
    def configs(self) -> Path:
        return self.root / "configs"

    @property
    def db_path(self) -> Path:
        return self.artifacts / "familyocr.db"

    def pages_dir(self, document_id: str, role: str) -> Path:
        return self.artifacts / "pages" / document_id / role

    def analysis_dir(self, document_id: str, name: str) -> Path:
        return self.artifacts / "analysis" / document_id / name

    def crops_dir(self, document_id: str) -> Path:
        return self.artifacts / "crops" / document_id
