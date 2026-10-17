# PS-170: daily observatory app lifecycle on the SCOPE PC.
#
# Closes NINA #1, NINA #2 and PHD2 after the dawn shutdown and launches them
# again before the noon re-arm, so every night starts on fresh driver
# instances (PHD2's in-process TheSky driver went stale after 2026-10-05 and
# refused every guide pulse for three nights, PS-167).
#
# Run by two scheduled tasks in jeremy's interactive session (GUI apps
# started from session 0 are invisible and unusable); install them with
# deploy\install-app-lifecycle-tasks.ps1. By hand, normal PowerShell:
#   powershell -ExecutionPolicy Bypass -File C:\astro\PhotonScript\deploy\observatory-apps.ps1 -Status
#   ... -Stop  [-DryRun] [-Force] [-IncludeTheSky]
#   ... -Start [-DryRun] [-SkipTheSkyCheck] [-NoPhd2Connect]
#
# The PhotonScript service decides, this script acts:
#   GET  /api/apps/status    enabled flag, stop guards, launch settings
#   POST /api/apps/report    the result of every run that acted (a failed
#                            run asks the service to page, once a day)
#   POST /api/equipment/connect, POST /api/preflight?push=0   after a start
#
# -Stop: refused unless the service answers and reports no blocker: armer
#   RUNNING / WATCHING / PAUSED_* / ARMED, a NINA sequence running, the sun
#   below -6 deg, the dawn shutdown or its cooler verify not done, or less
#   than app_lifecycle_stop_after_shutdown_min since the shutdown. -Force
#   only waives the shutdown timing (and the once-a-day rule), never the
#   armer, sequence or sun guards. PHD2: stop_capture, set_connected false,
#   shutdown (JSON-RPC). NINA: close the main window. Each app is killed
#   only after -CloseTimeoutS. TheSky is never touched unless -IncludeTheSky.
# -Start: idempotent, only what does not answer is launched. TheSky must be
#   running with its mount connected (else page and stop). Then NINA #1 and
#   NINA #2 with --profileid (profile name resolved from
#   %LOCALAPPDATA%\NINA\Profiles; never launched without one), then PHD2
#   (profile app_phd2_profile_id selected and equipment connected over its
#   event server). Each port must answer within -StartTimeoutS. Then
#   connect + preflight through the service.
# -Scheduled (set by the tasks): do nothing while app_lifecycle_enabled is
#   off, and stop at most once a day. A hand run ignores both.
#
# Log: <data_dir>\logs\observatory-apps.log (data_dir as in
# run-photonscript.ps1). Exit 0 = done or nothing to do, 1 = failed,
# 2 = refused.

param(
    [switch]$Stop,
    [switch]$Start,
    [switch]$Status,
    [switch]$DryRun,
    [switch]$Scheduled,
    [switch]$Force,
    [switch]$IncludeTheSky,
    [switch]$SkipTheSkyCheck,
    [switch]$NoPhd2Connect,
    [string]$Api = "http://127.0.0.1:8100",
    [string]$Repo = "C:\astro\PhotonScript",
    [int]$StartTimeoutS = 180,
    [int]$CloseTimeoutS = 60,
    # Fallbacks when the service does not answer (the service's config wins)
    [string]$NinaExe = "C:\Program Files\N.I.N.A. - Nighttime Imaging 'N' Astronomy\NINA.exe",
    [string]$Nina1Profile = "RC16",
    [string]$Nina2Profile = "Piggy-600",
    [string]$Phd2Exe = "C:\Program Files (x86)\PHDGuiding2\phd2.exe",
    [int]$Phd2ProfileId = 2
)

$ErrorActionPreference = "Stop"
$modes = @($Stop, $Start, $Status) | Where-Object { $_ }
if (@($modes).Count -ne 1) {
    Write-Host "Pick exactly one of -Stop, -Start, -Status." -ForegroundColor Red
    exit 2
}
$mode = if ($Stop) { "stop" } elseif ($Start) { "start" } else { "status" }

