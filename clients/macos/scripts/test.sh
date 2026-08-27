#!/bin/zsh
set -euo pipefail

SCRIPT_DIR=${0:A:h}
PACKAGE_DIR=${SCRIPT_DIR:h}

if [[ -d /Applications/Xcode-beta.app ]]; then
    export DEVELOPER_DIR=${DEVELOPER_DIR:-/Applications/Xcode-beta.app/Contents/Developer}
fi

xcrun swift test --package-path "$PACKAGE_DIR"
