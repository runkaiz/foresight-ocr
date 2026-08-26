#!/usr/bin/env python
"""PP-OCRv5 runner. Runs inside .venv-paddle; must not import foresight-ocr.

Usage:
    python ppocr_v5.py --probe
    python ppocr_v5.py manifest.json results.json

Vertical text: the glyphs in a vertical Chinese column are *upright* — only the
line direction is vertical. Rotating the crop 90° therefore makes every
character lie on its side and the recognizer returns nonsense. The crop is fed
unrotated and the detected boxes are reassembled top-to-bottom instead.
"""

from __future__ import annotations

import json
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")


def probe() -> int:
    try:
        import paddle
        import paddleocr
    except Exception as exc:  # noqa: BLE001 - report any import failure verbatim
        print(f"import failed: {exc}", file=sys.stderr)
        return 1
    print(f"paddle {paddle.__version__} / paddleocr {paddleocr.__version__}")
    return 0


def main(manifest_path: str, out_path: str) -> int:
    import cv2
    import numpy as np
    import paddleocr
    from paddleocr import PaddleOCR

    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    options = manifest.get("options") or {}

    ocr = PaddleOCR(
        lang=options.get("lang", "chinese_cht"),
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        device="cpu",
    )

    results = []
    for item in manifest["items"]:
        crop_id = item["crop_id"]
        started = time.perf_counter()
        try:
            img = cv2.imread(item["path"])
            if img is None:
                raise RuntimeError(f"unreadable crop: {item['path']}")

            out = ocr.predict(img)
            texts, scores, polys = [], [], []
            for page in out:
                data = (
                    page.json.get("res", page.json) if hasattr(page, "json") else page
                )
                texts.extend(data.get("rec_texts", []) or [])
                scores.extend(data.get("rec_scores", []) or [])
                polys.extend(data.get("rec_polys", data.get("dt_polys", [])) or [])

            # Reading order is top-to-bottom within a vertical column, and
            # right-to-left when a crop happens to span more than one column.
            if polys and len(polys) == len(texts):
                orientation = item.get("orientation", "vertical")
                image_width = img.shape[1]

                def _key(i, *, boxes=polys, direction=orientation, width=image_width):
                    poly = np.asarray(boxes[i], dtype=float).reshape(-1, 2)
                    cx, cy = poly[:, 0].mean(), poly[:, 1].mean()
                    if direction == "vertical":
                        # Group into columns first (widest gap dominates), then
                        # order top-to-bottom inside each.
                        return (-round(cx / max(width * 0.25, 1)), cy)
                    return (cy, cx)

                order = sorted(range(len(texts)), key=_key)
                texts = [texts[i] for i in order]
                scores = (
                    [scores[i] for i in order] if len(scores) == len(order) else scores
                )

            transcription = "".join(texts) if texts else None
            confidence = float(np.mean(scores)) if scores else None
            results.append(
                {
                    "crop_id": crop_id,
                    "transcription": transcription,
                    "confidence": confidence,
                    # PP-OCR reports per-line scores, not per-character ones.
                    "characters": [],
                    "latency_ms": (time.perf_counter() - started) * 1000.0,
                    "error": None if transcription else "no text detected",
                    "raw": {
                        "rec_texts": texts,
                        "rec_scores": [float(s) for s in scores],
                    },
                }
            )
        except Exception as exc:  # noqa: BLE001 - one bad crop must not stop the batch
            results.append(
                {
                    "crop_id": crop_id,
                    "transcription": None,
                    "confidence": None,
                    "characters": [],
                    "latency_ms": (time.perf_counter() - started) * 1000.0,
                    "error": f"{type(exc).__name__}: {exc}",
                    "raw": None,
                }
            )

    Path(out_path).write_text(
        json.dumps(
            {"model_version": f"paddleocr-{paddleocr.__version__}", "results": results},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--probe":
        raise SystemExit(probe())
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
