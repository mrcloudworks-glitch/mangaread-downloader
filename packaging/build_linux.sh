#!/usr/bin/env bash
# Build a single-file Linux executable with PyInstaller.
# Run from anywhere:  packaging/build_linux.sh
set -euo pipefail
cd "$(dirname "$0")/.."

python3 -m pip install --upgrade pyinstaller requests beautifulsoup4 Pillow

python3 -m PyInstaller --noconfirm --clean \
  --onefile \
  --windowed \
  --name "mangaread-downloader" \
  --icon "assets/icon.png" \
  --add-data "mangaread_downloader/assets:assets" \
  run.py

echo
echo "Done! Find it at  dist/mangaread-downloader"
echo "Install for all users :  sudo cp dist/mangaread-downloader /usr/local/bin/"
echo "Install desktop entry :  packaging/install_desktop.sh"
