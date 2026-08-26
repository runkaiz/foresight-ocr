# Contributing to Foresight OCR

Foresight OCR is built around verifiable transcription. A contribution should
make the source, transformation, and resulting text easier to audit—not hide a
guess behind a plausible output.

## Development setup

```bash
uv sync --extra dev
uv run pytest -q
```

Keep OCR engines in their dedicated environments; their model dependencies are
intentionally not part of the core development environment.

## Pull requests

- Add or update tests for behavioral changes.
- Keep source PDFs, generated artifacts, model weights, and local databases out
  of Git.
- Preserve Traditional Chinese and historical character variants. Do not
  silently normalize, simplify, or fill uncertain text.
- Keep human corrections distinct from machine output and retain provenance to
  the source region.
- Document corpus-specific heuristics in a profile or template instead of
  treating one volume's geometry as universal.
- Run the full test suite and describe any real-corpus validation you performed.

Small, focused changes are easiest to review. Bug reports are most useful when
they include the command, document profile, page number, and a redacted crop
that reproduces the problem.
