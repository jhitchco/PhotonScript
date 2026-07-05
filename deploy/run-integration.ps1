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
$runjs = Join-Path $stage "integrate_run.js"
# BOM-less write: PowerShell's UTF8 adds a BOM that breaks PixInsight's parser
[System.IO.File]::WriteAllText($runjs, $js)
Write-Host "Launching PixInsight pipeline for '$Target'..."
Write-Host "  bias/dark masters -> per-filter flats -> calibrate -> cosmetic -> register -> integrate"
Write-Host "Script: $runjs"
# PixInsight 1.9 startup-script flag is -r= (each form varies by build; try both)
& $PixInsight -n "-r=$runjs" "--run=$runjs"
Write-Host ""
Write-Host "If the PixInsight console shows no [SHO] lines within ~15s, run it manually:"
Write-Host "  SCRIPT menu > Execute Script File... > $runjs"
