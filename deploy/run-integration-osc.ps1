# Fill the staging path into integrate_osc.js and run it in PixInsight.
# Stage first with prepare-integration-osc.ps1.
#   .\deploy\run-integration-osc.ps1 -Name "M31_OSC"
param(
    [string]$Name = "M31_OSC",
    [string]$StageRoot = "$env:USERPROFILE\Astrophotography\Staging",
    [string]$PixInsight = "C:\Program Files\PixInsight\bin\PixInsight.exe",
    # CPU niceness: Idle = only spare cycles, BelowNormal = yields to your apps,
    # Normal = full speed. Default BelowNormal so the box stays usable.
    [ValidateSet('Idle','BelowNormal','Normal')][string]$Priority = 'BelowNormal',
    # Cap cores PixInsight may use (0 = all). Sets CPU affinity to the first N cores.
    [int]$MaxCores = 0
)
$stage = Join-Path $StageRoot ($Name -replace '[^\w\- ]','_')
$hasLights = (Test-Path "$stage\LIGHTS") -or `
    ($null -ne (Get-ChildItem $stage -Filter *.fits -ErrorAction SilentlyContinue | Select-Object -First 1))
if (-not $hasLights) {
    Write-Error "No staged lights at $stage (need a LIGHTS\ folder or loose *.fits) - run prepare-integration-osc.ps1, or drop culled fits in $stage."; exit 1
}
if (-not (Test-Path $PixInsight)) { Write-Error "PixInsight not found at $PixInsight"; exit 1 }
$js = [System.IO.File]::ReadAllText((Join-Path $PSScriptRoot "integrate_osc.js"))
$js = $js -replace '__STAGING__', ($stage -replace '\\','/')
$runjs = Join-Path $stage "integrate_osc_run.js"
# BOM-less write: PowerShell's UTF8 adds a BOM that breaks PixInsight's parser
[System.IO.File]::WriteAllText($runjs, $js)
Write-Host "Launching PixInsight OSC pipeline for '$Name'..."
Write-Host "  bias/dark/flat -> calibrate -> cosmetic -> debayer(RGGB) -> PSF-weighted"
Write-Host "  -> StarAlignment(distortion) -> PSF-weighted integration -> masterOSC + review.jpg"
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
