# Register (or remove) the "PhotonScript" scheduled task on the SCOPE PC so
# PhotonScript starts by itself and keeps running. (PS-44, PS-55)
#
# Run once from an elevated PowerShell on the scope PC, during the day with no
# session running:
#   powershell -ExecutionPolicy Bypass -File C:\astro\PhotonScript\deploy\install-autostart.ps1
#   ... -StartNow              also start it now (stop any console copy first)
#   ... -LogonType S4U         at boot with nobody logged on (see below)
#   ... -LogonType Password    like S4U but with jeremy's stored password
#   ... -Uninstall             remove the task
#
# Logon type (PS-55):
#   Interactive (default): starts when jeremy logs on, in his desktop session,
#     hidden window. This is the same environment as the console wrapper,
#     which ran healthy all night on 2026-09-26. NINA and PHD2 need jeremy
#     logged on anyway, so with Windows auto-logon it also comes up after a
#     reboot. Sign-out (not disconnect) of that session stops it.
#   S4U: "run whether user is logged on or not", session 0, no desktop, no
#     stored password. On 2026-09-26 the API got slower and slower under S4U
#     (6 s, then 45 s, then no answer) while the same code in a console was
#     fine; cause not yet proven (PS-55). Use it only for a supervised test.
#   Password: S4U with a stored password, if S4U fails with a logon error.
#
# Why a scheduled task and not a Windows service:
#   - Built in, no third-party service wrapper (NSSM/WinSW) to install.
#   - Runs as jeremy, so Path.home(), data_dir (C:\Users\jeremy\.photonscript),
#     NINA's image folders and the .env all resolve exactly as they do today.
#   - The restart logic lives in `photonscript supervise` (backoff, crash-loop
#     cap, Pushover), which is unit tested; the task only has to start it.

param(
    [string]$User = "jeremy",
    [string]$Repo = "C:\astro\PhotonScript",
    [string]$TaskName = "PhotonScript",
    [ValidateSet("Interactive", "S4U", "Password")]
    [string]$LogonType = "Interactive",
    [switch]$StartNow,
    [switch]$UsePassword,   # old spelling of -LogonType Password
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
if ($UsePassword) { $LogonType = "Password" }

$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "Run this from an elevated (Administrator) PowerShell." -ForegroundColor Red
    exit 1
}

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'."
    } else {
        Write-Host "No scheduled task named '$TaskName'."
    }
    exit 0
}

$wrapper = Join-Path $Repo "deploy\run-photonscript.ps1"
if (-not (Test-Path $wrapper)) {
    Write-Host "Wrapper not found: $wrapper" -ForegroundColor Red
    exit 1
}

$account = "$env:COMPUTERNAME\$User"
$launcher = "task-$LogonType"
$psExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$action = New-ScheduledTaskAction -Execute $psExe `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$wrapper`" -Launcher $launcher" `
    -WorkingDirectory $Repo

if ($LogonType -eq "Interactive") {
    # When jeremy logs on (auto-logon makes that "at boot"), 30 s later.
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $account
    $trigger.Delay = "PT30S"
} else {
    # At boot, one minute after startup so the network and Tailscale are up.
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $trigger.Delay = "PT1M"
}

# No run-time limit (default is 72 h), normal CPU priority (task default is
# below normal), one instance only, and if the wrapper itself dies the task
# is restarted up to 3 times, 5 min apart.
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -Priority 4 `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5) `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

$desc = "PhotonScript service (deploy\run-photonscript.ps1 -> photonscript supervise), logon type $LogonType. Installed by deploy\install-autostart.ps1 (PS-44, PS-55)."

if ($LogonType -eq "Password") {
    $cred = Get-Credential -UserName $account -Message "Password for $account (stored by Task Scheduler)"
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -User $account -Password $cred.GetNetworkCredential().Password `
        -RunLevel Limited -Description $desc -Force | Out-Null
} else {
    $principal = New-ScheduledTaskPrincipal -UserId $account -LogonType $LogonType -RunLevel Limited
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal -Description $desc -Force | Out-Null
}

Write-Host "Registered scheduled task '$TaskName' (runs as $account, logon type $LogonType)." -ForegroundColor Green

if ($LogonType -eq "Interactive") {
    $wl = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon" -ErrorAction SilentlyContinue
    if ($wl.AutoAdminLogon -ne "1" -or $wl.DefaultUserName -ne $User) {
        Write-Host "Note: Windows auto-logon for '$User' is not set, so after a reboot PhotonScript (and NINA/PHD2) wait until someone logs on." -ForegroundColor Yellow
    }
}

if ($StartNow) {
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 5
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Host ("State: {0}  Last result: 0x{1:X}" -f (Get-ScheduledTask -TaskName $TaskName).State, $info.LastTaskResult)
}

Write-Host ""
Write-Host "Verify:  photonscript autostart-check   (normal PowerShell, after 60 s)"
Write-Host "Check:   Get-ScheduledTask $TaskName | Get-ScheduledTaskInfo"
Write-Host "Status:  photonscript status"
Write-Host "Logs:    photonscript monitor"
