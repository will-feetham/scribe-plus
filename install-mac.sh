#!/usr/bin/env bash
# Scribe Plus installer and updater for macOS.
#
#   curl -fsSL https://scribeplus.feetham.us/install.sh | bash
#
# Run it again any time to update; your API keys and settings are kept.
#
# What it does, all inside your user account (no admin password):
#   1. downloads the latest Scribe Plus release from GitHub
#   2. sets up a private Python with uv (https://docs.astral.sh/uv) and the
#      app's packages, in ~/Library/Application Support/Scribe Plus
#   3. installs "Scribe Plus.app" in ~/Applications
#   4. optionally starts it when you log in
#   5. starts it; macOS then asks for the permissions a dictation app needs
#
# Options:  --local-whisper   also install offline speech-to-text
#           --no-login-item   don't start at login
#           --yes             don't ask questions (use the defaults)
set -euo pipefail

REPO="will-feetham/scribe-plus"
TARBALL_URL="https://github.com/$REPO/releases/latest/download/ScribePlus-mac.tar.gz"
BASE="${SCRIBE_PLUS_ROOT:-$HOME/Library/Application Support/Scribe Plus}"
APPS="${SCRIBE_PLUS_APPS:-$HOME/Applications}"
APP="$APPS/Scribe Plus.app"
LABEL="us.feetham.scribeplus"
AGENT="$HOME/Library/LaunchAgents/$LABEL.plist"
PYVER="3.12"

LOCAL_WHISPER=0
LOGIN_ITEM=ask
ASSUME_YES=0
for arg in "$@"; do
    case "$arg" in
        --local-whisper) LOCAL_WHISPER=1 ;;
        --no-login-item) LOGIN_ITEM=no ;;
        --yes|-y) ASSUME_YES=1 ;;
        *) echo "Unknown option: $arg" >&2; exit 2 ;;
    esac
