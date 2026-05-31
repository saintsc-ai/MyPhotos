# Bootstrap a MyPhotos checkout on Windows (development).
#
# Steps:
#   1. Pick Python interpreter (env PYTHON_BIN overrides; else py launcher)
#   2. Create .venv
#   3. Install project (editable) + apply Alembic migrations
#   4. Copy local.example.toml -> local.toml if missing

$ErrorActionPreference = "Stop"

Set-Location -Path (Join-Path $PSScriptRoot "..")
$AppDir = (Get-Location).Path
Write-Output "==> bootstrapping in $AppDir"

# 1. Pick Python
$Python = $env:PYTHON_BIN
if (-not $Python) {
    foreach ($candidate in @("py -3.13", "py -3.12", "py -3.11", "python")) {
        $parts = $candidate.Split(" ")
        $exe = $parts[0]
        if (Get-Command $exe -ErrorAction SilentlyContinue) {
            try {
                $ver = & $exe @($parts | Select-Object -Skip 1) -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
                if ($ver -match "^3\.(1[1-9]|[2-9]\d)$") {
                    $Python = $candidate
                    break
                }
            } catch {}
        }
    }
}
if (-not $Python) {
    throw "No Python 3.11+ found. Install Python and re-run, or set `$env:PYTHON_BIN."
}
Write-Output "==> using $Python"

# 2. Create venv
if (-not (Test-Path ".venv")) {
    $pyParts = $Python.Split(" ")
    & $pyParts[0] @($pyParts | Select-Object -Skip 1) -m venv .venv
}
. .\.venv\Scripts\Activate.ps1

# Use the venv's python explicitly for every install step. PowerShell's
# $ErrorActionPreference=Stop catches cmdlet failures but NOT native-
# command non-zero exits, so a pip install error would otherwise let
# the script march on to `alembic upgrade head` and fail there with
# the misleading "alembic not found". Check $LASTEXITCODE after each
# native call to abort at the actual root cause.
$VenvPy = Join-Path $AppDir ".venv\Scripts\python.exe"

& $VenvPy -m pip install -U pip wheel
if ($LASTEXITCODE -ne 0) { throw "pip install (pip + wheel) failed (exit $LASTEXITCODE)" }

# 3. Install project + run migrations
& $VenvPy -m pip install -e .
if ($LASTEXITCODE -ne 0) {
    throw "pip install -e . failed (exit $LASTEXITCODE). If the error mentions onnxruntime / numpy / tokenizers wheel resolution, see the ML deps comment in pyproject.toml — your Python version may need different pins than the DSM defaults."
}

& $VenvPy -m alembic upgrade head
if ($LASTEXITCODE -ne 0) { throw "alembic upgrade head failed (exit $LASTEXITCODE)" }

# 4. Local config
if (-not (Test-Path "config\local.toml")) {
    Copy-Item config\local.example.toml config\local.toml
    Write-Output "==> created config\local.toml -- edit it before starting the API"
}

# 5. Runtime dirs
New-Item -ItemType Directory -Force -Path data\thumbs, data\logs, data\state, data\trash | Out-Null

Write-Output "==> bootstrap complete"
Write-Output "    Next: edit config\local.toml, then run scripts\run-api.ps1 / run-worker.ps1"
