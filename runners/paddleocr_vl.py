#!/usr/bin/env python
"""PaddleOCR-VL runner on MLX/Metal. Runs inside .venv-vlm; must not import familyocr.

Usage:
    python paddleocr_vl.py --probe
    python paddleocr_vl.py manifest.json results.json

PaddleOCR-VL is a region recognizer: it expects an already-cropped region plus a
task prompt, which is exactly what the segmentation stage produces. The model is
loaded once per batch and reused, since load time dwarfs per-crop inference.

Note on 1.6: PaddlePaddle state that 1.6 is architecturally identical to 1.5, so
mlx-vlm's `paddleocr_vl` implementation loads it directly. `--probe` reports
which weights actually resolved, so the benchmark records the real model rather
than the one we hoped for.
"""

from __future__ import annotations

import json
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

DEFAULT_MODEL = "PaddlePaddle/PaddleOCR-VL-1.6"
FALLBACK_MODEL = "mlx-community/PaddleOCR-VL-1.5-4bit"
DEFAULT_PROMPT = "OCR:"


def probe() -> int:
    try:
        import mlx.core as mx
        import mlx_vlm
    except Exception as exc:  # noqa: BLE001
        print(f"import failed: {exc}", file=sys.stderr)
        return 1
    print(f"mlx-vlm {mlx_vlm.__version__} on {mx.default_device()}")
    return 0


def _load(model_id: str):
    from mlx_vlm import load

    try:
        return load(model_id), model_id
    except Exception as exc:  # noqa: BLE001
        if model_id == DEFAULT_MODEL:
            print(f"{model_id} failed to load ({exc}); trying {FALLBACK_MODEL}",
                  file=sys.stderr)
            from mlx_vlm import load as _reload

            return _reload(FALLBACK_MODEL), FALLBACK_MODEL
        raise


def _prepare_image(path: str, scale: float, workdir: Path) -> str:
    """Optionally downscale a crop before inference.

    Crops come off a 535 ppi scan at roughly 150 px per character, several times
    what a recognizer needs. Dynamic-resolution encoders bill by patch count, so
    resolution nobody reads is paid for on every crop. The rescaled copy is
    written to a temp file — the crop on disk stays untouched.
    """
    if scale >= 0.999:
        return path
    from PIL import Image

    with Image.open(path) as im:
        w, h = im.size
        out = im.resize(
            (max(int(w * scale), 8), max(int(h * scale), 8)), Image.LANCZOS
        )
        dest = workdir / Path(path).name
        out.save(dest)
    return str(dest)


def _run_batched(model, processor, items, prompt_text, max_tokens, image_paths):
    """Recognize a whole batch in one call.

    One `generate` per image leaves the GPU idle between crops and re-runs the
    vision encoder unbatched. These crops are near-identical in shape, which is
    exactly the case `group_by_shape` is for.
    """
    from mlx_vlm import batch_generate
    from mlx_vlm.prompt_utils import apply_chat_template

    config = model.config
    prompts = [
        apply_chat_template(processor, config, prompt_text, num_images=1)
        for _ in items
    ]
    # `images` is one image per prompt, flat. Passing a list-of-lists raises
    # deep inside the processor with an opaque AttributeError.
    outputs = batch_generate(
        model, processor, images=list(image_paths), prompts=prompts,
        max_tokens=max_tokens, verbose=False, group_by_shape=True,
    )
    texts = getattr(outputs, "texts", outputs)
    return [
        (t.strip() if isinstance(t, str) else str(getattr(t, "text", "")).strip())
        for t in texts
    ]


def main(manifest_path: str, out_path: str) -> int:
    import shutil
    import tempfile

    from mlx_vlm import generate
    from mlx_vlm.prompt_utils import apply_chat_template

    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    options = manifest.get("options") or {}
    prompt_text = options.get("prompt", DEFAULT_PROMPT)
    max_tokens = int(options.get("max_tokens", 256))
    temperature = float(options.get("temperature", 0.0))
    # Both default to the original behaviour so a benchmark comparing them is
    # comparing one change at a time.
    batched = bool(options.get("batched", False))
    scale = float(options.get("image_scale", 1.0))

    (model, processor), resolved = _load(manifest.get("model") or DEFAULT_MODEL)
    config = model.config

    workdir = Path(tempfile.mkdtemp(prefix="familyocr-vl-scaled-"))
    try:
        return _recognize(
            model, processor, resolved, config, manifest, out_path, workdir,
            prompt_text, max_tokens, temperature, batched, scale,
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _recognize(model, processor, resolved, config, manifest, out_path, workdir,
               prompt_text, max_tokens, temperature, batched, scale) -> int:
    from mlx_vlm import generate
    from mlx_vlm.prompt_utils import apply_chat_template

    items = manifest["items"]
    paths = [_prepare_image(it["path"], scale, workdir) for it in items]

    if batched:
        started = time.perf_counter()
        try:
            texts = _run_batched(
                model, processor, items, prompt_text, max_tokens, paths
            )
        except Exception as exc:  # noqa: BLE001 - fall back rather than lose a run
            print(f"batched generation failed ({exc}); falling back to per-crop",
                  file=sys.stderr)
            batched = False
        else:
            per_crop = (time.perf_counter() - started) * 1000.0 / max(len(items), 1)
            results = [
                {
                    "crop_id": it["crop_id"],
                    "transcription": text or None,
                    "confidence": None,
                    "characters": [],
                    "latency_ms": per_crop,
                    "error": None if text else "empty generation",
                    "raw": {"prompt": prompt_text, "batched": True,
                            "image_scale": scale},
                }
                for it, text in zip(items, texts)
            ]
            Path(out_path).write_text(
                json.dumps({"model_version": resolved, "results": results},
                           ensure_ascii=False),
                encoding="utf-8",
            )
            return 0

    results = []
    for item, image_path in zip(items, paths):
        crop_id = item["crop_id"]
        started = time.perf_counter()
        try:
            formatted = apply_chat_template(
                processor, config, prompt_text, num_images=1
            )
            out = generate(
                model, processor, formatted, [image_path],
                max_tokens=max_tokens, temperature=temperature, verbose=False,
            )
            text = getattr(out, "text", out)
            text = (text or "").strip()
            results.append({
                "crop_id": crop_id,
                "transcription": text or None,
                # The model emits text without token-level scores through this
                # path, so confidence is left null rather than invented.
                "confidence": None,
                "characters": [],
                "latency_ms": (time.perf_counter() - started) * 1000.0,
                "error": None if text else "empty generation",
                "raw": {"prompt": prompt_text, "batched": False,
                        "image_scale": scale},
            })
        except Exception as exc:  # noqa: BLE001 - one bad crop must not stop the batch
            results.append({
                "crop_id": crop_id, "transcription": None, "confidence": None,
                "characters": [],
                "latency_ms": (time.perf_counter() - started) * 1000.0,
                "error": f"{type(exc).__name__}: {exc}", "raw": None,
            })

    Path(out_path).write_text(
        json.dumps({"model_version": resolved, "results": results},
                   ensure_ascii=False),
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
