#!/usr/bin/env bash
# Builds "Scribe Plus.app" (launcher + Info.plist + icon) and the call-recording
# helper "audiotap" into the folder given. Runs on a Mac (CI). The installer
# copies the app to ~/Applications; the helper stays with the Python code.
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

# The helper is not part of the app bundle on purpose: macOS ties the
# permissions you grant to the bundle's signature, so the bundle must not
# change when the helper does.
clang -fobjc-arc -O2 -Wall -arch arm64 -arch x86_64 -mmacosx-version-min=12.0 \
    -framework Foundation -framework CoreAudio \
    -o "$out/audiotap" "$here/audiotap.m"
codesign --force --sign - --identifier us.feetham.scribeplus.audiotap "$out/audiotap"
lipo -archs "$out/audiotap"
