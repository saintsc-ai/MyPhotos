# Run worker in foreground (development).

$ErrorActionPreference = "Stop"
Set-Location -Path (Join-Path $PSScriptRoot "..")
$env:PYTHONUTF8 = "1"

. .\.venv\Scripts\Activate.ps1
python -m app.worker.main
