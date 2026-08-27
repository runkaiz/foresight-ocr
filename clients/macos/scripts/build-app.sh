#!/bin/zsh
set -euo pipefail

SCRIPT_DIR=${0:A:h}
PACKAGE_DIR=${SCRIPT_DIR:h}
REPO_DIR=${PACKAGE_DIR:h:h}
CONFIGURATION=${CONFIGURATION:-debug}
SIGN_IDENTITY=${SIGN_IDENTITY:-}
NOTARIZE=${NOTARIZE:-0}
NOTARY_KEYCHAIN_PROFILE=${NOTARY_KEYCHAIN_PROFILE:-}
CREATE_DMG=${CREATE_DMG:-0}
APP_DIR="$PACKAGE_DIR/dist/Foresight OCR.app"
CONTENTS_DIR="$APP_DIR/Contents"
MACOS_DIR="$CONTENTS_DIR/MacOS"
RESOURCES_DIR="$CONTENTS_DIR/Resources"
VERSION=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' \
    "$PACKAGE_DIR/Resources/Info.plist")
case "$(uname -m)" in
    arm64) BACKEND_TARGET=macos-arm64 ;;
    x86_64) BACKEND_TARGET=macos-x86_64 ;;
    *)
        print -u2 "Unsupported Mac architecture: $(uname -m)"
        exit 2
        ;;
esac
BACKEND_DIR=${BACKEND_DIR:-"$REPO_DIR/build/release/foresight-ocr-$VERSION-$BACKEND_TARGET"}
UV_EXECUTABLE=${UV_EXECUTABLE:-$(command -v uv || true)}
BACKEND_DEST="$RESOURCES_DIR/Backend/bin"
TOOLS_DIR="$RESOURCES_DIR/Tools"
NOTICES_DIR="$RESOURCES_DIR/ThirdParty"
DMG_PATH="$PACKAGE_DIR/dist/Foresight-OCR-$VERSION-$BACKEND_TARGET.dmg"

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
if [[ "$NOTARIZE" == "1" && "$CONFIGURATION" != "release" ]]; then
    print -u2 "NOTARIZE=1 requires CONFIGURATION=release."
    exit 2
fi

notarize_artifact() {
    local artifact=$1
    if [[ -n "$NOTARY_KEYCHAIN_PROFILE" ]]; then
        xcrun notarytool submit \
            "$artifact" \
            --keychain-profile "$NOTARY_KEYCHAIN_PROFILE" \
            --wait
    elif [[ -n "${FORESIGHT_APPLE_ID:-}" \
        && -n "${FORESIGHT_APPLE_TEAM_ID:-}" \
        && -n "${FORESIGHT_APPLE_APP_PASSWORD:-}" ]]; then
        xcrun notarytool submit \
            "$artifact" \
            --apple-id "$FORESIGHT_APPLE_ID" \
            --team-id "$FORESIGHT_APPLE_TEAM_ID" \
            --password "$FORESIGHT_APPLE_APP_PASSWORD" \
            --wait
    else
        print -u2 "Notarization requires NOTARY_KEYCHAIN_PROFILE or Apple notary credentials."
        exit 2
    fi
}

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
    if /usr/bin/file -b "$candidate" | /usr/bin/grep -q 'Mach-O'; then
        MACH_O_FILES+=("$candidate")
    fi
done < <(find "$APP_DIR" -type f -print0)

if [[ "$CONFIGURATION" == "release" ]]; then
    if [[ -z "$SIGN_IDENTITY" ]]; then
        print -u2 "Release builds require SIGN_IDENTITY with a Developer ID Application identity."
        exit 2
    fi
    if ! security find-identity -v -p codesigning \
        | /usr/bin/grep -F -q -- "$SIGN_IDENTITY"; then
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

