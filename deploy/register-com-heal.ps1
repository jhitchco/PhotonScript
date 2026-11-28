# PS-256: register (or remove) the "PhotonScript COM heal" scheduled task.
#
# Why: after the 11-13 Windows Update reboot the mount's serial port (COM3,
# Paramount MX via usbser) came up in Device Manager with problem code 10 and
# TheSky's mount connect failed with Error 201. The fix was Device Manager:
# disable COM3, enable COM3. The PhotonScript service runs WITHOUT admin, so
# it cannot restart a device. This script, run ONCE by Jeremy as admin,
# registers a task that can:
#
#   "PhotonScript COM heal"  runs as SYSTEM, highest privileges, no trigger
#                            (on demand only). Its ONLY action is
#                            pnputil /restart-device "<instance id>"
#                            with the instance id of the device found below
#                            baked in. It takes no parameters, so it cannot
#                            be pointed at anything else.
#
# It also grants the PhotonScript user (-User, default jeremy) read + run on
# that one task, so the service can start it with
#   schtasks /run /tn "PhotonScript COM heal"
# without admin. PhotonScript never runs this script, and never registers,
# changes or deletes the task (photonscript/scheduler/com_heal.py only starts
# it, gated: never while a night runs, at most com_heal_max_per_day a day).
#
# Run once from an elevated PowerShell on the scope PC:
#   powershell -ExecutionPolicy Bypass -File C:\astro\PhotonScript\deploy\register-com-heal.ps1 -DryRun
#   powershell -ExecutionPolicy Bypass -File C:\astro\PhotonScript\deploy\register-com-heal.ps1
#   ... -Port COM4             another port (default COM3, = PS_COM_HEAL_PORT)
#   ... -InstanceId "USB\..."  pick the device when more than one matches
#   ... -Yes                   no confirmation prompt
#   ... -Unregister            remove the task
#
# Re-run it if the mount moves to another USB port (the instance id changes).
# Check afterwards: GET http://<scope PC>:8100/api/com-heal ("registered": true).

param(
    [string]$Port = "COM3",
    [string]$User = "jeremy",
    [string]$TaskName = "PhotonScript COM heal",
    [string]$InstanceId = "",
    [switch]$Yes,
    [switch]$DryRun,
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"

$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin -and -not $DryRun) {
    Write-Host "Run this from an elevated (Administrator) PowerShell (or add -DryRun)." -ForegroundColor Red
    exit 1
}

if ($Unregister) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        if ($DryRun) { Write-Host "would remove '$TaskName'"; exit 0 }
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'." -ForegroundColor Green
    } else {
        Write-Host "No scheduled task named '$TaskName'."
    }
    exit 0
}

# --- find the mount's serial device ------------------------------------------
$pattern = "\(" + [regex]::Escape($Port) + "\)"
if ($InstanceId) {
    $dev = Get-PnpDevice -InstanceId $InstanceId -ErrorAction SilentlyContinue
    if (-not $dev) {
        Write-Host "No device with instance id '$InstanceId'." -ForegroundColor Red
        exit 1
    }
} else {
    $found = @(Get-PnpDevice -Class Ports -PresentOnly -ErrorAction SilentlyContinue |
               Where-Object { $_.FriendlyName -match $pattern })
    if ($found.Count -eq 0) {
        Write-Host "No present Ports device named '...($Port)'. All Ports devices:" -ForegroundColor Red
        Get-PnpDevice -Class Ports -ErrorAction SilentlyContinue |
            Format-Table -AutoSize Status, FriendlyName, InstanceId | Out-String | Write-Host
        Write-Host "Plug the mount in (or pass -InstanceId) and run again."
        exit 1
    }
    if ($found.Count -gt 1) {
        Write-Host "More than one device matches $Port; run again with -InstanceId:" -ForegroundColor Yellow
        $found | Format-Table -AutoSize Status, FriendlyName, InstanceId | Out-String | Write-Host
        exit 1
    }
    $dev = $found[0]
}
$id = $dev.InstanceId
$code = (Get-CimInstance Win32_PnPEntity -Filter ("DeviceID='" + $id.Replace("\", "\\") + "'") -ErrorAction SilentlyContinue).ConfigManagerErrorCode

Write-Host "Found the mount's serial device:"
Write-Host ("  Name:        {0}" -f $dev.FriendlyName)
Write-Host ("  Class:       {0}" -f $dev.Class)
Write-Host ("  Status:      {0} (problem code {1})" -f $dev.Status, $code)
Write-Host ("  Instance id: {0}" -f $id)

$pnputil = Join-Path $env:SystemRoot "System32\pnputil.exe"
$help = (& $pnputil /? 2>&1 | Out-String)
if ($help -notmatch "restart-device") {
    Write-Host "This Windows build's pnputil has no /restart-device (needs Windows 10 2004 or later)." -ForegroundColor Red
    exit 1
}

$account = "$env:COMPUTERNAME\$User"
try {
    $sid = (New-Object System.Security.Principal.NTAccount($account)).Translate([System.Security.Principal.SecurityIdentifier]).Value
} catch {
    Write-Host "Cannot resolve user '$account' (the account the PhotonScript service runs as)." -ForegroundColor Red
    exit 1
}

$arg = "/restart-device `"$id`""
Write-Host ""
Write-Host "Task '$TaskName': runs as SYSTEM (highest), on demand only, action:"
Write-Host "  $pnputil $arg"
Write-Host "and $account (SID $sid) may read and run it (schtasks /run, no admin)."
if ($DryRun) { Write-Host "Dry run: nothing registered."; exit 0 }

if (-not $Yes) {
    $ans = Read-Host "Register it for this device? [y/N]"
    if ($ans -notmatch "^(y|yes)$") { Write-Host "Nothing changed."; exit 0 }
}

# --- register ----------------------------------------------------------------
$action = New-ScheduledTaskAction -Execute $pnputil -Argument $arg
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 2) `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$desc = "PS-256: restart the mount serial port ($($dev.FriendlyName), $id) like Device Manager disable / enable. Started by PhotonScript (com_heal.py) when it has a problem code. Installed by deploy\register-com-heal.ps1."
Register-ScheduledTask -TaskName $TaskName -Action $action -Principal $principal `
    -Settings $settings -Description $desc -Force | Out-Null

# Let the (non-admin) PhotonScript user read and run this one task.
$svc = New-Object -ComObject Schedule.Service
$svc.Connect()
$task = $svc.GetFolder("\").GetTask($TaskName)
$sd = $task.GetSecurityDescriptor(4)          # DACL only
$ace = "(A;;GRGX;;;$sid)"
if ($sd -notlike "*$ace*") { $task.SetSecurityDescriptor($sd + $ace, 0) }

Write-Host "Registered scheduled task '$TaskName' (SYSTEM, highest; $User may run it)." -ForegroundColor Green
Write-Host ""
Write-Host "Check as $User (not elevated): schtasks /query /tn `"$TaskName`""
Write-Host "PhotonScript view:             GET /api/com-heal (registered: true)"
Write-Host "Re-run this script if the mount moves to another USB port."
