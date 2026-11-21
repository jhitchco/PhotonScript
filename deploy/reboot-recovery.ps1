# PS-87: bring the observatory stack back after a reboot of the SCOPE PC.
#
# Run at jeremy's logon by the "PhotonScript Reboot Recovery" task
# (deploy\install-reboot-recovery.ps1), which with Windows auto-logon means
# "after every boot". The PhotonScript service itself has its own task
# ("PhotonScript", install-autostart.ps1, 30 s after logon); this script
# starts everything around it, in order:
#
#   1. Wait for the PhotonScript API (GET /api/health on 127.0.0.1:8100),
#      up to -ServiceWaitS. Not fatal: step 2 runs anyway, step 3 needs it
#      for the launch settings and the connect + preflight.
#   2. TheSky: launched when no TheSky64 process runs, then wait for its
#      TCP server (:3040) and for the mount to report connected (read only,
#      IsConnected; this script never connects, parks or slews the mount).
#      TheSky's own setting connects the mount at launch (see
#      docs/MAINTENANCE.md "Recovery after a reboot"); if it does not, the
#      log says so and step 3 refuses with one page.
#   3. NINA #1, NINA #2 and PHD2: observatory-apps.ps1 -Start (PS-170):
#      only what does not answer is launched, on the right profiles, then
#      equipment connect + preflight through the service.
#
# Only right after a boot: when the PC has been up longer than
# -MaxUptimeMin (a later manual logon, an RDP logon after a sign-out) it
# logs and exits, unless -Force. Locking the screen is a separate task
# ("PhotonScript Lock After Logon"); apps keep running while locked.
#
# By hand, normal PowerShell:
#   powershell -ExecutionPolicy Bypass -File C:\astro\PhotonScript\deploy\reboot-recovery.ps1 -DryRun -Force
#   ... -Force          run now whatever the uptime
#
# Log: <data_dir>\logs\reboot-recovery.log. Exit 0 = done or nothing to do,
# 1 = a step failed, 2 = skipped (uptime).
#
# No credentials: this script never reads, stores or passes a password.

param(
    [switch]$DryRun,
    [switch]$Force,
    [string]$Repo = "C:\astro\PhotonScript",
    [string]$Api = "http://127.0.0.1:8100",
    # Empty = the first TheSky64.exe under Program Files\Software Bisque
    [string]$TheSkyExe = "",
    [int]$TheSkyPort = 3040,
    [int]$MaxUptimeMin = 30,
    [int]$ServiceWaitS = 300,
    [int]$TheSkyWaitS = 180,
    [int]$MountWaitS = 120
)

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------- logging
$dataDir = Join-Path $env:USERPROFILE ".photonscript"
$envFile = Join-Path $Repo ".env"
if (Test-Path $envFile) {
    $m = Select-String -Path $envFile -Pattern '^\s*PS_DATA_DIR\s*=\s*(.+?)\s*$' | Select-Object -First 1
    if ($m) { $dataDir = $m.Matches[0].Groups[1].Value.Trim('"', "'") }
}
$logDir = Join-Path $dataDir "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "reboot-recovery.log"
if ((Test-Path $log) -and ((Get-Item $log).Length -gt 1MB)) {
    Move-Item -Force $log "$log.1"
}

function Write-Log([string]$msg) {
    $line = "{0} {1}{2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $(if ($DryRun) { "[dry run] " } else { "" }), $msg
    Write-Host $line
    Add-Content -Path $log -Value $line
}

function Test-Port([string]$h, [int]$p, [int]$ms = 1500) {
    $c = New-Object System.Net.Sockets.TcpClient
    try {
        $iar = $c.BeginConnect($h, $p, $null, $null)
        if (-not $iar.AsyncWaitHandle.WaitOne($ms)) { return $false }
        $c.EndConnect($iar)
        return $true
    } catch { return $false } finally { $c.Close() }
}

function Get-TheSkyMount {
    # Same framing as observatory-apps.ps1: reads IsConnected only.
    $js = "/* Java Script */`n/* Socket Start Packet */`n" +
          "var Out; var c = false; try { c = sky6RASCOMTele.IsConnected; } catch (e) {} " +
          "Out = c ? 'connected=1' : 'connected=0';" +
          "`n/* Socket End Packet */`n"
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $iar = $client.BeginConnect("localhost", $TheSkyPort, $null, $null)
        if (-not $iar.AsyncWaitHandle.WaitOne(3000)) { return "no-answer" }
        $client.EndConnect($iar)
        $stream = $client.GetStream()
        $stream.ReadTimeout = 10000
        $bytes = [System.Text.Encoding]::ASCII.GetBytes($js)
        $stream.Write($bytes, 0, $bytes.Length)
        $buf = New-Object byte[] 4096
        $text = ""
        while ($text -notmatch '\|') {
            $n = $stream.Read($buf, 0, $buf.Length)
            if ($n -le 0) { break }
            $text += [System.Text.Encoding]::ASCII.GetString($buf, 0, $n)
        }
        if ($text -match 'connected=1') { return "connected" }
        if ($text -match 'connected=0') { return "disconnected" }
        return "unknown"
    } catch { return "no-answer" } finally { $client.Close() }
}

