# Fill the staging path into integrate_osc.js and run it in PixInsight.
# Stage first with prepare-integration-osc.ps1.
#   .\deploy\run-integration-osc.ps1 -Name "M31_OSC"
param(
    [string]$Name = "M31_OSC",
    [string]$StageRoot = "$env:USERPROFILE\Astrophotography\Staging",
    [string]$PixInsight = "C:\Program Files\PixInsight\bin\PixInsight.exe"
)
$stage = Join-Path $StageRoot ($Name -replace '[^\w\- ]','_')
if (-not (Test-Path "$stage\LIGHTS")) {
    Write-Error "No staged lights at $stage - run prepare-integration-osc.ps1 first."; exit 1
}
if (-not (Test-Path $PixInsight)) { Write-Error "PixInsight not found at $PixInsight"; exit 1 }
$js = [System.IO.File]::ReadAllText((Join-Path $PSScriptRoot "integrate_osc.js"))
$js = $js -replace '__STAGING__', ($stage -replace '\\','/')
$runjs = Join-Path $stage "integrate_osc_run.js"
# BOM-less write: PowerShell's UTF8 adds a BOM that breaks PixInsight's parser
[System.IO.File]::WriteAllText($runjs, $js)
Write-Host "Launching PixInsight OSC pipeline for '$Name'..."
Write-Host "  bias/dark/flat -> calibrate -> cosmetic -> debayer(RGGB) -> SubframeSelector(SSWEIGHT)"
Write-Host "  -> StarAlignment(distortion) -> weighted integration -> masterOSC + review.jpg"
Write-Host "Script: $runjs"
& $PixInsight -n "-r=$runjs" "--run=$runjs"
Write-Host ""
Write-Host "If no [OSC] lines appear within ~15s: SCRIPT menu > Execute Script File... > $runjs"