# ---------------------------------------------------------------- logging
$dataDir = Join-Path $env:USERPROFILE ".photonscript"
$envFile = Join-Path $Repo ".env"
if (Test-Path $envFile) {
    $m = Select-String -Path $envFile -Pattern '^\s*PS_DATA_DIR\s*=\s*(.+?)\s*$' | Select-Object -First 1
    if ($m) { $dataDir = $m.Matches[0].Groups[1].Value.Trim('"', "'") }
}
$logDir = Join-Path $dataDir "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "observatory-apps.log"
if ((Test-Path $log) -and ((Get-Item $log).Length -gt 1MB)) {
    Move-Item -Force $log "$log.1"
}
$steps = New-Object System.Collections.ArrayList

function Write-Log([string]$msg) {
    $line = "{0} [{1}] {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $mode, $msg
    Write-Host $line
    Add-Content -Path $log -Value $line -Encoding UTF8
}

function Add-Step([string]$msg) {
    [void]$steps.Add($msg)
    Write-Log $msg
}

# ---------------------------------------------------------------- service API
function Get-Api([string]$path, [int]$timeout = 30) {
    try { return Invoke-RestMethod -Uri ($Api + $path) -Method Get -TimeoutSec $timeout }
    catch { Write-Log ("GET {0} failed: {1}" -f $path, $_.Exception.Message); return $null }
}