# ---------------------------------------------------------------- 0. only after a boot
$boot = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime
$upMin = [int]((Get-Date) - $boot).TotalMinutes
Write-Log ("Reboot recovery: booted {0:yyyy-MM-dd HH:mm}, up {1} min, user {2}, session {3}" -f $boot, $upMin, $env:USERNAME, (Get-Process -Id $PID).SessionId)
if ($upMin -gt $MaxUptimeMin -and -not $Force) {
    Write-Log "Up longer than $MaxUptimeMin min: not a fresh boot, nothing to do (-Force to run anyway)."
    exit 2
}
$failed = $false

# ---------------------------------------------------------------- 1. PhotonScript service
$deadline = (Get-Date).AddSeconds($ServiceWaitS)
$health = $null
do {
    try { $health = Invoke-RestMethod -Uri "$Api/api/health" -TimeoutSec 10 } catch { $health = $null }
    if ($health) { break }
    if ($DryRun) { break }
    Start-Sleep -Seconds 10
} while ((Get-Date) -lt $deadline)
if ($health) {
    Write-Log ("PhotonScript up: {0}, launcher {1}, session {2}, armer {3}" -f $health.version, $health.process.launcher, $health.process.session_id, $health.armer)
} else {
    Write-Log "PhotonScript API did not answer within $ServiceWaitS s (check the 'PhotonScript' task: photonscript autostart-check)."
    $failed = $true
}

# ---------------------------------------------------------------- 2. TheSky
if (-not $TheSkyExe) {
    $hit = Get-ChildItem -Path "$env:ProgramFiles\Software Bisque", "${env:ProgramFiles(x86)}\Software Bisque" `
        -Filter TheSky64.exe -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($hit) { $TheSkyExe = $hit.FullName }
}
$sky = @(Get-Process -Name TheSky64 -ErrorAction SilentlyContinue)
if ($sky.Count -gt 0) {
    $ids = ($sky | ForEach-Object { $_.Id }) -join ","
    Write-Log "TheSky already running (pid $ids)"
} elseif (-not $TheSkyExe -or -not (Test-Path $TheSkyExe)) {
    Write-Log "TheSky not found at $TheSkyExe (pass -TheSkyExe)."
    $failed = $true
} elseif ($DryRun) {
    Write-Log "would launch TheSky: $TheSkyExe"
} else {
    Start-Process -FilePath $TheSkyExe | Out-Null
    Write-Log "Launched TheSky: $TheSkyExe"
}
if (-not $DryRun) {
    $deadline = (Get-Date).AddSeconds($TheSkyWaitS)
    while (-not (Test-Port "localhost" $TheSkyPort) -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 5 }
}
$mount = Get-TheSkyMount
if (-not $DryRun -and $mount -ne "connected") {
    $deadline = (Get-Date).AddSeconds($MountWaitS)
    while ($mount -ne "connected" -and (Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 10
        $mount = Get-TheSkyMount
    }
}
Write-Log "TheSky :$TheSkyPort mount: $mount"
if ($mount -ne "connected" -and -not $DryRun) {
    Write-Log "TheSky mount is not connected: connect it in TheSky (Telescope > Connect), then run observatory-apps.ps1 -Start."
}

# ---------------------------------------------------------------- 3. NINA x2 + PHD2
$apps = Join-Path $Repo "deploy\observatory-apps.ps1"
$psExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$appArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $apps, "-Start", "-Api", $Api, "-Repo", $Repo)
if ($DryRun) { $appArgs += "-DryRun" }
Write-Log ("observatory-apps.ps1 {0}" -f (($appArgs | Select-Object -Skip 5) -join " "))
& $psExe @appArgs
$rc = $LASTEXITCODE
Write-Log "observatory-apps.ps1 -Start exit $rc"
if ($rc -ne 0) { $failed = $true }

if ($failed) {
    Write-Log "Reboot recovery finished with problems (see above)."
    exit 1
}
Write-Log "Reboot recovery done."
exit 0
