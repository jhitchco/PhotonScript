# Headless(ish) SHO integration: stages must exist (run prepare-integration.ps1
# first). Fills the staging path into integrate_sho.js and runs it in
# PixInsight. PixInsight stays open at the end with the console log.
#   .\deploy\run-integration.ps1 -Target "Crescent Nebula"
param(
    [Parameter(Mandatory=$true)][string]$Target,
    [string]$StageRoot = $(if (Test-Path "D:\Astrophotography\Staging") { "D:\Astrophotography\Staging" } else { "$env:USERPROFILE\Astrophotography\Staging" }),
    [string]$PixInsight = "C:\Program Files\PixInsight\bin\PixInsight.exe",
    [double]$LockWaitMin = 60   # wait this long for another PixInsight job's queue lock
)
. (Join-Path $PSScriptRoot "pixinsight-lock.ps1")   # one PixInsight job at a time (pilock.py)
$stage = Join-Path $StageRoot ($Target -replace '[^\w\- ]','_')
if (-not (Test-Path "$stage\LIGHTS")) {
    Write-Error "No staged lights at $stage - run prepare-integration.ps1 first."
    exit 1
}
if (-not (Test-Path $PixInsight)) {
    Write-Error "PixInsight not found at $PixInsight"
    exit 1
}
$js = [System.IO.File]::ReadAllText((Join-Path $PSScriptRoot "integrate_sho.js"))
$js = $js -replace '__STAGING__', ($stage -replace '\\','/')
$runjs = Join-Path $stage "integrate_run.js"
# BOM-less write: PowerShell's UTF8 adds a BOM that breaks PixInsight's parser
[System.IO.File]::WriteAllText($runjs, $js)
Write-Host "Launching PixInsight pipeline for '$Target'..."
Write-Host "  bias/dark masters -> per-filter flats -> calibrate -> cosmetic -> register -> integrate"
Write-Host "Script: $runjs"
# PixInsight 1.9 startup-script flag is -r= (each form varies by build; try both)
$lockCmd = "run-integration.ps1 $Target"
Enter-PixInsightLock -Command $lockCmd -WaitMin $LockWaitMin
try {
    $proc = Start-Process -FilePath $PixInsight -ArgumentList @("-n", "-r=$runjs", "--run=$runjs") -PassThru
} catch { Remove-PixInsightLock; throw }
Set-PixInsightLockOwner -Process $proc -Command $lockCmd   # held until this PixInsight closes
Write-Host ""
Write-Host "If the PixInsight console shows no [SHO] lines within ~15s, run it manually:"
Write-Host "  SCRIPT menu > Execute Script File... > $runjs"
