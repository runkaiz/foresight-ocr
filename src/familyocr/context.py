"""The document profile in force for this process.

Band labels and the generation chain are document data, not constants, but they
are needed deep inside parsing and scoring where threading a profile argument
through every call would add noise to code that has nothing else to say about
configuration.

Each CLI command works on exactly one document, so the profile is set once at
the start of the command and read wherever it is needed. The default keeps the
first corpus working when no profile has been set — but `familyocr inspect`
writes a profile for every document, so the default is a fallback, not the
normal path.
"""

from __future__ import annotations

from familyocr.document.profile import DocumentProfile

_DEFAULT = DocumentProfile(
    document_id="",
    band_labels=["庶", "富", "教"],
    generation_chain=["允", "庶", "富", "教"],
    bands_per_page=3,
    notes="fallback profile; run `familyocr inspect` to write a real one",
)

_active: DocumentProfile = _DEFAULT


def set_profile(profile: DocumentProfile) -> None:
    global _active
    _active = profile


def get_profile() -> DocumentProfile:
    return _active


def band_labels() -> dict[int, str]:
    return _active.band_map()


def generation_chain() -> list[str]:
    return _active.generation_chain
