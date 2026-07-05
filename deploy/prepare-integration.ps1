# Stage one target's accepted lights + matched calibration for PixInsight WBPP.
# Run on the DESKTOP:
#   .\prepare-integration.ps1 -Target "Crescent Nebula"
# Then in PixInsight: Scripts > Batch Processing > WeightedBatchPreprocessing,
# click "+ Directory" and pick the staging folder — WBPP auto-classifies
# lights/darks/bias by FITS headers and groups by filter/exposure.

param(
    [Parameter(Mandatory=$true)][string]$Target,
    [string]$Library = "$env:USERPROFILE\ninashare\Library",
    [string]$StageRoot = "$env:USERPROFILE\Astrophotography\Staging",
    [switch]$Copy   # default = hardlink (instant, no extra disk); -Copy to copy
)

$src = Join-Path $Library $Target
if (-not (Test-Path $src)) {
    Write-Error "No '$Target' in $Library. Folders present:"
    Get-ChildItem $Library -Directory | ForEach-Object { Write-Host "  $($_.Name)" }
    exit 1
}
$stage = Join-Path $StageRoot ($Target -replace '[^\w\- ]','_')
New-Item -ItemType Directory -Force -Path $stage | Out-Null

function Get-FitsKeys($path) {
    # FITS headers are ASCII 80-char cards in the first blocks — cheap to read
    $fs = [System.IO.File]::OpenRead($path)
    $buf = New-Object byte[] (2880 * 4)
    $n = $fs.Read($buf, 0, $buf.Length); $fs.Close()
    $txt = [System.Text.Encoding]::ASCII.GetString($buf, 0, $n)
    $out = @{}
    foreach ($k in @("GAIN", "OFFSET", "EXPTIME", "SET-TEMP")) {
        if ($txt -match "$k\s*=\s*(-?[\d.]+)") { $out[$k] = [double]$matches[1] }
    }
    return $out
}

function Add-File($file, $destDir) {
    New-Item -ItemType Directory -Force -Path $destDir | Out-Null
    $dest = Join-Path $destDir $file.Name
    if (Test-Path $dest) { return 0 }
    if ($Copy) { Copy-Item $file.FullName $dest }
    else {
        try { New-Item -ItemType HardLink -Path $dest -Target $file.FullName | Out-Null }
        catch { Copy-Item $file.FullName $dest }
    }
    return 1
}

# Lights (already accepted-only, per filter) — record their epochs
$nLights = 0
$epochs = @{}
Get-ChildItem $src -Recurse -Filter *.fits | ForEach-Object {
    $filter = $_.Directory.Name
    $nLights += Add-File $_ (Join-Path $stage "LIGHTS\$filter")
    $k = Get-FitsKeys $_.FullName
    if ($k.EXPTIME) {
        $sig = "$($k.EXPTIME)|$($k.GAIN)|$($k.OFFSET)|$($k.'SET-TEMP')"
        $epochs[$sig] = $true
    }
}
Write-Host "Light epochs (exp|gain|offset|temp): $($epochs.Keys -join '  ·  ')"

# Darks: header-matched on exposure AND gain AND offset AND temperature —
# a dark from the wrong epoch silently poisons calibration
$nDarks = 0; $nDarkSkipped = 0
$darkRoot = Join-Path $Library "Calibration\DARK"
if (Test-Path $darkRoot) {
    Get-ChildItem $darkRoot -Recurse -Filter *.fits | ForEach-Object {
        $k = Get-FitsKeys $_.FullName
        $sig = "$($k.EXPTIME)|$($k.GAIN)|$($k.OFFSET)|$($k.'SET-TEMP')"
        if ($epochs.ContainsKey($sig)) {
            $nDarks += Add-File $_ (Join-Path $stage "DARKS")
        } else { $nDarkSkipped++ }
    }
}
if ($nDarkSkipped) {
    Write-Host "  ($nDarkSkipped darks skipped: wrong exposure/gain/offset/temp epoch)"
}

# Bias: newest session
$nBias = 0
foreach ($biasName in @("BIAS", "BIA")) {
    $biasRoot = Join-Path $Library "Calibration\$biasName"
    if (Test-Path $biasRoot) {
        $newest = Get-ChildItem $biasRoot -Directory |
            Sort-Object Name -Descending | Select-Object -First 1
        if ($newest) {
            Get-ChildItem $newest.FullName -Filter *.fits | ForEach-Object {
                $nBias += Add-File $_ (Join-Path $stage "BIAS")
            }
        }
    }
}

# Flats if any exist
$nFlats = 0
$flatRoot = Join-Path $Library "Calibration\FLAT"
if (Test-Path $flatRoot) {
    Get-ChildItem $flatRoot -Recurse -Filter *.fits | ForEach-Object {
        $nFlats += Add-File $_ (Join-Path $stage "FLATS")
    }
}

Write-Host ""
Write-Host "Staged for integration: $stage"
Write-Host "  Lights: $nLights new (exposures: $($exposures.Keys -join 's, ')s)"
Write-Host "  Darks (epoch-matched): $nDarks new · Bias: $nBias new · Flats: $nFlats new"
if ($nDarks -eq 0) { Write-Warning "No darks match the lights' exposure/gain/offset/temp - capture a matching set on the next cloudy night (the button on the dashboard)." }
if ($nFlats -eq 0) { Write-Warning "No flats staged - dawn sky flats will cover future nights." }
Write-Host ""
Write-Host "PixInsight: Scripts > Batch Processing > WeightedBatchPreprocessing"
Write-Host "  -> '+ Directory' -> $stage  (WBPP groups by headers automatically)"
$pix = "C:\Program Files\PixInsight\bin\PixInsight.exe"
if (Test-Path $pix) {
    $launch = Read-Host "Launch PixInsight now? (y/n)"
    if ($launch -eq 'y') { Start-Process $pix }
}
