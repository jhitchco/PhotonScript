# PhotonScript wrapper for the SCOPE PC.
#
# Normally started at boot by the "PhotonScript" scheduled task
# (deploy\install-autostart.ps1). To run it by hand in a console instead:
#   powershell -ExecutionPolicy Bypass -File C:\astro\PhotonScript\deploy\run-photonscript.ps1
#
# Loop: git pull, then `photonscript supervise`. The supervisor keeps the
# service up (restart on crash with backoff, crash-loop cap, Pushover alerts).
# When the service asks for an update (exit 42: the dashboard "Pull latest &
# restart" button or POST /api/update) the supervisor exits 42 and this loop
# pulls and starts a fresh supervisor, so new supervisor code runs too.
# Any other supervisor exit (operator stop, crash-loop give-up, already
# running) ends the wrapper.
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
$session = (Get-Process -Id $PID).SessionId
Write-Log "wrapper start (user $env:USERNAME, repo $Repo, mode $Mode, pid $PID, session $session, launcher $Launcher)"

while ($true) {
    $before = (git -C $Repo rev-parse --short HEAD 2>$null)
    $pullOut = (git -C $Repo pull --ff-only 2>&1 | Out-String).Trim()
    $pullRc = $LASTEXITCODE
    if ($pullRc -ne 0) {
        Write-Log "git pull failed (exit $pullRc): $pullOut"
        Write-Log "starting the code already on disk ($before)"
        & $Exe notify "git pull failed on $env:COMPUTERNAME (exit $pullRc); running $before" --title "PhotonScript update failed" --priority 1 | Out-Null
    } else {
        $after = (git -C $Repo rev-parse --short HEAD 2>$null)
        if ($after -ne $before) { Write-Log "updated $before -> $after" }
    }

    & $Exe supervise --mode $Mode
    $rc = $LASTEXITCODE
    if ($rc -ne 42) {
        Write-Log "supervisor exited with code $rc; wrapper done"
        exit $rc
    }
    Write-Log "update requested (42): pulling and restarting"
}