if [[ "$CONFIGURATION" == "release" && "$NOTARIZE" == "1" \
    && "$CREATE_DMG" != "1" ]]; then
    NOTARY_UPLOAD="$PACKAGE_DIR/dist/Foresight-OCR-notary.zip"
    ditto -c -k --keepParent "$APP_DIR" "$NOTARY_UPLOAD"
    notarize_artifact "$NOTARY_UPLOAD"
    xcrun stapler staple "$APP_DIR"
    xcrun stapler validate "$APP_DIR"
    spctl --assess --type execute --verbose=4 "$APP_DIR"
fi

if [[ "$CREATE_DMG" == "1" ]]; then
    DMG_ROOT=$(mktemp -d "$PACKAGE_DIR/dist/foresight-ocr-dmg.XXXXXX")
    DMG_MOUNT=""
    cleanup_dmg_root() {
        if [[ -n "$DMG_MOUNT" && -d "$DMG_MOUNT" ]]; then
            diskutil eject "$DMG_MOUNT" >/dev/null 2>&1 \
                || hdiutil detach "$DMG_MOUNT" >/dev/null 2>&1 \
                || true
            rmdir "$DMG_MOUNT" >/dev/null 2>&1 || true
        fi
        rm -rf "$DMG_ROOT"
    }
    trap cleanup_dmg_root EXIT
    ditto "$APP_DIR" "$DMG_ROOT/Foresight OCR.app"
    ln -s /Applications "$DMG_ROOT/Applications"
    rm -f "$DMG_PATH"
    if diskutil image create from --help >/dev/null 2>&1; then
        diskutil image create from \
            --volumeName "Foresight OCR" \
            --format UDZO \
            "$DMG_ROOT" \
            "$DMG_PATH"
    else
        hdiutil create \
            -volname "Foresight OCR" \
            -srcfolder "$DMG_ROOT" \
            -format UDZO \
            -ov \
            "$DMG_PATH"
    fi
    if [[ "$CONFIGURATION" == "release" ]]; then
        codesign \
            --force \
            --sign "$SIGN_IDENTITY" \
            --timestamp \
            "$DMG_PATH"
        codesign --verify --strict --verbose=2 "$DMG_PATH"
    fi
    if [[ "$CONFIGURATION" == "release" && "$NOTARIZE" == "1" ]]; then
        notarize_artifact "$DMG_PATH"
        xcrun stapler staple "$DMG_PATH"
        xcrun stapler validate "$DMG_PATH"
        spctl --assess \
            --type open \
            --context context:primary-signature \
            --verbose=4 \
            "$DMG_PATH"
    fi
    DMG_MOUNT=$(mktemp -d "$PACKAGE_DIR/dist/foresight-ocr-mount.XXXXXX")
    if diskutil image attach --help >/dev/null 2>&1; then
        diskutil image attach \
            --readOnly \
            --nobrowse \
            --mountPoint "$DMG_MOUNT" \
            "$DMG_PATH" >/dev/null
    else
        hdiutil attach \
            -readonly \
            -nobrowse \
            -mountpoint "$DMG_MOUNT" \
            "$DMG_PATH" >/dev/null
    fi
    if [[ ! -d "$DMG_MOUNT/Foresight OCR.app" \
        || ! -L "$DMG_MOUNT/Applications" \
        || "$(readlink "$DMG_MOUNT/Applications")" != "/Applications" ]]; then
        print -u2 "DMG layout verification failed: $DMG_PATH"
        exit 2
    fi
    codesign --verify --deep --strict --verbose=2 \
        "$DMG_MOUNT/Foresight OCR.app"
    "$DMG_MOUNT/Foresight OCR.app/Contents/Resources/Backend/bin/foresight-ocr" \
        --version
    "$DMG_MOUNT/Foresight OCR.app/Contents/Resources/Tools/uv" --version
    if ! diskutil eject "$DMG_MOUNT" >/dev/null 2>&1; then
        hdiutil detach "$DMG_MOUNT" >/dev/null
    fi
    rmdir "$DMG_MOUNT"
    DMG_MOUNT=""
    print "$DMG_PATH"
else
    print "$APP_DIR"
fi
