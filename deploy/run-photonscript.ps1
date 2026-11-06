# PhotonScript wrapper for the SCOPE PC.
#
# Normally started at boot by the "PhotonScript" scheduled task
# (deploy\install-autostart.ps1). To run it by hand in a console instead:
#   powershell -ExecutionPolicy Bypass -File C:\astro\PhotonScript\deploy\run-photonscript.ps1
#
# Loop: `photonscript self-update`, then `photonscript supervise`. The
# supervisor keeps the service up (restart on crash with backoff, crash-loop
# cap, Pushover alerts). When the service asks for an update (exit 42: the
# dashboard "Pull latest & restart" button or POST /api/update) the
# supervisor exits 42 and this loop updates and starts a fresh supervisor, so
# new supervisor code runs too.
#
# PS-58 safe update: self-update runs with the code ALREADY on disk. It
# fetches, checks the upstream commit out into a staging worktree, imports
# every module from it (optionally runs the fast tests), and only then
# fast-forwards; any failure keeps the old code and alerts. After a switch the
# supervisor waits for /api/health to report the new SHA; if it does not, the
# supervisor exits 43 and this loop runs `git reset --hard <previous SHA>`,
# records the rollback (Pushover) and restarts the old code without updating.
# The same rollback happens if the new supervisor itself dies right after an
# update. Any other supervisor exit (operator stop, crash-loop give-up,
# already running) ends the wrapper.
#
# Stop:     photonscript stop            (stays down until started again)
# Restart:  photonscript restart
# Start:    Start-ScheduledTask PhotonScript   (or run this script)
# Logs:     photonscript monitor         (service log, colorized)
#           <data_dir>\logs\supervisor.log, <data_dir>\logs\wrapper.log

param(
    [string]$Repo = "C:\astro\PhotonScript",
    [string]$Exe  = "C:\astro\venv\Scripts\photonscript.exe",
    [string]$Mode = "full",
    # Tag shown in the service log and GET /api/health (PS-55): "console" when
    # run by hand, "task-<LogonType>" when install-autostart.ps1 starts it.
    [string]$Launcher = "console"
)

# Run from the repo root so the app finds .env (pydantic reads ".env" relative
# to the current directory).
Set-Location $Repo

# data_dir: default %USERPROFILE%\.photonscript, or PS_DATA_DIR from .env
$dataDir = Join-Path $env:USERPROFILE ".photonscript"
$envFile = Join-Path $Repo ".env"
if (Test-Path $envFile) {
    $m = Select-String -Path $envFile -Pattern '^\s*PS_DATA_DIR\s*=\s*(.+?)\s*$' | Select-Object -First 1
    if ($m) { $dataDir = $m.Matches[0].Groups[1].Value.Trim('"', "'") }
}
$logDir = Join-Path $dataDir "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "wrapper.log"
if ((Test-Path $log) -and ((Get-Item $log).Length -gt 1MB)) {
    Move-Item -Force $log "$log.1"
}

function Write-Log([string]$msg) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Write-Host $line
    Add-Content -Path $log -Value $line -Encoding UTF8
}

$env:PS_LAUNCHER = $Launcher
# Tells `photonscript supervise` that this wrapper handles exit 43 (rollback)
$env:PS_WRAPPER_ROLLBACK = "1"
$session = (Get-Process -Id $PID).SessionId
Write-Log "wrapper start (user $env:USERNAME, repo $Repo, mode $Mode, pid $PID, session $session, launcher $Launcher)"

$stateFile = Join-Path $dataDir "update_state.json"
function Read-UpdateState {
    try { return (Get-Content -Raw -Path $stateFile -ErrorAction Stop | ConvertFrom-Json) }
    catch { return $null }
}

$skipUpdate = $false
while ($true) {
    $before = (git -C $Repo rev-parse --short HEAD 2>$null)
    if ($skipUpdate) {
        Write-Log "not updating after a rollback; starting $before"
        $skipUpdate = $false
    } else {
        $upOut = (& $Exe self-update 2>&1 | Out-String).Trim()
        $upRc = $LASTEXITCODE
        switch ($upRc) {
            0       { Write-Log "updated: $upOut (verifying after start)" }
            10      { }
            11      { Write-Log "update deferred: $upOut" }
            2       { Write-Log "update refused, starting the code already on disk ($before): $upOut" }
            default { Write-Log "self-update failed to run (exit $upRc), starting the code already on disk ($before): $upOut" }
        }
    }

    $startedAt = Get-Date
    & $Exe supervise --mode $Mode
    $rc = $LASTEXITCODE
    $ranS = ((Get-Date) - $startedAt).TotalSeconds
    if ($rc -eq 42) {
        Write-Log "update requested (42): updating and restarting"
        continue
    }

    # PS-58 rollback: the supervisor says the new code never came up (43), or
    # an update is still pending verification and the supervisor itself died
    # within 10 min (new supervisor code that cannot run at all).
    $st = Read-UpdateState
    $pending = ($st -ne $null) -and ($st.status -eq "pending" -or $st.status -eq "failed")
    if ($rc -eq 43 -or ($pending -and $ranS -lt 600 -and $rc -ne 0 -and $rc -ne 3)) {
        $prev = if ($st -ne $null) { "$($st.prev)" } else { "" }
        $bad = if ($st -ne $null) { "$($st.target)" } else { "" }
        if (-not $prev) {
            Write-Log "supervisor exited $rc after an update but no previous SHA is recorded; wrapper done"
            exit $rc
        }
        $resetOut = (git -C $Repo reset --hard $prev 2>&1 | Out-String).Trim()
        if ($LASTEXITCODE -ne 0) {
            Write-Log "ROLLBACK FAILED: git reset --hard $prev (exit $LASTEXITCODE): $resetOut; wrapper done"
            exit $rc
        }
        $why = if ($rc -eq 43 -and $st.reason) { "$($st.reason)" } else { "supervisor exited $rc right after the update" }
        Write-Log "ROLLED BACK $bad -> $prev ($why)"
        & $Exe rollback-done --reason $why | Out-Null
        $skipUpdate = $true
        continue
    }

    Write-Log "supervisor exited with code $rc; wrapper done"
    exit $rc
}
