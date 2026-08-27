# Native macOS 27 client

This directory contains the native SwiftUI/AppKit review client. It is a real
Mac application, not a WebView wrapper. The Python review service remains the
single owner of OCR, layout, correction, learning, and export semantics.

## Requirements

- End users: macOS 27 and a signed/notarized `Foresight OCR.app` distribution.
  They do not install Python, `uv`, the `foresight-ocr` CLI, or a project
  virtual environment.
- Developers building the app: Xcode 27 with the macOS 27 SDK, a staged
  standalone backend, and the release-pinned `uv` executable.

The package intentionally uses `// swift-tools-version: 6.4` and
`.macOS(.v27)`. The scripts select `/Applications/Xcode-beta.app` when present
without changing the machine's global `xcode-select` setting.

## Bundled runtime

The application bundle contains the standalone `foresight-ocr` backend and
`uv`. Normal startup prefers that backend; `FORESIGHT_OCR_EXECUTABLE` remains a
developer override only. OCR engines and their managed Python runtime are
installed on demand under `~/Library/Application Support/Foresight OCR/`, not
inside a user's project:

```text
Foresight OCR/
  engines/           managed OCR-engine environments
  python/            uv-managed Python runtime
  models/            model downloads
  cache/uv/          installer cache
```

Projects remain portable document folders containing the source PDF, configs,
derived artifacts, and database. They never need a `.venv`.

## Build and test

```bash
cd clients/macos
./scripts/test.sh
./scripts/build-app.sh
open "dist/Foresight OCR.app"
```

`build-app.sh` produces an ad-hoc-signed development application and verifies
its signature by default. For a Developer ID release build, pass the exact
identity reported by `security find-identity -v -p codesigning`:

```bash
CONFIGURATION=release \
SIGN_IDENTITY="Developer ID Application: Runkai Zhang (C58CLY4K2U)" \
./scripts/build-app.sh
```

Release builds enable Hardened Runtime and request a secure timestamp. The
result is Developer ID signed. To submit, staple, and assess it in the same
build, add `NOTARIZE=1` and `NOTARY_KEYCHAIN_PROFILE=PROFILE`.

To produce the end-user disk image, add `CREATE_DMG=1`. The DMG contains the
application and an Applications-folder shortcut:

```bash
CONFIGURATION=release \
SIGN_IDENTITY="Developer ID Application: Runkai Zhang (C58CLY4K2U)" \
CREATE_DMG=1 NOTARIZE=1 NOTARY_KEYCHAIN_PROFILE=PROFILE \
./scripts/build-app.sh
```

The release workflow builds this on the `xcode-27` Apple-silicon runner. It
signs nested code and the application before signing the disk image, submits
the DMG to Apple's notary service, staples the ticket, and runs Gatekeeper
assessment. Manual release-candidate runs may produce an ad-hoc-signed DMG when
release credentials are intentionally unavailable; tagged releases fail closed.

The native end-user startup flow is:

1. Choose **New Project** and select or drop exactly one PDF, or choose
   **Open Existing Project** for a portable project folder.
2. For a new project, choose the destination and document identifier. The app
   invokes its bundled backend to create the project and preserve the PDF.
3. Choose an OCR engine. If it is not ready, the app installs its pinned,
   managed environment and reports native progress.
4. Wait for the seven preparation stages, then enter the review workspace. The
   app starts `foresight-ocr review` on an ephemeral loopback port and validates
   its protocol handshake.

None of these steps requires a terminal, a system Python installation, `uv`, or
a project `.venv`. The welcome, import, runtime-manifest engine selection, and
seven-stage preparation views use standard macOS controls and have been
exercised in the packaged development app.

For UI automation or development, the initial project and document can be
selected without bypassing the same manifest/handshake path:

```bash
FORESIGHT_OCR_PROJECT=/path/to/project \
FORESIGHT_OCR_DOCUMENT=DOCUMENT_ID \
"dist/Foresight OCR.app/Contents/MacOS/Foresight OCR"
```

Attach mode accepts only explicit-port HTTP URLs on `127.0.0.1`, `localhost`,
or `::1`.

## Review guarantees

- The primary crop is height-fitted by default and remains zoomable, pannable,
  and scrollable; the source page is secondary
  context with exact server pixel geometry.
- Traditional Chinese, rare glyphs, line breaks, free-form headers, explicit
  blank corrections, and unreadable state remain distinct.
- Page and document re-OCR use the full-resolution watermark-suppressed variant
  and never overwrite human corrections.
- Opening correction learning is read-only. Analysis runs only from its explicit
  button and never launches OCR or enables rules.
- ZIP and folder exports are produced by the backend, preserving its semantic
  generation order.

The implementation contract and verification matrix are in
[`../../docs/macos-client-architecture.md`](../../docs/macos-client-architecture.md).