done
CI_MODE="${SCRIBE_PLUS_CI:-0}"

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\033[31mError: %s\033[0m\n' "$*" >&2; exit 1; }
# Questions read from the terminal, since this script itself arrives on stdin.
ask() {
    local prompt="$1" default="$2" reply=""
    if [[ "$ASSUME_YES" == 1 || "$CI_MODE" == 1 ]] || ! { : </dev/tty; } 2>/dev/null; then
        echo "$default"; return
    fi
    read -r -p "$prompt" reply </dev/tty || true
    echo "${reply:-$default}"
}

[[ "$(uname -s)" == "Darwin" ]] || die "this installer is for macOS. On Windows, see https://scribeplus.feetham.us"
macos_major="$(sw_vers -productVersion | cut -d. -f1)"
(( macos_major >= 12 )) || die "Scribe Plus needs macOS 12 (Monterey) or newer."

echo
echo "  Scribe Plus for Mac: AI dictation built on Scribe by Avi Ibrahms"
echo

# --- 1. stop a running copy ------------------------------------------------
if [[ "$CI_MODE" != 1 ]]; then
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    pkill -f "Scribe Plus.app/Contents/MacOS/Scribe Plus" 2>/dev/null || true
fi

# --- 2. download the release -------------------------------------------------
mkdir -p "$BASE"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
if [[ -n "${SCRIBE_PLUS_TARBALL:-}" ]]; then
    cp "$SCRIBE_PLUS_TARBALL" "$work/release.tar.gz"
else
    say "Downloading the latest Scribe Plus"
    curl -fsSL --retry 3 -o "$work/release.tar.gz" "$TARBALL_URL" \
        || die "couldn't download $TARBALL_URL"
fi
mkdir "$work/new"
tar -xzf "$work/release.tar.gz" -C "$work/new"
src="$work/new/ScribePlus"
[[ -f "$src/scribe_plus_mac.py" ]] || die "the download doesn't look like Scribe Plus."
version="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$src/scribe_plus/__init__.py")"
say "Installing Scribe Plus $version"

# Keep the API keys (.env) across updates, then swap the app folder in.
if [[ -f "$BASE/app/.env" ]]; then
    cp -p "$BASE/app/.env" "$src/.env"
fi
rm -rf "$BASE/app.old"
[[ -d "$BASE/app" ]] && mv "$BASE/app" "$BASE/app.old"
mv "$src" "$BASE/app"
rm -rf "$BASE/app.old"

# --- 3. Python and packages ----------------------------------------------------
UV="$BASE/uv/uv"
if [[ ! -x "$UV" ]]; then
    say "Installing uv (Python manager) into Scribe Plus's own folder"
    curl -LsSf https://astral.sh/uv/install.sh | env UV_UNMANAGED_INSTALL="$BASE/uv" sh >/dev/null \
        || die "couldn't install uv"
fi
export UV_PYTHON_INSTALL_DIR="$BASE/pythons"
export UV_PYTHON_PREFERENCE=only-managed
say "Setting up Python $PYVER"
"$UV" python install "$PYVER" >/dev/null
py="$("$UV" python find "$PYVER")"
pyhome="$(cd "$(dirname "$py")/.." && pwd)"
libpy="$(ls "$pyhome"/lib/libpython3*.dylib 2>/dev/null | head -n1 || true)"
[[ -n "$libpy" ]] || die "the Python from uv has no shared library at $pyhome/lib"
ln -sfn "$libpy" "$BASE/libpython.dylib"

say "Installing packages (about a minute the first time)"
rm -rf "$BASE/venv"
"$UV" venv --quiet --python "$py" "$BASE/venv"
reqs=(-r "$BASE/app/requirements-plus-mac.txt")
[[ "$LOCAL_WHISPER" == 1 ]] && reqs+=(-r "$BASE/app/requirements-local.txt")
VIRTUAL_ENV="$BASE/venv" "$UV" pip install --quiet "${reqs[@]}"

# --- 4. the app bundle ---------------------------------------------------------
mkdir -p "$APPS"
rm -rf "$APP"
cp -R "$BASE/app/macos/Scribe Plus.app" "$APP"
xattr -dr com.apple.quarantine "$APP" 2>/dev/null || true
codesign --verify "$APP" 2>/dev/null \
    || codesign --force --sign - --identifier "$LABEL" "$APP"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
    -f "$APP" 2>/dev/null || true

# --- 5. Groq key ---------------------------------------------------------------
envfile="$BASE/app/.env"
if ! grep -q '^GROQ_API_KEY=.' "$envfile" 2>/dev/null; then
    echo
    echo "Scribe Plus uses Groq for speech-to-text and cleanup; the free tier covers personal use."
    echo "Get a key at https://console.groq.com/keys (sign in, Create API Key, copy it)."
    key="$(ask "Paste your Groq API key, or press Enter to add it later from the menu bar: " "")"
    if [[ -n "$key" ]]; then
        printf 'GROQ_API_KEY=%s\n' "$key" >> "$envfile"
        chmod 600 "$envfile"
        echo "Saved."
    fi
fi

if [[ "$CI_MODE" == 1 ]]; then
    say "Installed (CI mode: not starting)"
    exit 0
fi

# --- 6. start at login, and start now -------------------------------------------
if [[ "$LOGIN_ITEM" == ask ]]; then
    reply="$(ask "Start Scribe Plus when you log in? [Y/n] " "y")"
    [[ "$reply" =~ ^[Nn] ]] && LOGIN_ITEM=no || LOGIN_ITEM=yes
fi
if [[ "$LOGIN_ITEM" == yes ]]; then
    mkdir -p "$(dirname "$AGENT")"
    cat > "$AGENT" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array><string>$APP/Contents/MacOS/Scribe Plus</string></array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
    <key>ThrottleInterval</key><integer>10</integer>
    <key>LimitLoadToSessionType</key><string>Aqua</string>
    <key>ProcessType</key><string>Interactive</string>
</dict>
</plist>
PLIST
    launchctl bootstrap "gui/$(id -u)" "$AGENT"
else
    rm -f "$AGENT"
    open "$APP"
fi

cat <<EOF

Scribe Plus $version is running: look for the five bars in your menu bar.

macOS will ask for three permissions the first time. Turn Scribe Plus on for:
  1. Input Monitoring  (so it can see the hotkey)
  2. Accessibility     (so it can paste for you)
  3. Microphone        (asked the first time you dictate)
Scribe Plus restarts itself once they're on.

Then click into any text box, hold Right Option, talk, and let go.
To update later, run the same install command again.
EOF
