"""OCR backend protocol.

The pipeline never imports a recognizer directly. Backends are registered by
name and selected by configuration, so adding a model does not touch document,
layout or segmentation code — and so the benchmark can run every backend over
byte-identical crops.

Nothing here normalizes or simplifies historical text. Traditional characters
stay as recognized. Known library-watermark fragments are excluded from the
usable transcription, with the exact unfiltered output recorded in `raw`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable


@dataclass
class CharacterResult:
    """Per-character output. `confidence` is None when a backend cannot supply it."""

    character: str
    confidence: float | None = None
    bbox: list[float] | None = None


@dataclass
class OCRRequest:
    """One crop to recognize, with everything needed to trace it back."""

    crop_id: str
    path: Path
    variant: str = "original"  # which image variant this crop was rendered from
    context: str = "tight"  # tight | medium | full
    orientation: str = "vertical"  # this corpus is vertical Chinese throughout
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class OCRResult:
    crop_id: str
    transcription: str | None
    backend: str
    model_version: str
    confidence: float | None = None
    character_results: list[CharacterResult] = field(default_factory=list)
    input_variant: str = "original"
    context: str = "tight"
    latency_ms: float | None = None
    error: str | None = None
    raw: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        from .watermarks import filter_watermark_text

        filtered = filter_watermark_text(self.transcription)
        if not filtered.changed:
            return
        unfiltered = self.transcription
        payload = dict(self.raw or {})
        payload["unfiltered_transcription"] = unfiltered
        payload["ignored_watermark_fragments"] = list(filtered.removed)
        self.raw = payload
        self.transcription = filtered.transcription

    @property
    def ok(self) -> bool:
        return self.error is None and self.transcription is not None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["path"] = None
        return d


@runtime_checkable
class OCRBackend(Protocol):
    """A recognizer. Implementations must not raise for a single bad crop.

    A crop that cannot be read comes back as an `OCRResult` with `error` set and
    `transcription` None. Returning a guess instead would corrupt the sequence
    check, which is the only ground-truth-free signal this project has.
    """

    name: str
    model_version: str

    def available(self) -> tuple[bool, str]:
        """(usable, reason). Checked before a benchmark run, never during."""
        ...

    def recognize(self, requests: list[OCRRequest]) -> list[OCRResult]: ...


BACKENDS: dict[str, Callable[..., OCRBackend]] = {}


def register_backend(
    name: str,
) -> Callable[[Callable[..., OCRBackend]], Callable[..., OCRBackend]]:
    def decorator(factory: Callable[..., OCRBackend]) -> Callable[..., OCRBackend]:
        BACKENDS[name] = factory
        return factory

    return decorator


def get_backend(name: str, **kwargs: Any) -> OCRBackend:
    # Import for side effects: each module registers itself on import.
    from foresight_ocr.ocr import runners  # noqa: F401,PLC0415

    if name not in BACKENDS:
        raise KeyError(f"unknown OCR backend {name!r}; known: {sorted(BACKENDS)}")
    return BACKENDS[name](**kwargs)
