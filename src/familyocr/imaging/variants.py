"""Image variants, including the cyan-watermark suppression experiments.

The library stamp is cyan; historical ink is close to neutral. Cyan pigment
absorbs red and reflects green and blue, so in the *red* channel the watermark is
dark — indistinguishable from ink — while in green and blue it is bright. Taking
the per-pixel maximum across channels therefore keeps ink dark (dark in all three)
and pushes the watermark toward white. The naive "just use the red channel" move
does the opposite of what is wanted.

Nothing here is destructive: each function returns a new variant, and the caller
keeps all of them so OCR backends can be benchmarked per variant.
"""

from __future__ import annotations

import cv2
import numpy as np

# Variant name -> short description, surfaced in reports and the DB `role` column.
VARIANTS: dict[str, str] = {
    "gray": "standard luminance (baseline; watermark still present)",
    "red": "red channel alone (watermark darkens — expected to be the worst)",
    "maxrgb": "per-pixel max over R,G,B (cyan suppressed, neutral ink kept)",
    "lab_l": "CIELAB lightness (perceptual baseline)",
    "neutral": "max-RGB with chroma-masked pixels pulled to local background",
    "inpaint": "max-RGB with chroma mask inpainted (Telea)",
    "contrast": "max-RGB + background flattening + CLAHE",
    "binary": "adaptive threshold over the contrast variant",
}


def _as_bgr(image: np.ndarray) -> np.ndarray:
    if image.ndim == 2:
        return cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    if image.shape[2] == 4:
        return cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)
    return image


def ink_channel(image: np.ndarray) -> np.ndarray:
    """Grayscale in which neutral ink stays dark and cyan pigment goes bright."""
    bgr = _as_bgr(image)
    return bgr.max(axis=2)


def chroma_mask(
    image: np.ndarray,
    threshold: int = 18,
    dilate: int = 3,
) -> np.ndarray:
    """Binary mask of strongly non-neutral (coloured) pixels.

    Distance from the neutral axis in CIELAB. Paper tint and ink both sit near
    a*=b*=128; the cyan stamp does not.
    """
    lab = cv2.cvtColor(_as_bgr(image), cv2.COLOR_BGR2LAB)
    a = lab[:, :, 1].astype(np.int16) - 128
    b = lab[:, :, 2].astype(np.int16) - 128
    dist = np.sqrt(a.astype(np.float32) ** 2 + b.astype(np.float32) ** 2)
    mask = (dist > threshold).astype(np.uint8) * 255
    if dilate > 0:
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (dilate, dilate))
        mask = cv2.dilate(mask, k)
    return mask


def flatten_background(gray: np.ndarray, sigma: int = 81) -> np.ndarray:
    """Divide out slow illumination/paper-tone variation, keeping stroke contrast."""
    blur = cv2.GaussianBlur(gray, (0, 0), sigma)
    blur = np.maximum(blur, 1)
    flat = (gray.astype(np.float32) / blur.astype(np.float32)) * 200.0
    return np.clip(flat, 0, 255).astype(np.uint8)


def build_variant(image: np.ndarray, name: str) -> np.ndarray:
    """Produce one named variant from an original BGR page."""
    bgr = _as_bgr(image)

    if name == "gray":
        return cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    if name == "red":
        return bgr[:, :, 2]
    if name == "maxrgb":
        return ink_channel(bgr)
    if name == "lab_l":
        return cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)[:, :, 0]

    if name == "neutral":
        base = ink_channel(bgr)
        mask = chroma_mask(bgr) > 0
        # Replace coloured pixels with a heavily blurred estimate of local paper
        # tone. Strokes under the stamp survive because max-RGB already kept them
        # dark, and the blur is computed from the same variant.
        bg = cv2.GaussianBlur(base, (0, 0), 25)
        out = base.copy()
        out[mask] = np.maximum(base[mask], bg[mask])
        return out

    if name == "inpaint":
        base = ink_channel(bgr)
        mask = chroma_mask(bgr, dilate=5)
        return cv2.inpaint(base, mask, 3, cv2.INPAINT_TELEA)

    if name == "contrast":
        base = flatten_background(ink_channel(bgr))
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        return clahe.apply(base)

    if name == "binary":
        contrast = build_variant(bgr, "contrast")
        return cv2.adaptiveThreshold(
            contrast, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 51, 12
        )

    raise ValueError(f"unknown variant {name!r}; known: {sorted(VARIANTS)}")
