#!/bin/bash
# SoriTaker: local, user-owned installation. Does not use sudo or Homebrew.
set -euo pipefail
IFS=$'\n\t'
SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_ROOT="$HOME/Library/Application Support/SoriTaker"
LEGACY_ROOT="$HOME/Library/Application Support/SoriNote"
# Reuse the old path: moving a Python environment can break absolute references.
if [[ ! -e "$APP_ROOT" && -d "$LEGACY_ROOT" ]]; then
  APP_ROOT="$LEGACY_ROOT"
fi
INSTALL_ROOT="$APP_ROOT/installation"
SOURCE_COPY="$INSTALL_ROOT/source"
UPDATE_ONLY=0
if [[ "${1:-}" == "--update-only" ]]; then
  UPDATE_ONLY=1
elif [[ $# -gt 0 ]]; then
  printf 'Unknown option: %s\n' "$1"
  exit 1
fi
BACKUP=""
TARGET="$HOME/Applications/SoriTaker.app"
PREVIOUS="$TARGET"
LEGACY_APP="$HOME/Applications/SoriNote.app"
if [[ ! -e "$TARGET" && -d "$LEGACY_APP" ]]; then
  PREVIOUS="$LEGACY_APP"
fi
# Clear flags left by previous releases; local model loading needs no network switch.
unset HF_HUB_OFFLINE TRANSFORMERS_OFFLINE
export UV_PYTHON_INSTALL_DIR="$INSTALL_ROOT/python"
export UV_CACHE_DIR="$INSTALL_ROOT/cache"
export HF_HUB_DISABLE_TELEMETRY=1
export DO_NOT_TRACK=1
mkdir -p "$INSTALL_ROOT" "$APP_ROOT/logs"
LOG_FILE="$APP_ROOT/logs/install-$(date +%Y%m%d-%H%M%S).log"
exec > >(tee -a "$LOG_FILE") 2>&1
on_error() {
  status=$?
  if [[ -n "$BACKUP" && -d "$BACKUP" && ! -e "$PREVIOUS" && ! -e "$TARGET" ]]; then
    mv "$BACKUP" "$PREVIOUS" || true
  fi
  printf '\nSetup could not finish. Your saved recordings and existing app are preserved.\n'
  printf 'Error log: %s\n' "$LOG_FILE"
  printf 'Copy the final messages from this window when reporting the error.\n'
  exit "$status"
}
trap on_error ERR

printf '\nSoriTaker — Local recording and transcription for Apple Silicon Macs\n\n'
if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
  printf 'Run this on an Apple Silicon Mac with Rosetta disabled for Terminal.\n'
  exit 1
fi
MAC_MAJOR="$(sw_vers -productVersion | cut -d. -f1)"
if [[ "$MAC_MAJOR" -lt 14 ]]; then
  printf 'macOS Sonoma 14 or later is required.\n'
  exit 1
fi
if ! /usr/bin/xcode-select -p >/dev/null 2>&1; then
  if [[ "$UPDATE_ONLY" -eq 1 ]]; then
    printf 'Existing build tools were not found. Update stopped; your existing app is preserved.\n'
    exit 1
  fi
  printf 'Install the macOS Command Line Tools once to build the app.\n'
  /usr/bin/xcode-select --install || true
  printf 'Complete the installation in the macOS dialog, then run Install.command again.\n'
  exit 0
fi
if /usr/bin/pgrep -x SoriTaker >/dev/null 2>&1 || /usr/bin/pgrep -x SoriNote >/dev/null 2>&1; then
  printf 'Quit SoriTaker or SoriNote completely with Command-Q, then try again.\n'
  exit 1
fi
if [[ "$UPDATE_ONLY" -eq 1 ]]; then
  PYTHON="$INSTALL_ROOT/venv/bin/python"
  if [[ ! -x "$PYTHON" || ! -d "$PREVIOUS" ]]; then
    printf 'An existing SoriTaker or SoriNote installation was not found. Update stopped because the local build environment is required.\n'
    printf 'Expected installation path: %s\n' "$INSTALL_ROOT"
    exit 1
  fi
  "$PYTHON" -c 'import PyInstaller, PySide6, mlx_whisper, sherpa_onnx, sounddevice, soundfile, imageio_ffmpeg'
  printf 'Update mode: reusing the existing Python environment, libraries, and models. No downloads.\n'
fi
if [[ ! -f "$SOURCE_DIR/src/main.py" ]]; then
  printf 'Extract the ZIP before running Install.command or Update.command.\n'
  exit 1
fi
# Hold a process-owned installation lock. A crashed installer may be retried.
LOCK_DIR="$INSTALL_ROOT/install.lock"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  previous_pid="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
  if [[ "$previous_pid" =~ ^[0-9]+$ ]] && kill -0 "$previous_pid" 2>/dev/null; then
    printf 'Setup is already running. Check the other setup window.\n'
    exit 1
  fi
  rm -f "$LOCK_DIR/pid"
  rmdir "$LOCK_DIR"
  mkdir "$LOCK_DIR"
fi
printf '%s' "$$" > "$LOCK_DIR/pid"
trap 'rm -f "$LOCK_DIR/pid"; rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT
/usr/bin/caffeinate -i -w $$ >/dev/null 2>&1 &

printf '[1/6] Preparing the dedicated runtime\n'
UV="$INSTALL_ROOT/uv/uv"
if [[ "$UPDATE_ONLY" -eq 0 ]]; then
if [[ ! -x "$UV" ]]; then
  ARCHIVE="$INSTALL_ROOT/uv-aarch64-apple-darwin.tar.gz"
  curl --fail --location --retry 3 --connect-timeout 30 \
    'https://github.com/astral-sh/uv/releases/download/0.8.15/uv-aarch64-apple-darwin.tar.gz' -o "$ARCHIVE"
  curl --fail --location --retry 3 --connect-timeout 30 \
    'https://github.com/astral-sh/uv/releases/download/0.8.15/uv-aarch64-apple-darwin.tar.gz.sha256' -o "$ARCHIVE.sha256"
  EXPECTED_HASH="$(awk '{print $1; exit}' "$ARCHIVE.sha256")"
  printf '%s  %s\n' "$EXPECTED_HASH" "$ARCHIVE" | shasum -a 256 -c -
  mkdir -p "$INSTALL_ROOT/uv"
  tar -xzf "$ARCHIVE" --strip-components=1 -C "$INSTALL_ROOT/uv"
  rm -f "$ARCHIVE" "$ARCHIVE.sha256"
fi
"$UV" python install 3.12
if [[ ! -x "$INSTALL_ROOT/venv/bin/python" ]]; then
  "$UV" venv --python 3.12 --managed-python "$INSTALL_ROOT/venv"
fi
else
  printf 'Reusing the existing runtime\n'
fi
PYTHON="$INSTALL_ROOT/venv/bin/python"
# SoriTaker's source is copied separately from records and model data.
if [[ "$SOURCE_DIR" != "$SOURCE_COPY" ]]; then
  mkdir -p "$SOURCE_COPY/src" "$SOURCE_COPY/assets"
  cp "$SOURCE_DIR"/src/*.py "$SOURCE_COPY/src/"
  cp "$SOURCE_DIR/assets/SoriTaker.png" "$SOURCE_DIR/assets/SoriTaker.icns" "$SOURCE_COPY/assets/"
  cp "$SOURCE_DIR/requirements.txt" "$SOURCE_DIR/requirements-macos.lock" "$SOURCE_DIR/SoriTaker.spec" "$SOURCE_DIR/entitlements.plist" \
     "$SOURCE_DIR/THIRD_PARTY_NOTICES.md" "$SOURCE_COPY/"
fi

printf '\n[2/6] Preparing transcription and GUI dependencies\n'
if [[ "$UPDATE_ONLY" -eq 0 ]]; then
"$UV" pip install --python "$PYTHON" -r "$SOURCE_COPY/requirements-macos.lock"
"$UV" pip freeze --python "$PYTHON" > "$APP_ROOT/logs/installed-packages.txt"
else
  printf 'Installation skipped: reusing installed dependencies\n'
fi

printf '\n[3/6] Preparing offline models (Fast + speaker labels)\n'
MODELS_OK=1
if [[ "$UPDATE_ONLY" -eq 0 ]]; then
if ! "$PYTHON" "$SOURCE_COPY/src/main.py" --prepare turbo --prepare diarization; then
  MODELS_OK=0
  printf '\nSome model downloads did not finish. Retry from Options → Download after setup.\n'
fi
else
  printf 'Downloads skipped: reusing local models\n'
fi

printf '\n[4/6] Building the app for this Mac\n'
"$PYTHON" -m PyInstaller --noconfirm --distpath "$INSTALL_ROOT/dist" --workpath "$INSTALL_ROOT/build" "$SOURCE_COPY/SoriTaker.spec"
BUILT_APP="$INSTALL_ROOT/dist/SoriTaker.app"

printf '\n[5/6] Checking the built app and GPU execution\n'
CHECK_JSON="$APP_ROOT/logs/app-check.json"
"$BUILT_APP/Contents/MacOS/SoriTaker" --self-check "$CHECK_JSON"
"$PYTHON" -c 'import json,sys; r=json.load(open(sys.argv[1])); print(json.dumps(r,ensure_ascii=False,indent=2)); sys.exit(0 if r.get("ok") else 1)' "$CHECK_JSON"

printf '\n[6/6] Installing in your Applications folder\n'
mkdir -p "$HOME/Applications"
STAGING="$HOME/Applications/SoriTaker-new-$$.app"
/usr/bin/ditto "$BUILT_APP" "$STAGING"
if [[ -d "$PREVIOUS" ]]; then
  BACKUP="${PREVIOUS%.app}-backup-$(date +%Y%m%d-%H%M%S)-$$.app"
  mv "$PREVIOUS" "$BACKUP"
fi
mv "$STAGING" "$TARGET"
# Remove only regenerable build and download caches owned by this installer.
rm -rf "$INSTALL_ROOT/build" "$INSTALL_ROOT/dist" "$INSTALL_ROOT/cache"
printf '\nSetup complete: %s\n' "$TARGET"
if [[ -n "$BACKUP" ]]; then
  printf 'Previous app backup: %s\n' "$BACKUP"
fi
printf 'Recordings and transcripts stay on this Mac. Keep the app in the Dock for easy access.\n'
if [[ "$MODELS_OK" -eq 0 ]]; then
  printf 'Connect to the internet and use Options → Download to finish downloading the models.\n'
fi
# Clear legacy flags inherited from an older installer before launching the app.
env -u HF_HUB_OFFLINE -u TRANSFORMERS_OFFLINE open "$TARGET"
