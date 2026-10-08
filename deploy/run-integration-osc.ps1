# Cull split-pointing / duplicate subs, fill the staging path into
# integrate_osc.js and run it in PixInsight.
# Stage first with prepare-integration-osc.ps1.
#   .\deploy\run-integration-osc.ps1 -Name "M31_OSC"
#   .\deploy\run-integration-osc.ps1 -Name "M31_OSC" -CullDryRun   # report only, then stop
#   .\deploy\run-integration-osc.ps1 -Name "M31_OSC" -NoCull       # skip the cull
# The cull (photonscript/image_processor/osc_cull.py, needs Python + numpy)
# MOVES rejects to <stage>\REJECTED\<reason>\ (never deletes), writes
# cull_report.csv and reference.txt (sharpest kept sub = registration reference).
param(
    [string]$Name = "M31_OSC",
    [string]$StageRoot = "$env:USERPROFILE\Astrophotography\Staging",
    [string]$PixInsight = "C:\Program Files\PixInsight\bin\PixInsight.exe",
    # CPU niceness: Idle = only spare cycles, BelowNormal = yields to your apps,
    # Normal = full speed. Default BelowNormal so the box stays usable.
    [ValidateSet('Idle','BelowNormal','Normal')][string]$Priority = 'BelowNormal',
    # Cap cores PixInsight may use (0 = all). Sets CPU affinity to the first N cores.
    [int]$MaxCores = 0,
    [switch]$NoCull,        # skip osc_cull.py
    [switch]$CullDryRun,    # run osc_cull.py in report-only mode and stop
    [switch]$RejectBright   # also move sky-background outliers (default: flag only)
)
$stage = Join-Path $StageRoot ($Name -replace '[^\w\- ]','_')
$hasLights = (Test-Path "$stage\LIGHTS") -or `
    ($null -ne (Get-ChildItem $stage -Filter *.fits -ErrorAction SilentlyContinue | Select-Object -First 1))
if (-not $hasLights) {
    Write-Error "No staged lights at $stage (need a LIGHTS\ folder or loose *.fits) - run prepare-integration-osc.ps1, or drop culled fits in $stage."; exit 1
}
if (-not (Test-Path $PixInsight)) { Write-Error "PixInsight not found at $PixInsight"; exit 1 }

# --- pre-integration cull (split pointing, duplicates, bright-sky flags) ---
if (-not $NoCull) {
    $cull = Join-Path $PSScriptRoot "..\photonscript\image_processor\osc_cull.py"
    # Python for the cull: the repo's .venv first, then py / python / python3 on PATH.
    # Pick the first one that can actually import numpy.
    $venvPy = Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe"
    $cands = @()
    if (Test-Path $venvPy) { $cands += ,@($venvPy) }
    foreach ($c in @("py -3", "python", "python3")) {
        $parts = $c -split ' '
        if (Get-Command $parts[0] -ErrorAction SilentlyContinue) { $cands += ,$parts }
    }
    $pyParts = $null
    foreach ($parts in $cands) {
        $pre = @(); if ($parts.Count -gt 1) { $pre = $parts[1..($parts.Count - 1)] }
        & $parts[0] @pre -c "import numpy" 2>$null
        if ($LASTEXITCODE -eq 0) { $pyParts = $parts; break }
    }
    if (-not $pyParts) {
        Write-Host ""
        Write-Host "osc_cull.py needs numpy and no Python here has it. One-time fix (repo venv):" -ForegroundColor Yellow
        Write-Host "    uv pip install --python .venv\Scripts\python.exe numpy" -ForegroundColor Yellow
        Write-Host "  (or, without uv:  py -3 -m pip install numpy)" -ForegroundColor Yellow
        Write-Error "No Python with numpy for osc_cull.py. Install as above or pass -NoCull."; exit 1
    }
    $cullArgs = @()
    if ($pyParts.Count -gt 1) { $cullArgs += $pyParts[1..($pyParts.Count - 1)] }
    $cullArgs += @($cull, $stage)
    if ($CullDryRun)   { $cullArgs += "--dry-run" }
    if ($RejectBright) { $cullArgs += "--reject-bright" }
    Write-Host "Culling split-pointing / duplicate subs ($($pyParts[0]) osc_cull.py)..."
    & $pyParts[0] @cullArgs
    if ($LASTEXITCODE -ne 0) { Write-Error "osc_cull.py failed (exit $LASTEXITCODE). Fix it or pass -NoCull."; exit 1 }
    if ($CullDryRun) { Write-Host "Dry run only: see $stage\cull_report.csv. Re-run without -CullDryRun to cull + integrate."; exit 0 }
}
$js = [System.IO.File]::ReadAllText((Join-Path $PSScriptRoot "integrate_osc.js"))
$js = $js -replace '__STAGING__', ($stage -replace '\\','/')
$runjs = Join-Path $stage "integrate_osc_run.js"
# BOM-less write: PowerShell's UTF8 adds a BOM that breaks PixInsight's parser
[System.IO.File]::WriteAllText($runjs, $js)
Write-Host "Launching PixInsight OSC pipeline for '$Name'..."
Write-Host "  clear stale intermediates -> bias/dark/flat -> calibrate -> cosmetic -> debayer(RGGB)"
Write-Host "  -> StarAlignment(distortion, ref=reference.txt) -> LocalNormalization"
Write-Host "  -> PSF-weighted integration -> masterOSC + review.jpg"
Write-Host "Script: $runjs"
Write-Host "  CPU: priority=$Priority$(if ($MaxCores -gt 0){" , cores=$MaxCores"})"
$proc = Start-Process -FilePath $PixInsight `
    -ArgumentList @("-n", "-r=$runjs", "--run=$runjs") -PassThru
Start-Sleep -Seconds 4
try {
    $proc.PriorityClass = [System.Diagnostics.ProcessPriorityClass]::$Priority
    Write-Host "Set PixInsight process priority to $Priority"
} catch { Write-Warning "Could not set priority ($_). Set it in Task Manager > Details > PixInsight.exe > Set priority." }
if ($MaxCores -gt 0) {
    try {
        $mask = [int]([math]::Pow(2, $MaxCores) - 1)
        $proc.ProcessorAffinity = [IntPtr]$mask
        Write-Host "Limited PixInsight to $MaxCores core(s)."
    } catch { Write-Warning "Could not set affinity ($_). Use Task Manager > Details > PixInsight.exe > Set affinity." }
}
Write-Host ""
Write-Host "If no [OSC] lines appear within ~15s: SCRIPT menu > Execute Script File... > $runjs"
Write-Host "Persistent option: PixInsight Edit > Global Preferences > Parallel processing -> cap the thread/processor count."
