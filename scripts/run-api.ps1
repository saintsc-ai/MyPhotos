# Run API in foreground (development).

$ErrorActionPreference = "Stop"
Set-Location -Path (Join-Path $PSScriptRoot "..")
$env:PYTHONUTF8 = "1"

. .\.venv\Scripts\Activate.ps1
uvicorn app.api.main:app --host 127.0.0.1 --port 8888 --reload
