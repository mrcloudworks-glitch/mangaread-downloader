#!/usr/bin/env bash
# Install a .desktop entry + icon so the app shows up in the app menu
# (works on regular Linux desktops and on SteamOS Desktop Mode).
set -euo pipefail
cd "$(dirname "$0")/.."

PREFIX="${PREFIX:-$HOME/.local}"
ICON_DIR="$PREFIX/share/icons/hicolor/256x256/apps"
APP_DIR="$PREFIX/share/applications"

mkdir -p "$ICON_DIR" "$APP_DIR"
cp assets/icon.png "$ICON_DIR/mangaread-downloader.png"
cp packaging/mangaread-downloader.desktop "$APP_DIR/mangaread-downloader.desktop"

# Exec should point at a binary on PATH; adjust if you run from a venv.
if ! command -v mangaread-downloader >/dev/null 2>&1; then
  echo "Note: 'mangaread-downloader' is not on PATH yet."
  echo "Install it first, e.g.:  python3 -m pip install --user ."
fi

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$APP_DIR" || true
fi

echo "Desktop entry installed to $APP_DIR/mangaread-downloader.desktop"
