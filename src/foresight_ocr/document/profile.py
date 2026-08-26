"""Per-document profile: which generation labels this volume uses.

`丙辰庶富教1` is not a generic name — 庶/富/教 are the 字派 characters naming the
three generations charted in that volume, and they appear on every entry. Other
volumes use different ones (`丙辰清廉麗1` charts 清/廉/麗) and not always three
(`丙辰清廉麗熙2` names four).

So band labels are document data, not constants. They are stored in an editable
YAML profile per document, seeded from the volume title and confirmed against
the header text printed on the first chart page — the same header that used to
be mis-cut as entries is the document telling us its own structure.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from foresight_ocr.project import validate_document_id

# Generation labels are single CJK characters. The volume title is of the form
# <cyclical year><labels><volume number>, e.g. 丙辰庶富教1 -> 庶富教.
_CYCLICAL = "甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥"
_TITLE_RE = re.compile(r"^[" + _CYCLICAL + r"]*(?P<labels>[一-鿿]+?)\d*$")

# Labels seen in a parent reference but never as a band of their own belong to
# the preceding volume; they extend the chain backwards.
KNOWN_PREDECESSORS = {
    "庶": "允",
}


@dataclass
class DocumentProfile:
    document_id: str
    band_labels: list[str]
    generation_chain: list[str]
    bands_per_page: int
    notes: str = ""
    label_variants: dict[str, str] = field(default_factory=dict)
    # Document-specific OCR errors in generation-label positions. Unlike
    # ``label_variants`` these are not orthographic equivalents, so they are
    # used only for structure-anchored recovery and reported as such.
    label_confusions: dict[str, str] = field(default_factory=dict)
    # OCR mistakes that are safe only inside this document's printed numeral
    # fields. They are not global substitutions: the parser applies them only
    # while reading an id token and records every repair on ParsedEntry.
    numeral_confusions: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_title(cls, document_id: str) -> "DocumentProfile":
        """Seed a profile from the volume title.

        A seed, not an answer: `verify-layout` checks it against the header text
        actually printed on the page, and the YAML is meant to be hand-edited.
        """
        document_id = validate_document_id(document_id)
        m = _TITLE_RE.match(document_id)
        labels = list(m.group("labels")) if m else []
        chain = list(labels)
        if labels and labels[0] in KNOWN_PREDECESSORS:
            chain = [KNOWN_PREDECESSORS[labels[0]], *chain]
        return cls(
            document_id=document_id,
            band_labels=labels,
            generation_chain=chain,
            bands_per_page=len(labels),
            notes="seeded from the volume title; verify against the header page",
        )

    def band_map(self) -> dict[int, str]:
        return {i: label for i, label in enumerate(self.band_labels)}

    def parent_of(self, label: str) -> str | None:
        if label not in self.generation_chain:
            return None
        i = self.generation_chain.index(label)
        return self.generation_chain[i - 1] if i > 0 else None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def profile_path(configs_dir: Path, document_id: str) -> Path:
    return configs_dir / f"profile_{validate_document_id(document_id)}.yaml"


def load_profile(configs_dir: Path, document_id: str) -> DocumentProfile:
    """Load the profile, seeding one from the title if none exists yet."""
    path = profile_path(configs_dir, document_id)
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        data.setdefault("document_id", document_id)
        data.setdefault("bands_per_page", len(data.get("band_labels", [])))
        return DocumentProfile(**data)
    return DocumentProfile.from_title(document_id)


def save_profile(configs_dir: Path, profile: DocumentProfile) -> Path:
    path = profile_path(configs_dir, profile.document_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(profile.to_dict(), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


def labels_from_header(texts: list[str]) -> list[str]:
    """Read band labels out of header text such as `庶字第` / `富字第`.

    This is the document stating its own structure, so it outranks the title.
    """
    found: list[str] = []
    for t in texts or []:
        for m in re.finditer(r"([一-鿿])字第", t or ""):
            label = m.group(1)
            if label not in found:
                found.append(label)
    return found
