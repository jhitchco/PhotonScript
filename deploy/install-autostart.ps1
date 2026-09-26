# Register (or remove) the "PhotonScript" scheduled task on the SCOPE PC so
# PhotonScript starts at boot with nobody logged on and keeps running. (PS-44)
#
# Run once from an elevated PowerShell on the scope PC, during the day with no
# session running:
#   powershell -ExecutionPolicy Bypass -File C:\astro\PhotonScript\deploy\install-autostart.ps1
#   ... -StartNow          also start it now (stop any console copy first)
#   ... -UsePassword       store jeremy's password instead of S4U (see below)
#   ... -Uninstall         remove the task
#
# Why a scheduled task and not a Windows service:
#   - Built in, no third-party service wrapper (NSSM/WinSW) to install.
#   - Runs as jeremy, so Path.home(), data_dir (C:\Users\jeremy\.photonscript),
#     NINA's image folders and the .env all resolve exactly as they do today.
#   - The restart logic lives in `photonscript supervise` (backoff, crash-loop
#     cap, Pushover), which is unit tested; the task only has to start it.
#   PhotonScript talks to NINA, PHD2 and Syncthing over localhost HTTP and
#   launches no GUI programs, so running without a desktop session is fine.
#   NINA and PHD2 themselves still need jeremy logged on (or auto-logon).
#
# Logon type: S4U ("run whether user is logged on or not", no stored
# password). It works because the GitHub repo is public (git pull needs no
# credentials) and nothing reads network shares. If the task fails to start
# with a logon error, re-run with -UsePassword.

param(
    [string]$User = "jeremy",
    [string]$Repo = "C:\astro\PhotonScript",
    [string]$TaskName = "PhotonScript",
    [switch]$StartNow,
    [switch]$UsePassword,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

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

$psExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$action = New-ScheduledTaskAction -Execute $psExe `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$wrapper`"" `
    -WorkingDirectory $Repo

# At boot, one minute after startup so the network and Tailscale are up.
$trigger = New-ScheduledTaskTrigger -AtStartup
$trigger.Delay = "PT1M"

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

$account = "$env:COMPUTERNAME\$User"
$desc = "PhotonScript service (deploy\run-photonscript.ps1 -> photonscript supervise). Installed by deploy\install-autostart.ps1 (PS-44)."

if ($UsePassword) {
    $cred = Get-Credential -UserName $account -Message "Password for $account (stored by Task Scheduler)"
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -User $account -Password $cred.GetNetworkCredential().Password `
        -RunLevel Limited -Description $desc -Force | Out-Null
} else {
    $principal = New-ScheduledTaskPrincipal -UserId $account -LogonType S4U -RunLevel Limited
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal -Description $desc -Force | Out-Null
}

Write-Host "Registered scheduled task '$TaskName' (runs as $account at startup)." -ForegroundColor Green

if ($StartNow) {
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 5
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Host ("State: {0}  Last result: 0x{1:X}" -f (Get-ScheduledTask -TaskName $TaskName).State, $info.LastTaskResult)
}

Write-Host ""
Write-Host "Check:   Get-ScheduledTask $TaskName | Get-ScheduledTaskInfo"
Write-Host "Status:  photonscript status"
Write-Host "Logs:    photonscript monitor"
