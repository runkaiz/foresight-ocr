# Releasing Foresight OCR

## Before the first public push

- Run `uv run python scripts/audit_public_release.py` and
  `uv run python scripts/audit_secrets.py`. The first command intentionally
  fails until every pending record in `PUBLICATION.toml` is resolved.
- Confirm that the images under `docs/screenshots/` may be published.
  They are real derived artifacts from the first genealogy corpus.
- Review the tracked benchmark transcription and document-specific YAML files
  for material that should remain private.
- Confirm that the author name and email already present in Git history are
  appropriate for a public repository.
- Create the GitHub repository, add its URLs under `[project.urls]`, and set the
  Git remote.
- Push a branch and confirm every Linux, Windows, and macOS `CI` matrix job
  passes on GitHub-hosted runners.
- Enable GitHub private vulnerability reporting so `SECURITY.md` has a working
  contact path.
- Register `foresight-ocr` as a PyPI Trusted Publisher for `.github/workflows/release.yml`
  and the protected `pypi` environment. Require manual approval for that environment.

## 1.0 signing gates

Standalone archives are reproducibly built and attested, but public 1.0 desktop
downloads must also satisfy each operating system's trust path:

- Sign and notarize both macOS binaries with an Apple Developer ID certificate.
- Authenticode-sign the Windows executable with the project's release identity.
- Exercise each signed archive on a clean, non-developer machine without
  bypassing Gatekeeper or SmartScreen.

The release workflow expects these repository secrets:

- `APPLE_DEVELOPER_ID_P12`, `APPLE_DEVELOPER_ID_PASSWORD`, and
  `APPLE_DEVELOPER_ID_IDENTITY` for Developer ID signing;
- `APPLE_NOTARY_ID`, `APPLE_TEAM_ID`, and `APPLE_APP_PASSWORD` for `notarytool`;
- `WINDOWS_SIGNING_PFX` and `WINDOWS_SIGNING_PASSWORD` for Authenticode.

Set the repository variable `WINDOWS_TIMESTAMP_URL` to the RFC 3161 timestamp
service supplied by the Windows certificate issuer. Certificate blobs are
base64-encoded PKCS#12/PFX files. Starting at version 1.0.0, the standalone
builder fails if the platform's signing inputs are absent; macOS also fails if
Apple notarization or Gatekeeper assessment does not succeed.

Do not call an unsigned release candidate 1.0. PyPI installs remain separately
covered by Trusted Publishing and package attestations.

## Release checks

```bash
uv sync --frozen --extra dev --extra release --extra audit
uv run ruff check .
uv run ruff format --check .
uv run mypy src/foresight_ocr
uv run mypy --platform win32 src/foresight_ocr
uv run bandit -q -ll -r src/foresight_ocr scripts runners -x tests
uv run pytest -q --cov=foresight_ocr --cov-branch --cov-report=term-missing
uv run pip-audit
uv build
uv run twine check dist/foresight_ocr-*.whl dist/foresight_ocr-*.tar.gz
uv run python scripts/check_version.py
uv run python scripts/check_platform_wheels.py
uv run python scripts/smoke_cli_distribution.py
uv run python scripts/audit_public_release.py
uv run python scripts/audit_secrets.py
uv run python scripts/verify_release_artifacts.py
uv run python scripts/verify_standalone_archive.py dist/foresight-ocr-*-*.tar.gz \
  dist/foresight-ocr-*-*.zip
uv run foresight-ocr --version
uv run foresight-ocr --help
git diff --check
```

CI also installs `.[dev]` with uv's `lowest-direct` resolution and runs the full
suite. This protects both ends of each declared dependency range: the locked
environment proves the current resolution, while the minimum-dependency job
proves that the advertised lower bounds remain honest.

Standalone archives must be built by the release workflow or from a fresh
runtime-plus-`release` environment. The builder deliberately refuses direct
`dev` or `audit` dependencies so PyInstaller cannot absorb test or audit tooling
into a redistributable archive. For a local candidate, replace `TARGET` with the
current platform label:

```bash
uv run --isolated --frozen --extra release python \
  scripts/build_binary.py --target TARGET
```

Inspect both archives before tagging:

```bash
tar -tzf dist/foresight_ocr-*.tar.gz
unzip -l dist/foresight_ocr-*-py3-none-any.whl
```

The wheel must contain `foresight_ocr/review/app.html` and expose only the
`foresight-ocr` console script. Source PDFs, databases, generated artifacts,
model weights, local design output, and graph indexes must stay untracked.
Corpus-specific benchmarks, profiles, screenshots, and reports must not appear
in either installable archive; `verify_release_artifacts.py` enforces this.
Every standalone archive must also contain the project license, its target-specific
README, and deterministic `THIRD_PARTY_NOTICES.txt` covering the complete installed
runtime dependency closure. The standalone verifier rejects missing notices, links,
path traversal, sensitive file types, and unexpected archive roots.
`scripts/check_platform_wheels.py` must resolve every supported target with binary
wheels only; this prevents an install from silently requiring a local compiler or
raising the documented macOS deployment floor.

Before tagging, run the `Release` workflow manually and install every resulting
standalone archive on a clean target. Once the signed candidates pass, update
all three version declarations together, tag that exact commit as `vX.Y.Z`, and
push the tag. The tag workflow rebuilds and tests all assets, publishes a GitHub
release with checksums and provenance attestations, and sends only the wheel and
source distribution to PyPI through the protected environment.
