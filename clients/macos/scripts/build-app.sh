#!/bin/zsh
set -euo pipefail

SCRIPT_DIR=${0:A:h}
PACKAGE_DIR=${SCRIPT_DIR:h}
REPO_DIR=${PACKAGE_DIR:h:h}
CONFIGURATION=${CONFIGURATION:-debug}
SIGN_IDENTITY=${SIGN_IDENTITY:-}
NOTARIZE=${NOTARIZE:-0}
NOTARY_KEYCHAIN_PROFILE=${NOTARY_KEYCHAIN_PROFILE:-}
APP_DIR="$PACKAGE_DIR/dist/Foresight OCR.app"
CONTENTS_DIR="$APP_DIR/Contents"
MACOS_DIR="$CONTENTS_DIR/MacOS"
RESOURCES_DIR="$CONTENTS_DIR/Resources"
BACKEND_DIR=${BACKEND_DIR:-"$REPO_DIR/build/release/foresight-ocr-0.1.0-macos-arm64"}
UV_EXECUTABLE=${UV_EXECUTABLE:-$(command -v uv || true)}
BACKEND_DEST="$RESOURCES_DIR/Backend/bin"
TOOLS_DIR="$RESOURCES_DIR/Tools"
NOTICES_DIR="$RESOURCES_DIR/ThirdParty"

if [[ -d /Applications/Xcode-beta.app ]]; then
    export DEVELOPER_DIR=${DEVELOPER_DIR:-/Applications/Xcode-beta.app/Contents/Developer}
fi

if [[ ! -x "$BACKEND_DIR/foresight-ocr" || ! -d "$BACKEND_DIR/_internal" ]]; then
    print -u2 "Standalone backend is missing or incomplete: $BACKEND_DIR"
    print -u2 "Build it first with scripts/build_binary.py, or set BACKEND_DIR."
    exit 2
fi
if [[ ! -f "$BACKEND_DIR/LICENSE" || ! -f "$BACKEND_DIR/THIRD_PARTY_NOTICES.txt" ]]; then
    print -u2 "Backend staging must include LICENSE and THIRD_PARTY_NOTICES.txt: $BACKEND_DIR"
    exit 2
fi
if [[ -z "$UV_EXECUTABLE" || ! -x "$UV_EXECUTABLE" ]]; then
    print -u2 "A uv executable is required for managed OCR-engine installation."
    print -u2 "Set UV_EXECUTABLE to the release-pinned uv binary."
    exit 2
fi

cd "$PACKAGE_DIR"
xcrun swift build --configuration "$CONFIGURATION" --product ForesightOCR
BIN_DIR=$(xcrun swift build --configuration "$CONFIGURATION" --show-bin-path)

if [[ "$APP_DIR" != "$PACKAGE_DIR/dist/Foresight OCR.app" ]]; then
    print -u2 "Refusing unexpected app destination: $APP_DIR"
    exit 2
fi

rm -rf "$APP_DIR"
mkdir -p "$MACOS_DIR" "$RESOURCES_DIR" "$TOOLS_DIR" "$NOTICES_DIR"
cp "$BIN_DIR/ForesightOCR" "$MACOS_DIR/Foresight OCR"
cp "$PACKAGE_DIR/Resources/Info.plist" "$CONTENTS_DIR/Info.plist"
ditto "$BACKEND_DIR" "$BACKEND_DEST"
cp "$UV_EXECUTABLE" "$TOOLS_DIR/uv"
chmod 755 "$BACKEND_DEST/foresight-ocr" "$TOOLS_DIR/uv"
cp "$REPO_DIR/LICENSE" "$NOTICES_DIR/Foresight-OCR-LICENSE-APACHE"
cp "$PACKAGE_DIR/Resources/ThirdParty/uv-LICENSE-MIT" "$NOTICES_DIR/uv-LICENSE-MIT"
sed \
    's/^   Copyright 2026 Runkai Zhang$/   Copyright [yyyy] [name of copyright owner]/' \
    "$REPO_DIR/LICENSE" > "$NOTICES_DIR/uv-LICENSE-APACHE"

typeset -a MACH_O_FILES
while IFS= read -r -d '' candidate; do
    if /usr/bin/file -b "$candidate" | rg -q 'Mach-O'; then
        MACH_O_FILES+=("$candidate")
    fi
done < <(find "$APP_DIR" -type f -print0)

if [[ "$CONFIGURATION" == "release" ]]; then
    if [[ -z "$SIGN_IDENTITY" ]]; then
        print -u2 "Release builds require SIGN_IDENTITY with a Developer ID Application identity."
        exit 2
    fi
    if ! security find-identity -v -p codesigning | rg -F -q -- "$SIGN_IDENTITY"; then
        print -u2 "Signing identity is not available in the current keychain: $SIGN_IDENTITY"
        exit 2
    fi
    for code in $MACH_O_FILES; do
        codesign \
            --force \
            --sign "$SIGN_IDENTITY" \
            --options runtime \
            --timestamp \
            "$code"
    done
    codesign \
        --force \
        --sign "$SIGN_IDENTITY" \
        --options runtime \
        --timestamp \
        "$APP_DIR"
else
    for code in $MACH_O_FILES; do
        codesign --force --sign - --timestamp=none "$code"
    done
    codesign --force --sign - --timestamp=none "$APP_DIR"
fi

codesign --verify --deep --strict --verbose=2 "$APP_DIR"
"$BACKEND_DEST/foresight-ocr" --version
"$BACKEND_DEST/foresight-ocr" doctor
"$TOOLS_DIR/uv" --version

if [[ "$CONFIGURATION" == "release" && "$NOTARIZE" == "1" ]]; then
    if [[ -z "$NOTARY_KEYCHAIN_PROFILE" ]]; then
        print -u2 "NOTARIZE=1 requires a notarytool keychain profile in NOTARY_KEYCHAIN_PROFILE."
        exit 2
    fi
    NOTARY_UPLOAD="$PACKAGE_DIR/dist/Foresight-OCR-notary.zip"
    ditto -c -k --keepParent "$APP_DIR" "$NOTARY_UPLOAD"
    xcrun notarytool submit \
        "$NOTARY_UPLOAD" \
        --keychain-profile "$NOTARY_KEYCHAIN_PROFILE" \
        --wait
    xcrun stapler staple "$APP_DIR"
    xcrun stapler validate "$APP_DIR"
    spctl --assess --type execute --verbose=4 "$APP_DIR"
fi

print "$APP_DIR"
