# MyPhotos service control for Windows dev.
#
# Usage:
#   .\scripts\myphotos.ps1 status        # show running components + PIDs
#   .\scripts\myphotos.ps1 start         # start any that aren't running
#   .\scripts\myphotos.ps1 stop          # stop everything (including zombies)
#   .\scripts\myphotos.ps1 restart       # stop + start
#
# Each component (api / worker / ml-worker) is launched in its own
# minimised PowerShell window so logs stay visible if you need to peek.
# Stop matches by command-line pattern, which catches orphans left behind
# when an earlier terminal was closed without Ctrl+C — the bug that
# silently broke indexing during the 2026-05-31 troubleshooting session.

param(
    [Parameter(Position=0)]
    [ValidateSet('status', 'start', 'stop', 'restart')]
    [string]$Action = 'status'
)

# Don't $ErrorActionPreference=Stop here — same PS 5.1 native-stderr
# trap that broke bootstrap.ps1. Check return values explicitly.

$AppDir = Split-Path -Parent $PSScriptRoot
Set-Location -ErrorAction Stop $AppDir

# Component table: (display name, run-*.ps1 path, command-line match
# used by stop / status to identify the process). The match string
# is what Get-CimInstance reads from the process's CommandLine.
$Components = @(
    @{ Name='api';       Script='.\scripts\run-api.ps1';       Match='uvicorn.exe.*app\.api\.main' },
    @{ Name='worker';    Script='.\scripts\run-worker.ps1';    Match='-m app\.worker\.main' },
    @{ Name='ml-worker'; Script='.\scripts\run-ml-worker.ps1'; Match='-m app\.worker_ml\.main' }
)

function Get-Procs($matchPattern) {
    # Pair = venv launcher (python.exe in .venv\Scripts) + real interpreter.
    # Both match the same command-line pattern. Return both PIDs so stop
    # kills the pair atomically (killing only the launcher leaves the
    # interpreter as an orphan).
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine -match $matchPattern }
}

function Cmd-Status {
    Write-Output ""
    Write-Output "MyPhotos services:"
    Write-Output ""
    foreach ($c in $Components) {
        $procs = @(Get-Procs $c.Match)
        if ($procs.Count -eq 0) {
            Write-Output ("  {0,-12} stopped" -f $c.Name)
        } else {
            $pids = ($procs | ForEach-Object { $_.ProcessId }) -join ', '
            $started = ($procs | Select-Object -First 1).CreationDate
            $uptime = (Get-Date) - $started
            $upStr = "{0:dd}d {0:hh}h {0:mm}m" -f $uptime
            $countNote = if ($procs.Count -gt 2) {
                " ⚠ {0} processes (expected 2)" -f $procs.Count
            } else { "" }
            Write-Output ("  {0,-12} running   PIDs={1}  started={2:yyyy-MM-dd HH:mm:ss}  up {3}{4}" -f $c.Name, $pids, $started, $upStr, $countNote)
        }
    }
    Write-Output ""
}

function Cmd-Start {
    foreach ($c in $Components) {
        $procs = @(Get-Procs $c.Match)
        if ($procs.Count -gt 0) {
            Write-Output "  $($c.Name): already running (PID $($procs[0].ProcessId))"
            continue
        }
        Write-Output "  $($c.Name): starting..."
        # New minimised PowerShell window per component so the log
        # is visible if you ever want to peek, but it isn't in your face.
        # -NoExit keeps the window open after the worker exits (so an
        # error message lingers instead of disappearing).
        Start-Process powershell -WindowStyle Minimized -ArgumentList @(
            '-NoExit',
            '-ExecutionPolicy', 'Bypass',
            '-Command', "& '$($c.Script)'"
        )
        Start-Sleep -Milliseconds 600
    }
    Write-Output ""
    Cmd-Status
}

function Cmd-Stop {
    # Two-phase kill. Stop-Process leaves multiprocessing spawn children
    # alive as orphans (uvicorn + Python's multiprocessing fork a pool
    # that detaches from the parent on Windows), so a previous flow of
    # "open new terminal → run-api.ps1 → close old terminal" left zombie
    # listeners squatting on port 8888 with the OLD code, while the new
    # process started up fine but couldn't bind. taskkill /F /T walks
    # the full process tree.
    $totalKilled = 0
    foreach ($c in $Components) {
        $procs = @(Get-Procs $c.Match)
        if ($procs.Count -eq 0) {
            Write-Output "  $($c.Name): not running"
            continue
        }
        $pids = ($procs | ForEach-Object { $_.ProcessId })
        Write-Output ("  {0}: killing PIDs {1}" -f $c.Name, ($pids -join ', '))
        foreach ($p in $pids) {
            taskkill /F /T /PID $p 2>&1 | Out-Null
        }
        $totalKilled += $pids.Count
    }
    # Sweep orphans: any python.exe spawn child whose parent is already
    # gone (status='not found' from the taskkills above). multiprocessing
    # detaches these so they survive the supervisor — match by command
    # line, not parent PID.
    $orphans = @(
        Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
            Where-Object { $_.CommandLine -and $_.CommandLine -match 'multiprocessing\.spawn' }
    )
    if ($orphans.Count -gt 0) {
        Write-Output ("  zombie sweep: {0} multiprocessing.spawn orphan(s)" -f $orphans.Count)
        foreach ($o in $orphans) {
            taskkill /F /PID $o.ProcessId 2>&1 | Out-Null
            $totalKilled++
        }
    }
    if ($totalKilled -gt 0) {
        Start-Sleep -Seconds 1
    }
    Write-Output ""
    Write-Output "  $totalKilled process(es) terminated."
}

switch ($Action) {
    'status'  { Cmd-Status }
    'start'   { Cmd-Start }
    'stop'    { Cmd-Stop }
    'restart' { Cmd-Stop; Start-Sleep -Seconds 1; Cmd-Start }
}
