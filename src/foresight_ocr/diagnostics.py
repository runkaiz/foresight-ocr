"""Small real-runtime checks used by installations and frozen releases."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Diagnostic:
    name: str
    ok: bool
    detail: str


def _check(name: str, operation: Callable[[], str]) -> Diagnostic:
    try:
        return Diagnostic(name, True, operation())
    except Exception as exc:  # noqa: BLE001 - diagnostics must report every failure
        return Diagnostic(name, False, f"{type(exc).__name__}: {exc}")


def _numpy_opencv() -> str:
    import cv2
    import numpy as np

    image = np.zeros((4, 4), dtype=np.uint8)
    image[1:3, 1:3] = 255
    if cv2.countNonZero(image) != 4:
        raise RuntimeError("OpenCV returned an invalid pixel count")
    return f"numpy {np.__version__}; opencv {cv2.__version__}"


def _pillow() -> str:
    from PIL import Image

    with Image.new("L", (2, 2), color=255) as image:
        if image.getpixel((0, 0)) != 255:
            raise RuntimeError("Pillow pixel round-trip failed")
    return f"pillow {Image.__version__}"


def _pdf() -> str:
    import pikepdf

    with pikepdf.Pdf.new() as pdf:
        pdf.add_blank_page(page_size=(72, 72))
        if len(pdf.pages) != 1:
            raise RuntimeError("pikepdf page round-trip failed")
    return f"pikepdf {pikepdf.__version__}"


def _layout() -> str:
    import sklearn
    from sklearn.cluster import DBSCAN

    labels = DBSCAN(eps=0.5, min_samples=1).fit_predict([[0.0], [2.0]])
    if len({int(label) for label in labels}) != 2:
        raise RuntimeError("scikit-learn clustering smoke failed")
    return f"scikit-learn {sklearn.__version__}"


def _yaml() -> str:
    import yaml

    if yaml.safe_load("generation: 庶")["generation"] != "庶":
        raise RuntimeError("UTF-8 YAML round-trip failed")
    return f"pyyaml {yaml.__version__}"


def _resources() -> str:
    from foresight_ocr.ocr.runners import _runner
    from foresight_ocr.review.server import APP_HTML

    runners = (
        _runner(Path.cwd(), "ppocr_v5.py"),
        _runner(Path.cwd(), "paddleocr_vl.py"),
    )
    missing = [path for path in (APP_HTML, *runners) if not path.is_file()]
    if missing:
        raise RuntimeError(f"missing packaged resources: {missing}")
    return "review workspace and OCR runners present"


def core_diagnostics() -> list[Diagnostic]:
    """Exercise native libraries and packaged resources without user data."""
    return [
        _check("image runtime", _numpy_opencv),
        _check("image codec", _pillow),
        _check("PDF runtime", _pdf),
        _check("layout runtime", _layout),
        _check("configuration", _yaml),
        _check("package data", _resources),
    ]
