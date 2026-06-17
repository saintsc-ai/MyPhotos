# MyPhotos — Docker setup wizard (Windows / PowerShell)
#
# One-shot interactive installer for users who don't want to hand-write a
# .env, fiddle with override files, or remember the compose commands.
#
#   cd <repo>
#   .\scripts\setup.ps1
#
# What it does:
#   1. Verifies Docker Desktop is installed AND its engine is running.
#   2. If .env already exists, offers to re-use or overwrite.
#   3. Asks for:
#        - photo library source  (local folder | NAS SMB share)
#        - SMB credentials       (only in SMB mode; password via SecureString
#                                 so it never echoes and never lands in the
#                                 command history)
#        - host port             (default 8888)
#        - timezone              (default Asia/Seoul)
#   4. Writes .env + config-docker/local.toml + (SMB only)
#      docker-compose.override.yml + placeholder dir.
#   5. docker compose pull (skipped if the user opted to build locally)
#   6. docker compose up -d
#   7. Polls /healthz until the API answers, then opens the browser.
#
# Re-runnable: every step asks before overwriting an existing file. Hitting
# Enter at most prompts keeps the previous value.

#Requires -Version 5.1
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

function Write-Step([string]$msg) {
    Write-Host ""
    Write-Host "==> $msg" -ForegroundColor Cyan
}

function Write-Ok([string]$msg)   { Write-Host "    $msg" -ForegroundColor Green }
function Write-Warn2([string]$msg) { Write-Host "    $msg" -ForegroundColor Yellow }
function Write-Err([string]$msg)  { Write-Host "    $msg" -ForegroundColor Red }

function Read-Default([string]$prompt, [string]$default) {
    $suffix = if ($default) { " [$default]" } else { "" }
    $val = Read-Host "$prompt$suffix"
    if ([string]::IsNullOrWhiteSpace($val)) { return $default }
    return $val.Trim()
}

function Read-YesNo([string]$prompt, [string]$default = 'y') {
    $hint = if ($default -eq 'y') { '[Y/n]' } else { '[y/N]' }
    while ($true) {
        $v = Read-Host "$prompt $hint"
        if ([string]::IsNullOrWhiteSpace($v)) { $v = $default }
        switch ($v.ToLower()) {
            'y'   { return $true }
            'yes' { return $true }
            'n'   { return $false }
            'no'  { return $false }
        }
    }
}

# Random 48-byte URL-safe-ish secret. CryptoServiceProvider is gone in newer
# .NET cores but the wrapper Get-Random + RandomNumberGenerator is fine.
function New-SecretKey {
    $bytes = New-Object byte[] 48
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $rng.GetBytes($bytes)
    # base64url-ish (drop +, /, =) — same alphabet python secrets.token_urlsafe uses.
    return ([Convert]::ToBase64String($bytes)) -replace '\+','-' -replace '/','_' -replace '=',''
}

# ------------------------------------------------------------------ paths
$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
Set-Location $RepoRoot
Write-Host "MyPhotos Docker setup wizard" -ForegroundColor White
Write-Host "Repo: $RepoRoot"

# ------------------------------------------------------------------ 1) docker
Write-Step "Checking Docker"
try {
    $null = & docker --version 2>$null
    if ($LASTEXITCODE -ne 0) { throw "docker not found on PATH" }
    Write-Ok "docker CLI: $(& docker --version)"
} catch {
    Write-Err "Docker CLI not found. Install Docker Desktop from https://www.docker.com/products/docker-desktop/ then re-run this script."
    exit 1
}

try {
    $null = & docker info 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "engine not reachable"
    }
    Write-Ok "Docker engine is running."
} catch {
    Write-Err "Docker engine isn't reachable. Start Docker Desktop and wait for the whale icon to stop animating, then re-run."
    exit 1
}

try {
    $null = & docker compose version 2>$null
    if ($LASTEXITCODE -ne 0) { throw "compose missing" }
    Write-Ok "compose: $(& docker compose version)"
} catch {
    Write-Err "docker compose plugin missing. Update Docker Desktop and re-run."
    exit 1
}

