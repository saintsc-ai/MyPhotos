# Build MyPhotos desktop client into dist\MyPhotos.exe (single file).
#
# Run from PowerShell in this folder:   .\build.ps1
# First run takes ~5-10 min (PySide6 install + Qt6/Chromium bundling).
# Subsequent runs are faster thanks to PyInstaller's analysis cache
# under .\build\, but cleaning between runs catches stale leftovers.

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$venv = ".\.venv"
if (-not (Test-Path $venv)) {
    Write-Host "==> Creating venv at $venv"
    python -m venv $venv
}

$py = Join-Path $venv "Scripts\python.exe"

Write-Host "==> Updating pip + installing build deps"
& $py -m pip install --upgrade pip
& $py -m pip install -r requirements.txt

Write-Host "==> Cleaning previous build output"
Remove-Item -Recurse -Force .\build, .\dist -ErrorAction SilentlyContinue

Write-Host "==> Running PyInstaller"
& $py -m PyInstaller --clean myphotos.spec

$out = Join-Path $PSScriptRoot "dist\MyPhotos.exe"
if (Test-Path $out) {
    $size = [math]::Round((Get-Item $out).Length / 1MB, 1)
    Write-Host ""
    Write-Host "==> Done: $out  ($size MB)"
} else {
    Write-Host ""
    Write-Host "==> Build finished but $out is missing — check PyInstaller output above."
    exit 1
}
