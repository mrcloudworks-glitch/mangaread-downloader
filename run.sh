#!/usr/bin/env bash
# MangaRead Downloader — double-click launcher for Linux / SteamOS (Desktop Mode).
#
# First run: creates .venv/ and installs dependencies (needs internet once).
# Every run: starts the GUI.
set -euo pipefail

cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"

info() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
fail() {
  printf '\033[1;31merror:\033[0m %s\n\n' "$*" >&2
  read -r -p "Press Enter to close this window..." _ || true
  exit 1
}

# --- find Python 3.9+ -------------------------------------------------------
PY=""
for candidate in python3 python python3.13 python3.12 python3.11 python3.10 python3.9; do
  if command -v "$candidate" >/dev/null 2>&1 \
     && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
    PY="$candidate"
    break
  fi
done
[ -n "$PY" ] || fail "Python 3.9+ not found. Install it first:
  Arch / SteamOS : sudo steamos-readonly disable && sudo pacman -S --needed python tk
  Debian/Ubuntu  : sudo apt install python3 python3-tk python3-venv"

info "Using $("$PY" --version 2>&1) at $(command -v "$PY")"

# --- Tkinter is not pip-installable; check it explicitly --------------------
if ! "$PY" -c 'import tkinter' >/dev/null 2>&1; then
  fail "Tkinter is missing (it comes from the OS, not pip).
  Arch / SteamOS : sudo steamos-readonly disable && sudo pacman -S --needed tk
  Debian/Ubuntu  : sudo apt install python3-tk
  Fedora         : sudo dnf install python3-tkinter"
fi

# --- create / reuse the virtual environment ---------------------------------
VENV=".venv"
VENV_PY="$VENV/bin/python"

if [ ! -x "$VENV_PY" ]; then
  info "Creating virtual environment in $VENV ..."
  "$PY" -m venv "$VENV" || fail "Could not create a virtual environment.
  Arch / SteamOS : sudo pacman -S --needed python   # venv + pip are bundled there"
fi

# --- install dependencies only when needed (marker vs requirements.txt) -----
MARKER="$VENV/.deps-ok"
if [ ! -f "$MARKER" ] || [ requirements.txt -nt "$MARKER" ]; then
  info "Installing dependencies (requests, beautifulsoup4, Pillow) ..."
  "$VENV_PY" -m pip install --quiet --upgrade pip || true
  "$VENV_PY" -m pip install --quiet -r requirements.txt || fail "Dependency install failed (are you online?)"
  touch "$MARKER"
fi

# --- launch ------------------------------------------------------------------
info "Starting MangaRead Downloader ..."
"$VENV_PY" run.py
