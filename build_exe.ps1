# Build the single-file Windows .exe -> dist\NordicOTAFlasher.exe
# Usage:  .\build_exe.ps1   (run from the project root, with the .venv created)
$ErrorActionPreference = "Stop"
$base = $PSScriptRoot
$py = Join-Path $base ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "No .venv found. Create it first:  python -m venv .venv; .\.venv\Scripts\python.exe -m pip install -r requirements.txt pyinstaller"
    exit 1
}
& $py -m pip install --quiet pyinstaller
& $py -m PyInstaller --noconfirm --clean (Join-Path $base "NordicOTAFlasher.spec")
$exe = Join-Path $base "dist\RFLab.io OTA Flasher.exe"
if (Test-Path $exe) {
    $mb = [math]::Round((Get-Item $exe).Length / 1MB, 1)
    Write-Host "Built: $exe ($mb MB)"
} else {
    Write-Host "Build failed - no exe produced."
    exit 1
}