# ------------------------------------------------------------------ 2) reuse .env?
$EnvPath = Join-Path $RepoRoot ".env"
$Reuse = $false
if (Test-Path $EnvPath) {
    Write-Step ".env already exists"
    if (Read-YesNo "Use the existing .env (skip the prompts and jump to bring-up)?" 'y') {
        $Reuse = $true
    }
}

if (-not $Reuse) {
    Write-Step "Photo library source"
    Write-Host "    1) Local folder (e.g. D:\Photos)"
    Write-Host "    2) Synology / NAS SMB share (//host/share)"
    $mode = Read-Default "    Pick 1 or 2" "1"

    $PhotoRoot   = ""
    $SmbHost     = ""
    $SmbShare    = ""
    $SmbUser     = ""
    $SmbPassPlain = ""
    $UseSmb      = $false

    if ($mode -eq '2') {
        $UseSmb = $true
        $SmbHost  = Read-Default "NAS hostname or IP" "192.168.1.201"
        $SmbShare = Read-Default "SMB share name (no leading slash)" "photo"
        $SmbUser  = Read-Default "SMB username" $env:USERNAME
        $pw = Read-Host "SMB password" -AsSecureString
        $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($pw)
        try {
            $SmbPassPlain = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
        } finally {
            [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
        }
        # The base compose file insists PHOTO_ROOT be set, so we point it at a
        # local dummy dir. The override file replaces /photos with the SMB
        # named volume regardless.
        $PhotoRoot = "./_photos_unused"
        New-Item -ItemType Directory -Force -Path (Join-Path $RepoRoot "_photos_unused") | Out-Null
    } else {
        while ($true) {
            $PhotoRoot = Read-Default "Photo folder (absolute Windows path; forward slashes OK)" "D:/Photos"
            $PhotoRootHost = $PhotoRoot -replace '/','\'
            if (Test-Path -LiteralPath $PhotoRootHost) { break }
            Write-Warn2 "Path doesn't exist."
            if (Read-YesNo "Create it now?" 'y') {
                New-Item -ItemType Directory -Force -Path $PhotoRootHost | Out-Null
                Write-Ok "Created $PhotoRootHost"
                break
            }
        }
    }

    $ApiPort = Read-Default "API port (host side)" "8888"
    $Tz      = Read-Default "Timezone" "Asia/Seoul"

    # Generate a real secret. The committed example uses a placeholder; we
    # bake a real one so first boot doesn't ship with a known token.
    $Secret = New-SecretKey

    # ----- write .env -----------------------------------------------------
    Write-Step "Writing .env"
    $envLines = @(
        "# MyPhotos — generated by scripts/setup.ps1",
        "# Re-run the wizard to regenerate; hand-edit anything you want to pin.",
        "",
        "PHOTO_ROOT=$PhotoRoot",
        "DATA_DIR=./data",
        "CONFIG_DIR=./config-docker",
        "API_PORT=$ApiPort",
        "TZ=$Tz",
        "APP_UID=1000",
        "APP_GID=1000",
        ""
    )
    if ($UseSmb) {
        $envLines += @(
            "# NAS SMB credentials — read by docker-compose.override.yml.",
            "# Treat .env as a secret (it's in .gitignore). Do NOT paste it in",
            "# chat / screenshots / forum posts.",
            "SMB_HOST=$SmbHost",
            "SMB_SHARE=$SmbShare",
            "SMB_USER=$SmbUser",
            "SMB_PASS=$SmbPassPlain"
        )
    }
    [System.IO.File]::WriteAllText($EnvPath, ($envLines -join "`n"), [System.Text.UTF8Encoding]::new($false))
    Write-Ok ".env written"

    # ----- config-docker/local.toml ---------------------------------------
    Write-Step "Writing config-docker/local.toml"
    $ConfigDir = Join-Path $RepoRoot "config-docker"
    New-Item -ItemType Directory -Force -Path $ConfigDir | Out-Null
    # Mirror default.toml so the docker stack has both files in its mount.
    $DefaultSrc = Join-Path $RepoRoot "config\default.toml"
    if (Test-Path $DefaultSrc) {
        Copy-Item -Force $DefaultSrc (Join-Path $ConfigDir "default.toml")
    }
    $localToml = @"
# MyPhotos config (Docker mount target)
#
# Auto-generated by scripts/setup.ps1. Adjust freely; re-running the wizard
# overwrites only when you let it.

[server]
host = "0.0.0.0"
port = 8888

[security]
secret_key = "$Secret"
"@
    # PowerShell 5.1's `Set-Content -Encoding utf8` writes a UTF-8 BOM
    # (EF BB BF). Python 3.11's tomllib rejects that as "Invalid
    # statement at line 1, column 1" and the api container crash-loops
    # at alembic startup. .NET's UTF8Encoding(false) writes plain UTF-8.
    [System.IO.File]::WriteAllText((Join-Path $ConfigDir "local.toml"), $localToml, [System.Text.UTF8Encoding]::new($false))
    Write-Ok "config-docker/local.toml written"

    # ----- compose override (SMB only) ------------------------------------
    $OverridePath = Join-Path $RepoRoot "docker-compose.override.yml"
    if ($UseSmb) {
        Write-Step "Writing docker-compose.override.yml (cifs mount)"
        $override = @"
# Auto-generated by scripts/setup.ps1. Mounts the NAS SMB share as the
# /photos volume in all three services. Edit ".env" (SMB_*) to change
# credentials; re-run the wizard to switch back to a local folder.

services:
  api:
    volumes:
      - photos:/photos:ro
  worker:
    volumes:
      - photos:/photos:ro
  ml-worker:
    volumes:
      - photos:/photos:ro

volumes:
  photos:
    driver: local
    driver_opts:
      type: cifs
      o: "username=`${SMB_USER},password=`${SMB_PASS},uid=1000,gid=1000,ro,vers=3.0,nounix,iocharset=utf8"
      device: "//`${SMB_HOST}/`${SMB_SHARE}"
"@
        [System.IO.File]::WriteAllText($OverridePath, $override, [System.Text.UTF8Encoding]::new($false))
        Write-Ok "override written"
    } elseif (Test-Path $OverridePath) {
        if (Read-YesNo "Existing docker-compose.override.yml found — delete it (local-folder mode doesn't need it)?" 'y') {
            Remove-Item -Force $OverridePath
            Write-Ok "override deleted"
        }
    }

    # Best-effort scrub the password from this PowerShell session.
    Remove-Variable SmbPassPlain -ErrorAction SilentlyContinue
    [System.GC]::Collect()
}

# ------------------------------------------------------------------ 3) bring up
Write-Step "Pulling image (this can take a minute the first time)"
& docker compose pull
if ($LASTEXITCODE -ne 0) {
    Write-Warn2 "Pull failed or no image available — Compose will try to build on up."
}

Write-Step "Starting the stack (docker compose up -d)"
& docker compose up -d
if ($LASTEXITCODE -ne 0) {
    Write-Err "Compose up failed. Run 'docker compose logs' to see why."
    exit 1
}

# ------------------------------------------------------------------ 4) healthcheck poll
$envPort = "8888"
if (Test-Path $EnvPath) {
    foreach ($line in Get-Content $EnvPath) {
        if ($line -match '^\s*API_PORT\s*=\s*(\S+)') { $envPort = $matches[1] }
    }
}
$healthUrl = "http://127.0.0.1:$envPort/healthz"

Write-Step "Waiting for $healthUrl"
$ok = $false
for ($i = 0; $i -lt 60; $i++) {
    try {
        $r = Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 2
        if ($r.StatusCode -eq 200) { $ok = $true; break }
    } catch {
        # not up yet
    }
    Start-Sleep -Seconds 1
}
if ($ok) {
    Write-Ok "API is healthy."
    Write-Step "Opening browser"
    Start-Process "http://127.0.0.1:$envPort"
    Write-Host ""
    Write-Host "Done. To watch logs:  docker compose logs -f api worker" -ForegroundColor White
    Write-Host "To stop the stack:    docker compose down" -ForegroundColor White
} else {
    Write-Err "API didn't respond in 60s. Show its log with:"
    Write-Host "   docker compose logs --tail 80 api" -ForegroundColor Gray
    exit 1
}
