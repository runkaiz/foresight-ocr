"""Debug overlays.

The spec is blunt about this: an algorithm that cannot explain its geometry
visually is not production-ready. Every geometric stage writes one of these.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from .io import read_image, write_image

BGR = tuple[int, int, int]
GREEN: BGR = (60, 200, 60)
RED: BGR = (40, 40, 220)
BLUE: BGR = (230, 160, 40)
AMBER: BGR = (30, 190, 240)


def draw_frame_overlay(
    image: np.ndarray,
    corners: list[list[float]],
    interior_h: list[float],
    out_path: Path,
    ok: bool,
    caption: str = "",
    scale: float = 0.25,
) -> Path:
    """Draw the fitted page frame and interior rules over the original page."""
    canvas = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    if canvas.ndim == 2:
        canvas = cv2.cvtColor(canvas, cv2.COLOR_GRAY2BGR)
    colour = GREEN if ok else RED

    if corners:
        pts = (np.array(corners, dtype=np.float32) * scale).astype(np.int32)
        cv2.polylines(canvas, [pts], isClosed=True, color=colour, thickness=2)
        for i, p in enumerate(pts):
            cv2.circle(canvas, tuple(p), 6, colour, -1)
            cv2.putText(
                canvas,
                "TL TR BR BL".split()[i],
                tuple(p + np.array([8, -8])),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.4,
                colour,
                1,
                cv2.LINE_AA,
            )

    for y in interior_h:
        yy = int(y * scale)
        cv2.line(canvas, (0, yy), (canvas.shape[1], yy), BLUE, 1, cv2.LINE_AA)

    if caption:
        for i, line in enumerate(caption.split("\n")):
            cv2.putText(
                canvas,
                line,
                (8, 18 + 16 * i),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.45,
                (0, 0, 0),
                3,
                cv2.LINE_AA,
            )
            cv2.putText(
                canvas,
                line,
                (8, 18 + 16 * i),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.45,
                colour,
                1,
                cv2.LINE_AA,
            )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    write_image(out_path, canvas)
    return out_path


def draw_grid_overlay(
    image: np.ndarray,
    band_edges: list[float],
    column_edges: list[float],
    out_path: Path,
    caption: str = "",
    scale: float = 0.25,
) -> Path:
    """Draw band boundaries and entry-column boundaries on a normalized page."""
    canvas = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    if canvas.ndim == 2:
        canvas = cv2.cvtColor(canvas, cv2.COLOR_GRAY2BGR)
    h, w = canvas.shape[:2]

    for y in band_edges:
        cv2.line(
            canvas, (0, int(y * scale)), (w, int(y * scale)), GREEN, 2, cv2.LINE_AA
        )
    for x in column_edges:
        cv2.line(
            canvas, (int(x * scale), 0), (int(x * scale), h), AMBER, 1, cv2.LINE_AA
        )

    if caption:
        cv2.putText(
            canvas,
            caption,
            (8, 18),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.45,
            (0, 0, 0),
            3,
            cv2.LINE_AA,
        )
        cv2.putText(
            canvas,
            caption,
            (8, 18),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.45,
            GREEN,
            1,
            cv2.LINE_AA,
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    write_image(out_path, canvas)
    return out_path


def contact_sheet(
    paths: list[Path], out_path: Path, cols: int = 5, cell: tuple[int, int] = (260, 400)
) -> Path:
    """Tile overlays so a whole run can be eyeballed in one image."""
    if not paths:
        raise ValueError("no images to tile")
    rows = (len(paths) + cols - 1) // cols
    sheet = np.full((rows * cell[1], cols * cell[0], 3), 245, dtype=np.uint8)
    for i, p in enumerate(paths):
        img = read_image(p)
        if img is None:
            continue
        img = cv2.resize(img, cell, interpolation=cv2.INTER_AREA)
        r, c = divmod(i, cols)
        sheet[r * cell[1] : (r + 1) * cell[1], c * cell[0] : (c + 1) * cell[0]] = img
    out_path.parent.mkdir(parents=True, exist_ok=True)
    write_image(out_path, sheet)
    return out_path
