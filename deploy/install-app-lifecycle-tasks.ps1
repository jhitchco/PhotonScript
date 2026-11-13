# PS-170: register (or remove) the two scheduled tasks that close and
# relaunch NINA #1, NINA #2 and PHD2 every day on the SCOPE PC.
#
#   "PhotonScript Apps Stop"   daily at -StopAt (06:30), then every 15 min
#                              for -StopWindowHours (4.5 h, until 11:00):
#                              observatory-apps.ps1 -Stop -Scheduled. The
#                              script acts once a day, only when the service
#                              says it is safe (dawn shutdown + its cooler
#                              verify done, app_lifecycle_stop_after_shutdown_min
#                              passed, armer idle, no NINA sequence running,
#                              sun up), so the window follows the season.
#   "PhotonScript Apps Start"  daily at -StartAt (11:45, before the 12:00
#                              noon re-arm): observatory-apps.ps1 -Start
#                              -Scheduled. Launches only what is missing,
#                              then connect + preflight.
#
# Both run as jeremy, logon type Interactive ("run only when user is logged
# on"), so NINA and PHD2 open on his desktop (a session-0 launch would be
# invisible and unusable). With auto-logon (PS-87) that is always true.
# Both do nothing while app_lifecycle_enabled is off.
#
# Run once from an elevated PowerShell on the scope PC:
#   powershell -ExecutionPolicy Bypass -File C:\astro\PhotonScript\deploy\install-app-lifecycle-tasks.ps1 -DryRun
#   powershell -ExecutionPolicy Bypass -File C:\astro\PhotonScript\deploy\install-app-lifecycle-tasks.ps1
#   ... -Uninstall      remove both tasks
# Keep -StartAt equal to app_lifecycle_start_local (System page).

param(
    [string]$User = "jeremy",
    [string]$Repo = "C:\astro\PhotonScript",
    [string]$StopAt = "06:30",
    [double]$StopWindowHours = 4.5,
    [int]$StopEveryMin = 15,
    [string]$StartAt = "11:45",
    [string]$StopTask = "PhotonScript Apps Stop",
    [string]$StartTask = "PhotonScript Apps Start",
    [switch]$DryRun,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin -and -not $DryRun) {
    Write-Host "Run this from an elevated (Administrator) PowerShell (or add -DryRun)." -ForegroundColor Red
    exit 1
}

if ($Uninstall) {
    foreach ($name in @($StopTask, $StartTask)) {
        if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
            if ($DryRun) { Write-Host "would remove '$name'"; continue }
            Unregister-ScheduledTask -TaskName $name -Confirm:$false
            Write-Host "Removed scheduled task '$name'."
        } else {
            Write-Host "No scheduled task named '$name'."
        }
    }
    exit 0
}

$script = Join-Path $Repo "deploy\observatory-apps.ps1"
if (-not (Test-Path $script)) {
    Write-Host "Script not found: $script" -ForegroundColor Red
    exit 1
}

$account = "$env:COMPUTERNAME\$User"
$psExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
function New-AppAction([string]$modeArg) {
    New-ScheduledTaskAction -Execute $psExe `
        -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`" $modeArg -Scheduled" `
        -WorkingDirectory $Repo
}

# Stop: daily, repeating inside the morning window (the script's guards
# decide the moment; once it has closed the apps it leaves them alone).
$stopTrigger = New-ScheduledTaskTrigger -Daily -At $StopAt
$rep = New-ScheduledTaskTrigger -Once -At $StopAt `
    -RepetitionInterval (New-TimeSpan -Minutes $StopEveryMin) `
    -RepetitionDuration (New-TimeSpan -Minutes ([int]($StopWindowHours * 60)))
$stopTrigger.Repetition = $rep.Repetition
$startTrigger = New-ScheduledTaskTrigger -Daily -At $StartAt

$principal = New-ScheduledTaskPrincipal -UserId $account -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 45) `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

$plan = @(
    @{ Name = $StopTask; Trigger = $stopTrigger; Action = (New-AppAction "-Stop")
       Desc = "Close NINA #1 / #2 + PHD2 after the dawn shutdown (deploy\observatory-apps.ps1 -Stop -Scheduled). Installed by deploy\install-app-lifecycle-tasks.ps1 (PS-170)."
       When = "daily $StopAt, every $StopEveryMin min for $StopWindowHours h" },
    @{ Name = $StartTask; Trigger = $startTrigger; Action = (New-AppAction "-Start")
       Desc = "Launch NINA #1 / #2 + PHD2, connect, preflight (deploy\observatory-apps.ps1 -Start -Scheduled). Installed by deploy\install-app-lifecycle-tasks.ps1 (PS-170)."
       When = "daily $StartAt" }
)

foreach ($t in $plan) {
    if ($DryRun) {
        Write-Host ("would register '{0}': {1}, as {2} (Interactive), runs {3} {4}" -f `
            $t.Name, $t.When, $account, $t.Action.Execute, $t.Action.Arguments)
        continue
    }
    Register-ScheduledTask -TaskName $t.Name -Action $t.Action -Trigger $t.Trigger `
        -Settings $settings -Principal $principal -Description $t.Desc -Force | Out-Null
    Write-Host ("Registered '{0}' ({1}, as {2}, Interactive)." -f $t.Name, $t.When, $account) -ForegroundColor Green
}

Write-Host ""
Write-Host "Nothing acts until app_lifecycle_enabled is set (System page, App lifecycle)."
Write-Host "Watched first run:  ...\deploy\observatory-apps.ps1 -Status, then -Stop -DryRun / -Stop, then -Start"
Write-Host "Check:   Get-ScheduledTask 'PhotonScript Apps*' | Get-ScheduledTaskInfo"
Write-Host "Log:     <data_dir>\logs\observatory-apps.log"
