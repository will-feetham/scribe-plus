#!/usr/bin/env bash
# Builds "Scribe Plus.app" (launcher + Info.plist + icon) into the folder given.
# Runs on a Mac (CI). The installer copies the result to ~/Applications.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
out="${1:?usage: build-app.sh OUTDIR}"
app="$out/Scribe Plus.app"
rm -rf "$app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
clang -O2 -Wall -arch arm64 -arch x86_64 -mmacosx-version-min=11.0 \
    -o "$app/Contents/MacOS/Scribe Plus" "$here/launcher.c"
cp "$here/Info.plist" "$app/Contents/Info.plist"
cp "$here/ScribePlus.icns" "$app/Contents/Resources/ScribePlus.icns"
printf 'APPL????' > "$app/Contents/PkgInfo"
codesign --force --sign - --identifier us.feetham.scribeplus "$app"
codesign --verify --verbose=2 "$app"
lipo -archs "$app/Contents/MacOS/Scribe Plus"
