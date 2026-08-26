"""Unicode-safe OpenCV image file I/O.

OpenCV's filename-based codecs can fail on Windows when a path contains CJK
characters. Python's file APIs use Windows' Unicode path support, so decode and
encode through byte buffers while keeping OpenCV responsible for the codec.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np


def read_image(path: Path, flags: int = cv2.IMREAD_COLOR) -> np.ndarray | None:
    """Read an image from any Python-supported path, returning ``None`` on failure."""
    try:
        payload = path.read_bytes()
    except OSError:
        return None
    if not payload:
        return None
    encoded = np.frombuffer(payload, dtype=np.uint8)
    return cv2.imdecode(encoded, flags)


def write_image(path: Path, image: np.ndarray) -> bool:
    """Write an image to any Python-supported path using its filename suffix."""
    suffix = path.suffix.lower()
    if not suffix:
        return False
    try:
        ok, encoded = cv2.imencode(suffix, image)
        if not ok:
            return False
        path.write_bytes(encoded.tobytes())
    except (cv2.error, OSError):
        return False
    return True
