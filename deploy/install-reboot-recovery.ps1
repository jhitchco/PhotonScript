# PS-87: register (or remove) the two logon tasks that finish recovery after
# a reboot of the SCOPE PC. Needs Windows auto-logon for jeremy (set by hand
# with Sysinternals Autologon, docs/MAINTENANCE.md "Recovery after a
# reboot"); this script never asks for, reads or stores a password.
#
#   "PhotonScript Reboot Recovery"  at jeremy's logon, after -RecoveryDelay
#                                   (1 min): deploy\reboot-recovery.ps1
#                                   (TheSky, then NINA #1, NINA #2, PHD2 via
#                                   observatory-apps.ps1 -Start). Acts only
#                                   within 30 min of a boot.
#   "PhotonScript Lock After Logon" at jeremy's logon, after -LockDelay
#                                   (2 min): lock the workstation. Apps keep
#                                   running while the session is locked.
#
# The PhotonScript service has its own task ("PhotonScript",
# install-autostart.ps1, 30 s after logon). Both tasks here run as jeremy,
# logon type Interactive (GUI apps on his desktop, never session 0).
#
# Run once from an elevated PowerShell on the scope PC:
#   powershell -ExecutionPolicy Bypass -File C:\astro\PhotonScript\deploy\install-reboot-recovery.ps1 -DryRun
#   powershell -ExecutionPolicy Bypass -File C:\astro\PhotonScript\deploy\install-reboot-recovery.ps1
#   ... -NoLock       only the recovery task
#   ... -Uninstall    remove both tasks

param(
    [string]$User = "jeremy",
    [string]$Repo = "C:\astro\PhotonScript",
    [string]$RecoveryTask = "PhotonScript Reboot Recovery",
    [string]$LockTask = "PhotonScript Lock After Logon",
    [string]$RecoveryDelay = "PT1M",
    [string]$LockDelay = "PT2M",
    [switch]$NoLock,
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
    foreach ($name in @($RecoveryTask, $LockTask)) {
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

$script = Join-Path $Repo "deploy\reboot-recovery.ps1"
if (-not (Test-Path $script)) {
    Write-Host "Script not found: $script" -ForegroundColor Red
    exit 1
}

$account = "$env:COMPUTERNAME\$User"
$psExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$principal = New-ScheduledTaskPrincipal -UserId $account -LogonType Interactive -RunLevel Limited

# Recovery: one instance, up to 30 min (the waits inside are about 10 min).
$recAction = New-ScheduledTaskAction -Execute $psExe `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`"" `
    -WorkingDirectory $Repo
$recTrigger = New-ScheduledTaskTrigger -AtLogOn -User $account
$recTrigger.Delay = $RecoveryDelay
$recSettings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -Priority 4 `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$recDesc = "After a reboot: TheSky, then NINA #1, NINA #2, PHD2 (deploy\reboot-recovery.ps1). Installed by deploy\install-reboot-recovery.ps1 (PS-87)."

# Lock: rundll32 user32.dll,LockWorkStation in jeremy's session.
$lockAction = New-ScheduledTaskAction -Execute "rundll32.exe" -Argument "user32.dll,LockWorkStation"
$lockTrigger = New-ScheduledTaskTrigger -AtLogOn -User $account
$lockTrigger.Delay = $LockDelay
$lockSettings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$lockDesc = "Locks the screen $LockDelay after jeremy's (auto-)logon; apps keep running. Installed by deploy\install-reboot-recovery.ps1 (PS-87)."

$plan = New-Object System.Collections.ArrayList
[void]$plan.Add(@($RecoveryTask, $recAction, $recTrigger, $recSettings, $recDesc))
if (-not $NoLock) { [void]$plan.Add(@($LockTask, $lockAction, $lockTrigger, $lockSettings, $lockDesc)) }

foreach ($t in $plan) {
    $name, $action, $trigger, $settings, $desc = $t
    if ($DryRun) {
        Write-Host ("would register '{0}': at logon of {1} + {2}, Interactive, runs {3} {4}" -f $name, $account, $trigger.Delay, $action.Execute, $action.Arguments)
        continue
    }
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal -Description $desc -Force | Out-Null
    Write-Host "Registered scheduled task '$name' (runs as $account, logon type Interactive)." -ForegroundColor Green
}

$wl = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon" -ErrorAction SilentlyContinue
if ($wl.AutoAdminLogon -ne "1" -or $wl.DefaultUserName -ne $User) {
    Write-Host "Note: Windows auto-logon for '$User' is not set, so these tasks run only when someone logs on. Set it with Sysinternals Autologon (docs/MAINTENANCE.md)." -ForegroundColor Yellow
}
if (-not (Get-ScheduledTask -TaskName "PhotonScript" -ErrorAction SilentlyContinue)) {
    Write-Host "Note: the 'PhotonScript' service task is not registered: run deploy\install-autostart.ps1 too." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Test by hand: powershell -ExecutionPolicy Bypass -File $script -DryRun -Force"
Write-Host "Verify:       photonscript autostart-check"
Write-Host "Log:          <data_dir>\logs\reboot-recovery.log"