function Post-Api([string]$path, $body = $null, [int]$timeout = 60) {
    try {
        $json = if ($null -ne $body) { $body | ConvertTo-Json -Depth 6 -Compress } else { "{}" }
        return Invoke-RestMethod -Uri ($Api + $path) -Method Post -Body $json `
            -ContentType "application/json" -TimeoutSec $timeout
    } catch { Write-Log ("POST {0} failed: {1}" -f $path, $_.Exception.Message); return $null }
}

function Send-Report([bool]$ok, [bool]$acted, [string]$message, [bool]$page) {
    $body = @{ mode = $mode; ok = $ok; acted = $acted; dry_run = [bool]$DryRun
               scheduled = [bool]$Scheduled; message = $message; page = $page
               steps = @($steps) }
    Write-Log ("result: ok={0} acted={1} {2}" -f $ok, $acted, $message)
    $r = Post-Api "/api/apps/report" $body 30
    if ($null -eq $r) { Write-Log "report not delivered (service down?)" }
}

# ---------------------------------------------------------------- probes
function Test-Port([string]$h, [int]$p, [int]$ms = 1500) {
    $c = New-Object System.Net.Sockets.TcpClient
    try {
        $iar = $c.BeginConnect($h, $p, $null, $null)
        if (-not $iar.AsyncWaitHandle.WaitOne($ms)) { return $false }
        $c.EndConnect($iar)
        return $true
    } catch { return $false } finally { $c.Close() }
}

function Wait-Port([string]$h, [int]$p, [int]$timeoutS) {
    $deadline = (Get-Date).AddSeconds($timeoutS)
    while ((Get-Date) -lt $deadline) {
        if (Test-Port $h $p) { return $true }
        Start-Sleep -Seconds 3
    }
    return $false
}

function Get-PortOwner([int]$p) {
    try {
        $c = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction Stop | Select-Object -First 1
        if ($c) { return [int]$c.OwningProcess }
    } catch { }
    return $null
}

# ---------------------------------------------------------------- PHD2 JSON-RPC
$script:rpcId = 100
function Invoke-Phd2Rpc([string]$method, $params = $null, [int]$timeoutMs = 10000) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $iar = $client.BeginConnect("localhost", $phd2Port, $null, $null)
        if (-not $iar.AsyncWaitHandle.WaitOne(3000)) { throw ("PHD2 :{0} did not answer" -f $phd2Port) }
        $client.EndConnect($iar)
        $stream = $client.GetStream()
        $stream.ReadTimeout = $timeoutMs
        $reader = New-Object System.IO.StreamReader($stream, [System.Text.Encoding]::ASCII)
        $writer = New-Object System.IO.StreamWriter($stream, [System.Text.Encoding]::ASCII)
        $writer.NewLine = "`r`n"
        $writer.AutoFlush = $true
        $script:rpcId++
        $req = @{ method = $method; id = $script:rpcId }
        if ($null -ne $params) { $req.params = $params }
        $writer.WriteLine(($req | ConvertTo-Json -Compress -Depth 4))
        $deadline = (Get-Date).AddMilliseconds($timeoutMs)
        while ((Get-Date) -lt $deadline) {
            $line = $null
            try { $line = $reader.ReadLine() } catch { break }
            if ($null -eq $line) { break }
            if ($line -notmatch '"jsonrpc"') { continue }   # an event line
            $msg = $line | ConvertFrom-Json
            if ($msg.id -ne $script:rpcId) { continue }
            if ($msg.error) { throw ("PHD2 {0}: {1}" -f $method, $msg.error.message) }
            return $msg.result
        }
        throw ("PHD2 {0}: no reply" -f $method)
    } finally { $client.Close() }
}

# ---------------------------------------------------------------- TheSky (read only)
function Get-TheSkyMount {
    # Same framing as photonscript.telescope_agent.thesky_client; reads
    # IsConnected only (never Connect, park or slew).
    $js = "/* Java Script */`n/* Socket Start Packet */`n" +
          "var Out; var c = false; try { c = sky6RASCOMTele.IsConnected; } catch (e) {} " +
          "Out = c ? 'connected=1' : 'connected=0';" +
          "`n/* Socket End Packet */`n"
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $iar = $client.BeginConnect("localhost", $theskyPort, $null, $null)
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

# ---------------------------------------------------------------- NINA profiles
function Resolve-NinaProfile([string]$ref) {
    if ($ref -match '^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$') { return $ref }
    $dir = Join-Path $env:LOCALAPPDATA "NINA\Profiles"
    $hits = @()
    foreach ($f in @(Get-ChildItem -Path $dir -Filter *.profile -ErrorAction SilentlyContinue)) {
        try { [xml]$x = Get-Content -Raw -LiteralPath $f.FullName } catch { continue }
        $kids = @($x.DocumentElement.ChildNodes)
        $n = $kids | Where-Object { $_.LocalName -eq "Name" } | Select-Object -First 1
        $i = $kids | Where-Object { $_.LocalName -eq "Id" } | Select-Object -First 1
        if ($n -and $n.InnerText -eq $ref) {
            if ($i) { $hits += $i.InnerText } else { $hits += $f.BaseName }
        }
    }
    if ($hits.Count -eq 1) { return $hits[0] }
    if ($hits.Count -gt 1) { throw ("NINA profile '{0}' is ambiguous ({1} files)" -f $ref, $hits.Count) }
    throw ("NINA profile '{0}' not found in {1}" -f $ref, $dir)
}

# ---------------------------------------------------------------- close helper
function Close-Gracefully($proc, [string]$label) {
    if ($proc.HasExited) { return "already closed" }
    if ($proc.MainWindowHandle -ne [IntPtr]::Zero) { [void]$proc.CloseMainWindow() }
    if ($proc.WaitForExit($CloseTimeoutS * 1000)) { return "closed" }
    Stop-Process -Id $proc.Id -Force
    return ("KILLED after {0} s (did not close)" -f $CloseTimeoutS)
}

# ---------------------------------------------------------------- settings
$st = Get-Api "/api/apps/status" 60
$launch = if ($st) { $st.launch } else { $null }
if ($launch) {
    $NinaExe = $launch.nina_exe; $Nina1Profile = $launch.nina1_profile
    $Nina2Profile = $launch.nina2_profile; $Phd2Exe = $launch.phd2_exe
    $Phd2ProfileId = [int]$launch.phd2_profile_id
}
$nina1Port = if ($launch) { [int]$launch.nina1_port } else { 1888 }
$nina2Port = if ($launch) { [int]$launch.nina2_port } else { 1889 }
$nina2On = if ($launch) { [bool]$launch.nina2_enabled } else { $true }
$phd2Port = if ($launch) { [int]$launch.phd2_port } else { 4400 }
$theskyPort = if ($launch) { [int]$launch.thesky_port } else { 3040 }

Write-Log ("run (user {0}, session {1}, scheduled={2}, dry-run={3}, service {4})" -f `
    $env:USERNAME, (Get-Process -Id $PID).SessionId, [bool]$Scheduled, [bool]$DryRun,
    $(if ($st) { "answers" } else { "DOWN" }))

if ($Scheduled -and $st -and -not $st.enabled) {
    Write-Log "app_lifecycle_enabled is off: nothing to do"
    exit 0
}

# ---------------------------------------------------------------- -Status
if ($Status) {
    $rows = @(@("NINA #1", $nina1Port), @("PHD2", $phd2Port), @("TheSky", $theskyPort))
    if ($nina2On) { $rows += ,@("NINA #2", $nina2Port) }
    foreach ($r in $rows) {
        Write-Log ("{0,-8} :{1} {2}" -f $r[0], $r[1], $(if (Test-Port "localhost" $r[1]) { "answers" } else { "NOT answering" }))
    }
    Write-Log ("TheSky mount: {0}" -f (Get-TheSkyMount))
    if ($st) {
        Write-Log ("service: enabled={0} armer={1} stop_ok={2} stop_done_today={3}" -f `
            $st.enabled, $st.armer_state, $st.stop_ok, $st.stop_done_today)
        foreach ($b in @($st.stop_blockers)) { if ($b) { Write-Log "  stop blocker: $b" } }
    }
    exit 0
}

# ---------------------------------------------------------------- -Stop
if ($Stop) {
    if (-not $st) {
        Write-Log "REFUSED: the PhotonScript service does not answer, so the stop guards cannot be checked"
        exit 2
    }
    if ($Scheduled -and $st.stop_done_today -and -not $Force) {
        Write-Log "already stopped today: leaving the apps alone"
        exit 0
    }
    $waivable = '^(waiting until|dawn shutdown cooler verify|night over but)'
    $blockers = @(@($st.stop_blockers) | Where-Object { $_ -and -not ($Force -and $_ -match $waivable) })
    if ($blockers.Count -gt 0) {
        Write-Log ("not now: {0}" -f ($blockers -join "; "))
        if (-not $Scheduled) { Add-Step ("refused: " + ($blockers -join "; ")); Send-Report $false $false "stop refused" $false }
        exit 2
    }
    $phd2 = @(Get-Process -Name phd2 -ErrorAction SilentlyContinue)
    $ninas = @(Get-Process -Name NINA -ErrorAction SilentlyContinue)
    $sky = @(Get-Process -Name TheSky64 -ErrorAction SilentlyContinue)
    if ($phd2.Count -eq 0 -and $ninas.Count -eq 0 -and -not ($IncludeTheSky -and $sky.Count -gt 0)) {
        Add-Step "nothing running"
        Send-Report $true $true "nothing to stop" $false
        exit 0
    }
    if ($DryRun) {
        Add-Step ("would close: PHD2 x{0}, NINA x{1}{2}" -f $phd2.Count, $ninas.Count,
            $(if ($IncludeTheSky) { ", TheSky x$($sky.Count)" } else { "" }))
        Send-Report $true $false "dry run" $false
        exit 0
    }
    $failed = $false
    # PHD2: stop looping, release camera + mount (the stale driver), shutdown
    if ($phd2.Count -gt 0) {
        if (Test-Port "localhost" $phd2Port) {
            foreach ($call in @(@("stop_capture", $null), @("set_connected", @($false)), @("shutdown", $null))) {
                try { [void](Invoke-Phd2Rpc $call[0] $call[1]); Add-Step ("PHD2 {0} ok" -f $call[0]) }
                catch { Add-Step ("PHD2 {0} failed: {1}" -f $call[0], $_.Exception.Message) }
            }
        }
        foreach ($p in $phd2) {
            $r = Close-Gracefully $p "PHD2"
            Add-Step ("PHD2 pid {0}: {1}" -f $p.Id, $r)
        }
    }
    # NINA: #2 first, then #1, then any other NINA
    $owner1 = Get-PortOwner $nina1Port
    $owner2 = Get-PortOwner $nina2Port
    $ordered = @($ninas | Sort-Object { if ($_.Id -eq $owner2) { 0 } elseif ($_.Id -eq $owner1) { 1 } else { 2 } })
    foreach ($p in $ordered) {
        $label = if ($p.Id -eq $owner1) { "NINA #1" } elseif ($p.Id -eq $owner2) { "NINA #2" } else { "NINA" }
        $r = Close-Gracefully $p $label
        Add-Step ("{0} pid {1}: {2}" -f $label, $p.Id, $r)
    }
    if ($IncludeTheSky) {
        foreach ($p in $sky) {
            $r = Close-Gracefully $p "TheSky"
            Add-Step ("TheSky pid {0}: {1}" -f $p.Id, $r)
        }
    }
    $left = @(Get-Process -Name phd2, NINA -ErrorAction SilentlyContinue)
    if ($left.Count -gt 0) { $failed = $true; Add-Step ("still running: {0}" -f (($left | ForEach-Object { "$($_.Name) $($_.Id)" }) -join ", ")) }
    $killed = @($steps | Where-Object { $_ -match "KILLED" }).Count
    $msg = if ($failed) { "some apps did not close" } elseif ($killed) { "closed ($killed killed after the timeout)" } else { "closed" }
    Send-Report (-not $failed) $true $msg $failed
    if ($failed) { exit 1 } else { exit 0 }
}

# ---------------------------------------------------------------- -Start
if ($Start) {
    if (-not $st) { Write-Log "service does not answer: launching anyway (connect + preflight skipped)" }
    if ($st -and -not $st.stop_done_today) {
        Add-Step "note: the apps were not closed today (stop blocked or not run), so running apps are not fresh"
    }
    # TheSky must be up with its mount connected (NINA and PHD2 drive the
    # Paramount through it). Never started or connected from here.
    if (-not $SkipTheSkyCheck) {
        $skyProc = @(Get-Process -Name TheSky64 -ErrorAction SilentlyContinue)
        $mount = if ($skyProc.Count -gt 0) { Get-TheSkyMount } else { "not running" }
        Add-Step ("TheSky: {0} process(es), mount {1}" -f $skyProc.Count, $mount)
        if ($mount -ne "connected") {
            Send-Report $false $false ("TheSky not ready (mount {0}): nothing launched; open TheSky, connect the mount, then run -Start" -f $mount) $true
            exit 1
        }
    }
    $launched = 0
    $failed = $false
    $nina = @(,@("NINA #1", $nina1Port, $Nina1Profile))
    if ($nina2On) { $nina += ,@("NINA #2", $nina2Port, $Nina2Profile) }
    foreach ($n in $nina) {
        $label = $n[0]; $port = [int]$n[1]
        if (Test-Port "localhost" $port) { Add-Step ("{0} :{1} already answers" -f $label, $port); continue }
        try { $pid2 = Resolve-NinaProfile $n[2] }
        catch { Add-Step ("{0}: {1}; NOT launched" -f $label, $_.Exception.Message); $failed = $true; continue }
        if ($DryRun) { Add-Step ("would launch {0}: `"{1}`" --profileid {2}" -f $label, $NinaExe, $pid2); continue }
        if (-not (Test-Path -LiteralPath $NinaExe)) { Add-Step ("{0}: {1} not found" -f $label, $NinaExe); $failed = $true; continue }
        $p = Start-Process -FilePath $NinaExe -ArgumentList @("--profileid", $pid2) -PassThru
        $launched++
        if (Wait-Port "localhost" $port $StartTimeoutS) {
            Add-Step ("{0} launched (pid {1}, profile {2}), :{3} answers" -f $label, $p.Id, $n[2], $port)
        } else {
            Add-Step ("{0} launched (pid {1}) but :{2} did not answer in {3} s" -f $label, $p.Id, $port, $StartTimeoutS)
            $failed = $true
        }
    }
    # PHD2: launch, select the profile, connect camera + mount (fresh driver)
    if (Test-Port "localhost" $phd2Port) {
        Add-Step ("PHD2 :{0} already answers" -f $phd2Port)
    } elseif ($DryRun) {
        Add-Step ("would launch PHD2: `"{0}`", profile id {1}" -f $Phd2Exe, $Phd2ProfileId)
    } elseif (-not (Test-Path -LiteralPath $Phd2Exe)) {
        Add-Step ("PHD2: {0} not found" -f $Phd2Exe); $failed = $true
    } else {
        $p = Start-Process -FilePath $Phd2Exe -PassThru
        $launched++
        if (Wait-Port "localhost" $phd2Port $StartTimeoutS) {
            Add-Step ("PHD2 launched (pid {0}), :{1} answers" -f $p.Id, $phd2Port)
            Start-Sleep -Seconds 5
            try {
                $prof = Invoke-Phd2Rpc "get_profile"
                if ([int]$prof.id -ne $Phd2ProfileId) {
                    if (Invoke-Phd2Rpc "get_connected") { [void](Invoke-Phd2Rpc "set_connected" @($false)) }
                    [void](Invoke-Phd2Rpc "set_profile" @{ id = $Phd2ProfileId })
                    Add-Step ("PHD2 profile {0} -> {1}" -f $prof.id, $Phd2ProfileId)
                } else {
                    Add-Step ("PHD2 profile {0} ({1})" -f $prof.id, $prof.name)
                }
                if (-not $NoPhd2Connect) {
                    [void](Invoke-Phd2Rpc "set_connected" @($true) 60000)
                    Add-Step "PHD2 equipment connected"
                }
            } catch {
                Add-Step ("PHD2 setup failed: {0}" -f $_.Exception.Message); $failed = $true
            }
        } else {
            Add-Step ("PHD2 launched (pid {0}) but :{1} did not answer in {2} s" -f $p.Id, $phd2Port, $StartTimeoutS)
            $failed = $true
        }
    }
    if ($DryRun) {
        Send-Report (-not $failed) $false "dry run" $false
        if ($failed) { exit 1 } else { exit 0 }
    }
    # Connect every rig's equipment and run preflight through the service
    if ($st) {
        if ($launched -gt 0) { Start-Sleep -Seconds 15 }
        $c = Post-Api "/api/equipment/connect" $null 300
        if ($c) {
            foreach ($rig in $c.results.PSObject.Properties) {
                $vals = ($rig.Value.PSObject.Properties | ForEach-Object { "$($_.Name)=$($_.Value)" }) -join ", "
                Add-Step ("connect {0}: {1}" -f $rig.Name, $vals)
            }
        } else { Add-Step "equipment connect call failed"; $failed = $true }
        $pf = Post-Api "/api/preflight?push=0" $null 300
        if ($pf) {
            $bad = @($pf.checks | Where-Object { $_.status -eq "fail" } | ForEach-Object { $_.name })
            Add-Step ("preflight go={0} (pass {1}, warn {2}, fail {3}){4}" -f $pf.go, $pf.summary.pass,
                $pf.summary.warn, $pf.summary.fail, $(if ($bad.Count) { ": " + ($bad -join ", ") } else { "" }))
        } else { Add-Step "preflight call failed"; $failed = $true }
    }
    $msg = if ($failed) { "start incomplete" } elseif ($launched) { "launched $launched app(s), connected, preflight go" } else { "all apps already running" }
    Send-Report (-not $failed) ($launched -gt 0) $msg $failed
    if ($failed) { exit 1 } else { exit 0 }
}
