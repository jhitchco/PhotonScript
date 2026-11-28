# One PixInsight job at a time: the PixInsight queue lock shared with
# photonscript integrate / blend / finish / integrate-watch / autointegrate
# (photonscript/integration/pilock.py). Dot-source it from a launcher:
#   . (Join-Path $PSScriptRoot "pixinsight-lock.ps1")
#   Enter-PixInsightLock -Command "run-integration $Target" -WaitMin 60
#   $proc = Start-Process ... -PassThru
#   Set-PixInsightLockOwner -Process $proc -Command "run-integration $Target"
# Enter waits up to -WaitMin minutes while another live job holds the lock,
# then stops the script (exit 3) naming that job. The lock is first written
# with this shell's pid and "starting": true (stale after 2 min); Set-Owner
# rewrites it with the PixInsight pid, so the lock ends when that PixInsight
# is closed. Remove-PixInsightLock drops it (a -Wait launcher after the exit,
# or a launch that failed). Lock file: %LOCALAPPDATA%\PhotonScript\pixinsight.lock
# (env PS_PIXINSIGHT_LOCK overrides). Pure ASCII, Windows PowerShell 5.1.

$script:PiLockStartingStaleS = 120
$script:PiLockUnreadableStaleS = 30

function Get-PixInsightLockPath {
    if ($env:PS_PIXINSIGHT_LOCK) { return $env:PS_PIXINSIGHT_LOCK }
    return (Join-Path $env:LOCALAPPDATA "PhotonScript\pixinsight.lock")
}

function ConvertTo-PiLockEpoch([datetime]$t) {
    return [math]::Round(([DateTimeOffset]$t.ToUniversalTime()).ToUnixTimeMilliseconds() / 1000.0, 3)
}

function New-PiLockJson([int]$ProcessId, [string]$Command, [bool]$Starting) {
    $created = $null
    try { $created = ConvertTo-PiLockEpoch (Get-Process -Id $ProcessId -ErrorAction Stop).StartTime } catch {}
    $o = [ordered]@{
        pid = $ProcessId; pid_created = $created; command = $Command
        started = (Get-Date).ToString("yyyy-MM-ddTHH:mm:ss")
        started_epoch = ConvertTo-PiLockEpoch (Get-Date); host = $env:COMPUTERNAME
    }
    if ($Starting) { $o["starting"] = $true }
    return ($o | ConvertTo-Json -Compress)
}

function Read-PixInsightLock {
    $p = Get-PixInsightLockPath
    if (-not (Test-Path -LiteralPath $p)) { return $null }
    try {
        $txt = [System.IO.File]::ReadAllText($p)
        $o = $txt | ConvertFrom-Json
        if ($null -eq $o.pid) { throw "no pid" }
        return $o
    } catch {
        $age = 0
        try { $age = ((Get-Date) - (Get-Item -LiteralPath $p).LastWriteTime).TotalSeconds } catch {}
        return [pscustomobject]@{ unreadable = $true; age_s = $age }
    }
}

# '' = the lock is held; else why it is stale (same rules as pilock.stale_reason)
function Get-PixInsightLockStale($info) {
    if ($info.unreadable) {
        if ($info.age_s -gt $script:PiLockUnreadableStaleS) { return "unreadable lock file" }
        return ""
    }
    $lockPid = [int]$info.pid
    if ($lockPid -le 0) { return "no pid in the lock" }
    $proc = Get-Process -Id $lockPid -ErrorAction SilentlyContinue
    if (-not $proc) { return "pid $lockPid is not running" }
    if ($info.pid_created) {
        try {
            $got = ConvertTo-PiLockEpoch $proc.StartTime
            if ([math]::Abs($got - [double]$info.pid_created) -gt 2) { return "pid $lockPid is now another process" }
        } catch {}
    }
    if ($info.starting) {
        $age = (ConvertTo-PiLockEpoch (Get-Date)) - [double]$info.started_epoch
        if ($age -gt $script:PiLockStartingStaleS) { return "launcher pid $lockPid never started PixInsight" }
    }
    return ""
}

function Format-PixInsightLock($info) {
    if ($info.unreadable) { return "an unreadable lock file" }
    return "'" + $info.command + "' (pid " + $info.pid + ", since " + $info.started + ")"
}

function Enter-PixInsightLock {
    param([Parameter(Mandatory=$true)][string]$Command, [double]$WaitMin = 60, [int]$PollS = 15)
    $p = Get-PixInsightLockPath
    $dir = Split-Path $p -Parent
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    $deadline = (Get-Date).AddMinutes([math]::Max(0, $WaitMin))
    $said = $false
    while ($true) {
        try {
            $fs = [System.IO.File]::Open($p, [System.IO.FileMode]::CreateNew,
                                         [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
            try {
                $bytes = [System.Text.Encoding]::ASCII.GetBytes((New-PiLockJson $PID $Command $true))
                $fs.Write($bytes, 0, $bytes.Length)
            } finally { $fs.Close() }
            return
        } catch [System.IO.IOException] {
            # exists: held, or stale
        }
        $cur = Read-PixInsightLock
        if ($null -eq $cur) { continue }
        $why = Get-PixInsightLockStale $cur
        if ($why) {
            Write-Host "PixInsight queue lock: taking over a stale lock ($why; was $(Format-PixInsightLock $cur))"
            Remove-Item -LiteralPath $p -Force -ErrorAction SilentlyContinue
            if (-not (Test-Path -LiteralPath $p)) { continue }
        }
        if ((Get-Date) -ge $deadline) {
            Write-Error ("PixInsight queue busy: $(Format-PixInsightLock $cur) holds $p. " +
                         "One PixInsight job runs at a time: run this again when it ends (-LockWaitMin waits longer).")
            exit 3
        }
        if (-not $said) {
            Write-Host "PixInsight queue busy: $(Format-PixInsightLock $cur); waiting up to $WaitMin min (Ctrl+C stops)"
            $said = $true
        }
        Start-Sleep -Seconds $PollS
    }
}

function Set-PixInsightLockOwner {
    param([Parameter(Mandatory=$true)]$Process, [Parameter(Mandatory=$true)][string]$Command)
    $p = Get-PixInsightLockPath
    [System.IO.File]::WriteAllText($p, (New-PiLockJson $Process.Id $Command $false))
}

function Remove-PixInsightLock {
    param([int]$OwnerPid = 0)
    $p = Get-PixInsightLockPath
    $cur = Read-PixInsightLock
    if ($null -eq $cur) { return }
    $mine = ($cur.pid -eq $PID) -or ($OwnerPid -gt 0 -and $cur.pid -eq $OwnerPid)
    if ($mine) { Remove-Item -LiteralPath $p -Force -ErrorAction SilentlyContinue }
}
