"""The document profile in force for this process.

Band labels and the generation chain are document data, not constants, but they
are needed deep inside parsing and scoring where threading a profile argument
through every call would add noise to code that has nothing else to say about
configuration.

Each CLI command works on exactly one document, so the profile is set once at
the start of the command and read wherever it is needed. The default keeps the
first corpus working when no profile has been set — but `foresight-ocr inspect`
writes a profile for every document, so the default is a fallback, not the
normal path.
"""

from __future__ import annotations

from foresight_ocr.document.profile import DocumentProfile

_DEFAULT = DocumentProfile(
    document_id="",
    band_labels=["庶", "富", "教"],
    generation_chain=["允", "庶", "富", "教"],
    bands_per_page=3,
    notes="fallback profile; run `foresight-ocr inspect` to write a real one",
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


class _BandLabels(dict):
    """Band index -> label, read from whichever profile is active right now.

    A mapping rather than a function because it replaced a module-level dict
    that callers already used as one. It looks up on every access instead of
    caching, so a module imported before `set_profile` still sees the right
    labels — which is the whole reason the constant had to go.
    """

    def get(self, key, default=None):
        return band_labels().get(key, default)

    def items(self):
        return band_labels().items()

    def __getitem__(self, key):
        return band_labels()[key]

    def __contains__(self, key):
        return key in band_labels()


#: Shared by the harness, the review app and the CLI. Defined once here because
#: two identical copies had already drifted apart in the places that used it.
BAND_LABELS = _BandLabels()
