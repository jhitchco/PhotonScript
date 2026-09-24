# Stage the OSC (one-shot-color, AP26CC / 600mm) lights + matched calibration
# for the PixInsight OSC pipeline (integrate_osc.js). Run on the DESKTOP:
#   .\deploy\prepare-integration-osc.ps1 -Name "M31_OSC"
# Then:
#   .\deploy\run-integration-osc.ps1 -Name "M31_OSC"
#
# Calibration is matched to the OSC camera (INSTRUME=AP26CC) AND the light epoch
# (exposure|gain|offset|temp). The existing library calibration is all AP26MC
# (the mono main cam) so nothing will match until OSC darks/flats/bias are shot
# (see the sequencer's OSC calibration plan). The pipeline runs fine uncalibrated.
param(
    [string]$Name = "M31_OSC",
    [string]$Input = "$env:USERPROFILE\ninashare\Library\_\OSC",
    [string]$Library = "$env:USERPROFILE\ninashare\Library",
    [string]$StageRoot = "$env:USERPROFILE\Astrophotography\Staging",
    [string]$Instrument = "AP26CC",
    [switch]$Copy   # default = hardlink; -Copy to physically copy
)

if (-not (Test-Path $Input)) { Write-Error "No OSC lights at $Input"; exit 1 }
$stage = Join-Path $StageRoot ($Name -replace '[^\w\- ]','_')
New-Item -ItemType Directory -Force -Path $stage | Out-Null

function Get-FitsKeys($path) {
    $fs = [System.IO.File]::OpenRead($path)
    $buf = New-Object byte[] (2880 * 6)
    $n = $fs.Read($buf, 0, $buf.Length); $fs.Close()
    $txt = [System.Text.Encoding]::ASCII.GetString($buf, 0, $n)
    $out = @{}
    foreach ($k in @("GAIN","OFFSET","EXPTIME","SET-TEMP")) {
        if ($txt -match "$k\s*=\s*(-?[\d.]+)") { $out[$k] = [double]$matches[1] }
    }
    if ($txt -match "INSTRUME\s*=\s*'?\s*([A-Za-z0-9_]+)") { $out["INSTRUME"] = $matches[1] }
    return $out
}
function Add-File($file, $destDir) {
    New-Item -ItemType Directory -Force -Path $destDir | Out-Null
    $dest = Join-Path $destDir $file.Name
    if (Test-Path $dest) { return 0 }
    if ($Copy) { Copy-Item $file.FullName $dest }
    else { try { New-Item -ItemType HardLink -Path $dest -Target $file.FullName | Out-Null }
           catch { Copy-Item $file.FullName $dest } }
    return 1
}

# Lights -> LIGHTS/OSC ; record epochs
$nLights = 0; $epochs = @{}
Get-ChildItem $Input -Filter *.fits | ForEach-Object {
    $nLights += Add-File $_ (Join-Path $stage "LIGHTS\OSC")
    $k = Get-FitsKeys $_.FullName
    if ($k.EXPTIME) { $epochs["$($k.EXPTIME)|$($k.GAIN)|$($k.OFFSET)|$($k.'SET-TEMP')"] = $true }
}
Write-Host "Light epochs (exp|gain|offset|temp): $($epochs.Keys -join '  |  ')"

# Darks: instrument + full-epoch match
$nDarks = 0; $nDarkSkip = 0
$darkRoot = Join-Path $Library "Calibration\DARK"
if (Test-Path $darkRoot) {
    Get-ChildItem $darkRoot -Recurse -Filter *.fits | ForEach-Object {
        $k = Get-FitsKeys $_.FullName
        $sig = "$($k.EXPTIME)|$($k.GAIN)|$($k.OFFSET)|$($k.'SET-TEMP')"
        if ($k.INSTRUME -eq $Instrument -and $epochs.ContainsKey($sig)) {
            $nDarks += Add-File $_ (Join-Path $stage "DARKS")
        } else { $nDarkSkip++ }
    }
}

# Bias: newest session that is the OSC instrument
$nBias = 0
foreach ($bn in @("BIAS","BIA")) {
    $root = Join-Path $Library "Calibration\$bn"
    if (Test-Path $root) {
        Get-ChildItem $root -Directory | Sort-Object Name -Descending | ForEach-Object {
            if ($nBias -gt 0) { return }
            $frames = Get-ChildItem $_.FullName -Filter *.fits
            if ($frames.Count -and (Get-FitsKeys $frames[0].FullName).INSTRUME -eq $Instrument) {
                $frames | ForEach-Object { $nBias += Add-File $_ (Join-Path $stage "BIAS") }
            }
        }
    }
}

# Flats: instrument match -> FLATS/OSC
$nFlats = 0
$flatRoot = Join-Path $Library "Calibration\FLAT"
if (Test-Path $flatRoot) {
    Get-ChildItem $flatRoot -Recurse -Filter *.fits | ForEach-Object {
        if ((Get-FitsKeys $_.FullName).INSTRUME -eq $Instrument) {
            $nFlats += Add-File $_ (Join-Path $stage "FLATS\OSC")
        }
    }
}

Write-Host ""
Write-Host "Staged for OSC integration: $stage"
Write-Host "  Lights: $nLights  |  Darks: $nDarks (skipped $nDarkSkip)  |  Bias: $nBias  |  Flats: $nFlats"
if ($nDarks -eq 0) { Write-Warning "No $Instrument darks match (need 120s/gain/offset/temp at $Instrument) - run the OSC calibration plan on a cloudy night. Pipeline will run UNCALIBRATED." }
if ($nFlats -eq 0) { Write-Warning "No $Instrument flats - shoot OSC sky/panel flats. Pipeline will run without flat correction." }
Write-Host ""
Write-Host "Next: .\deploy\run-integration-osc.ps1 -Name `"$Name`""
