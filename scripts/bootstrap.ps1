# Bootstrap a MyPhotos checkout on Windows (development).
#
# Steps:
#   1. Pick Python interpreter (env PYTHON_BIN overrides; else py launcher)
#   2. Create .venv
#   3. Install project (editable) + apply Alembic migrations
#   4. Copy local.example.toml -> local.toml if missing

# Note: deliberately NOT setting $ErrorActionPreference = "Stop".
# In Windows PowerShell 5.1, that combined with native commands
# writing to stderr (alembic + pip both log INFO/WARNING there)
# wraps each stderr line as a NativeCommandError → terminates the
# script even when the exe exited 0. We use explicit -ErrorAction
# Stop on cmdlets that need it, and $LASTEXITCODE checks after
# every native call instead.

Set-Location -ErrorAction Stop -Path (Join-Path $PSScriptRoot "..")
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

# Use the venv's python explicitly for every install step. We can't
# rely on Activate.ps1 + bare `pip` / `alembic` because:
#   - $LASTEXITCODE on a failed pip install would otherwise let the
#     script march on to `alembic upgrade head` and fail there with
#     the misleading "alembic not found" — even with explicit checks
#     after each step (the next call still needs a working binary).
#   - Activate.ps1 changes PATH for the CURRENT shell only; if the
#     user re-runs from a fresh terminal they'd be relying on global
#     state. Calling $VenvPy directly side-steps both.
# Check $LASTEXITCODE after every native call to abort at the actual
# root cause instead of cascading.
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
    Copy-Item -ErrorAction Stop config\local.example.toml config\local.toml
    Write-Output "==> created config\local.toml -- edit it before starting the API"
}

# 5. Runtime dirs
New-Item -ErrorAction Stop -ItemType Directory -Force -Path data\thumbs, data\logs, data\state, data\trash | Out-Null

Write-Output "==> bootstrap complete"
Write-Output "    Next: edit config\local.toml, then run scripts\run-api.ps1 / run-worker.ps1"
