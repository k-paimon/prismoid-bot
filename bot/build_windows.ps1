# Build GridBotLauncher.exe (Windows) — a single portable exe containing the
# control panel, the web UI server, the bot API, and the bot itself.
#
#   powershell -ExecutionPolicy Bypass -File build_windows.ps1
#
# Output: dist\GridBotLauncher.exe  (double-click to run; no Python needed)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

py -m pip install --upgrade pyinstaller

py -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name GridBotLauncher `
    --add-data "web;web" `
    --python-option u `
    launcher.py

Write-Host ""
Write-Host "done: $PSScriptRoot\dist\GridBotLauncher.exe"
