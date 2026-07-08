#!/usr/bin/env bash
# Build GridBotLauncher.app and GridBotLauncher.dmg (macOS).
# Must be run ON a Mac (PyInstaller cannot cross-compile):
#
#   bash build_macos.sh
#
# Output: dist/GridBotLauncher.dmg — open it, drag the app to Applications.
# Note: the app is unsigned; first launch needs right-click > Open (Gatekeeper).
set -euo pipefail
cd "$(dirname "$0")"

python3 -m pip install --upgrade pyinstaller

python3 -m PyInstaller --noconfirm --clean --windowed \
    --name GridBotLauncher \
    --add-data "web:web" \
    --python-option u \
    launcher.py

hdiutil create -volname "Grid Bot Launcher" \
    -srcfolder "dist/GridBotLauncher.app" \
    -ov -format UDZO "dist/GridBotLauncher.dmg"

echo
echo "done: $(pwd)/dist/GridBotLauncher.dmg"
