# Run ML worker in foreground (development).
#
# Picks up classify_objects / classify_embedding / classify_faces jobs
# enqueued from the admin UI. Models must be present in data\models\
# first (see scripts\install-ml-models.sh — runs in Git Bash, or
# download the six ONNX files from the Release page manually).

$ErrorActionPreference = "Stop"
Set-Location -Path (Join-Path $PSScriptRoot "..")
$env:PYTHONUTF8 = "1"

. .\.venv\Scripts\Activate.ps1
python -m app.worker_ml.main
