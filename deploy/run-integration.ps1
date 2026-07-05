# Headless(ish) SHO integration: stages must exist (run prepare-integration.ps1
# first). Fills the staging path into integrate_sho.js and runs it in
# PixInsight. PixInsight stays open at the end with the console log.
#   .\deploy\run-integration.ps1 -Target "Crescent Nebula"
param(
    [Parameter(Mandatory=$true)][string]$Target,
    [string]$StageRoot = "$env:USERPROFILE\Astrophotography\Staging",
    [string]$PixInsight = "C:\Program Files\PixInsight\bin\PixInsight.exe"
)
$stage = Join-Path $StageRoot ($Target -replace '[^\w\- ]','_')
if (-not (Test-Path "$stage\LIGHTS")) {
    Write-Error "No staged lights at $stage - run prepare-integration.ps1 first."
    exit 1
}
if (-not (Test-Path $PixInsight)) {
    Write-Error "PixInsight not found at $PixInsight"
    exit 1
}
$js = Get-Content (Join-Path $PSScriptRoot "integrate_sho.js") -Raw
$js = $js -replace '__STAGING__', ($stage -replace '\\','/')
$tmp = Join-Path $env:TEMP "photonscript_integrate.js"
Set-Content -Path $tmp -Value $js -Encoding UTF8
Write-Host "Launching PixInsight pipeline for '$Target'..."
Write-Host "  bias/dark masters -> per-filter flats -> calibrate -> cosmetic -> register -> integrate"
& $PixInsight -n --run="$tmp"
