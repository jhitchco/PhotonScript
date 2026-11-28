# Register (or remove) the "PhotonScript AutoIntegrate" scheduled task on the
# DESKTOP (PS-161). Every 30 min it runs
#   photonscript autointegrate --once
# which integrates a goal when new approved subs have landed and Syncthing has
# settled, blends two-rig goals, and sends a review JPG with Pushover. It does
# nothing while PixInsight is open and starts at most one PixInsight at a time.
#
# Not installed by the build: run it yourself once, from a normal (not
# elevated) PowerShell on the desktop, as the user who runs PixInsight:
#   powershell -ExecutionPolicy Bypass -File C:\dev\PhotonScript\deploy\install-autointegrate-task.ps1
#   ... -DryRun          the task runs with --dry-run (decisions only; try this first)
#   ... -IntervalMin 60  another interval
#   ... -Uninstall       remove the task
#
# Interactive logon on purpose: PixInsight needs the desktop session, and the
# task must see the same Library mirror (D:\ninashare\Library) and .env.
# Output goes to <Repo>\logs\autointegrate.log (appended per run).

param(
    [string]$Repo = "C:\dev\PhotonScript",
    [string]$TaskName = "PhotonScript AutoIntegrate",
    [int]$IntervalMin = 30,
    [switch]$DryRun,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'."
    } else {
        Write-Host "No scheduled task named '$TaskName'."
    }
    exit 0
}

$exe = Join-Path $Repo ".venv\Scripts\photonscript.exe"
if (-not (Test-Path $exe)) {
    Write-Host "photonscript not found: $exe (install the repo venv first)" -ForegroundColor Red
    exit 1
}
if ($IntervalMin -lt 5) {
    Write-Host "IntervalMin must be at least 5." -ForegroundColor Red
    exit 1
}

$logDir = Join-Path $Repo "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "autointegrate.log"
$cliArgs = "autointegrate --once" + $(if ($DryRun) { " --dry-run" } else { "" })
# cmd /c so the output (and errors) land in the log file
$cmdExe = Join-Path $env:SystemRoot "System32\cmd.exe"
$action = New-ScheduledTaskAction -Execute $cmdExe `
    -Argument "/c `"`"$exe`" $cliArgs >> `"$log`" 2>&1`"" -WorkingDirectory $Repo

$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMin)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive
# One run at a time; a long PixInsight run is never cut off by the next tick.
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 12) -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null
Write-Host "Registered '$TaskName': every $IntervalMin min, '$exe $cliArgs', log $log"
Write-Host "Check it once by hand first: $exe autointegrate --once --dry-run"
