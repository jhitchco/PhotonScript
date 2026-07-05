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

# Lights (already accepted-only, per filter)
$nLights = 0
$exposures = @{}
Get-ChildItem $src -Recurse -Filter *.fits | ForEach-Object {
    $filter = $_.Directory.Name
    $nLights += Add-File $_ (Join-Path $stage "LIGHTS\$filter")
    if ($_.Name -match '_(\d+(?:\.\d+)?)s') { $exposures[$matches[1]] = $true }
}

# Darks: only sessions whose exposure matches the lights'
$nDarks = 0
$darkRoot = Join-Path $Library "Calibration\DARK"
if (Test-Path $darkRoot) {
    Get-ChildItem $darkRoot -Recurse -Filter *.fits | ForEach-Object {
        foreach ($e in $exposures.Keys) {
            if ($_.Name -match "_$($e)") {
                $nDarks += Add-File $_ (Join-Path $stage "DARKS")
                break
            }
        }
    }
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
Write-Host "  Darks (matched): $nDarks new · Bias: $nBias new · Flats: $nFlats new"
if ($nDarks -eq 0) { Write-Warning "No darks match the light exposures - capture some on the next cloudy night." }
if ($nFlats -eq 0) { Write-Warning "No flats staged - dawn sky flats will cover future nights." }
Write-Host ""
Write-Host "PixInsight: Scripts > Batch Processing > WeightedBatchPreprocessing"
Write-Host "  -> '+ Directory' -> $stage  (WBPP groups by headers automatically)"
$pix = "C:\Program Files\PixInsight\bin\PixInsight.exe"
if (Test-Path $pix) {
    $launch = Read-Host "Launch PixInsight now? (y/n)"
    if ($launch -eq 'y') { Start-Process $pix }
}
