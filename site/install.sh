#!/bin/bash
# Scribe Plus for macOS. Fetches the installer that matches the latest
# release and runs it. Usage:  curl -fsSL https://scribeplus.feetham.us/install.sh | bash
set -euo pipefail
tmp="$(mktemp -t scribe-plus-install)"
trap 'rm -f "$tmp"' EXIT
curl -fsSL --retry 3 -o "$tmp" \
    "https://github.com/will-feetham/scribe-plus/releases/latest/download/install-mac.sh"
bash "$tmp" "$@"
