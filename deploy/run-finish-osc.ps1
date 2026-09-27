# Finish an OSC master in PixInsight: crop -> gradient -> plate solve -> SPCC
# -> [BlurX/NoiseXTerminator if installed] -> stretch -> saturation -> save.
# Run AFTER run-integration-osc.ps1 (needs out\master\masterOSC.xisf).
#   .\deploy\run-finish-osc.ps1 -Name "M31_OSC2"                 # target guessed from Name
#   .\deploy\run-finish-osc.ps1 -Name "X" -RaDeg 10.685 -DecDeg 41.269
#   .\deploy\run-finish-osc.ps1 -Name "M31_OSC2" -Gradient none  # keep the raw background
# Output: Staging\<Name>\out\final\<Name>_final.{xisf,tif,jpg} + <Name>_linear.xisf
# Log:    Staging\<Name>\out\finish.log  (ends EXIT OK or ERROR: ...)
param(
    [string]$Name = "M31_OSC2",
    [string]$StageRoot = "$env:USERPROFILE\Astrophotography\Staging",
    [string]$PixInsight = "C:\Program Files\PixInsight\bin\PixInsight.exe",
    [string]$Target = "",          # catalog name for the solver seed; default = guessed from -Name
    [double]$RaDeg = [double]::NaN,
    [double]$DecDeg = [double]::NaN,
    [double]$FocalMm = 600,        # Piggy-600 (Askar FRA600)
    [double]$PixelUm = 3.76,       # AP26CC / IMX571
    [ValidateSet('auto','abe','none')][string]$Gradient = 'auto',
    [switch]$NoRC,                 # don't use BlurXTerminator / NoiseXTerminator even if installed
    [ValidateSet('Idle','BelowNormal','Normal')][string]$Priority = 'BelowNormal'
)
$stage = Join-Path $StageRoot ($Name -replace '[^\w\- ]','_')
if (-not (Test-Path "$stage\out\master\masterOSC.xisf")) {
    Write-Error "No master at $stage\out\master\masterOSC.xisf - run run-integration-osc.ps1 first."; exit 1
}
if (-not (Test-Path $PixInsight)) { Write-Error "PixInsight not found at $PixInsight"; exit 1 }

# Solver seed. Piggy-600 frames rarely center on the named target, so the
# script spirals outward from this point (up to ~1.2 deg) until a solve lands.
$catalog = @{
    "M31" = @(10.6847, 41.2692);  "M33" = @(23.4621, 30.6599);  "M42" = @(83.8221, -5.3911)
    "M45" = @(56.8500, 24.1167);  "M51" = @(202.4696, 47.1952); "M81" = @(148.8882, 69.0653)
    "M101" = @(210.8025, 54.3490); "M13" = @(250.4235, 36.4613); "M8" = @(270.9042, -24.3867)
    "M16" = @(274.7000, -13.8067); "M20" = @(270.6208, -23.0300); "M27" = @(299.9016, 22.7212)
    "NGC7000" = @(314.7500, 44.3333); "IC1396" = @(324.7250, 57.5000); "NGC6960" = @(311.4250, 30.7167)
    "IC1805" = @(38.1750, 61.4500); "IC1848" = @(42.8000, 60.4333); "NGC2237" = @(97.9800, 5.0333)
}
if ([double]::IsNaN($RaDeg) -or [double]::IsNaN($DecDeg)) {
    $t = $Target
    if (-not $t -and $Name -match '^(M\d+|NGC\d+|IC\d+)') { $t = $matches[1] }
    $t = ($t -replace '\s','').ToUpper()
    if ($t -and $catalog.ContainsKey($t)) {
        $RaDeg = $catalog[$t][0]; $DecDeg = $catalog[$t][1]
        Write-Host "Solver seed: $t (RA $RaDeg, Dec $DecDeg)"
    } else {
        Write-Warning "No coordinates for '$t' - pass -RaDeg/-DecDeg. Plate solve and SPCC will be skipped (BN + ColorCalibration fallback)."
    }
}
function JsNum($x) { if ([double]::IsNaN($x)) { "NaN" } else { $x.ToString([Globalization.CultureInfo]::InvariantCulture) } }

# ImageSolver ships with PixInsight under src\scripts\AdP. In library mode
# (USE_SOLVER_LIBRARY) it does NOT pull in its own dependencies or defines, so
# mirror what ImageSolver.js does for itself when run standalone (PI 1.9.3,
# solver 6.3.1): TITLE / SETTINGS_MODULE / STAR_CSV_FILE, then WCSmetadata,
# AstronomicalCatalogs, SearchCoordinatesDialog, CatalogDownloader, ImageSolver.
$piRoot = Split-Path (Split-Path $PixInsight -Parent) -Parent
$adp = Join-Path $piRoot "src\scripts\AdP"
$deps = @("WCSmetadata.jsh", "AstronomicalCatalogs.jsh", "SearchCoordinatesDialog.js",
          "CatalogDownloader.js", "ImageSolver.js")
$missing = @($deps | Where-Object { -not (Test-Path (Join-Path $adp $_)) })
$include = ""
if ($missing.Count -eq 0) {
    $adpFwd = $adp -replace '\\','/'
    $lines = @(
        '#define USE_SOLVER_LIBRARY true',
        '#define TITLE "PhotonScript Finish"',
        '#define SETTINGS_MODULE "SOLVER"',
        '#define STAR_CSV_FILE (File.systemTempDirectory + format( "/stars-%03d.csv", CoreApplication.instance ))'
    )
    foreach ($d in $deps) { $lines += "#include `"$adpFwd/$d`"" }
    $include = $lines -join "`n"
} else { Write-Warning "ImageSolver files missing in $adp ($($missing -join ', ')) - plate solve/SPCC will be skipped." }

$js = [System.IO.File]::ReadAllText((Join-Path $PSScriptRoot "finish_osc.js"))
$js = $js.Replace('//__SOLVER_INCLUDE__', $include)
$js = $js.Replace('__STAGING__', ($stage -replace '\\','/'))
$js = $js.Replace('__NAME__', ($Name -replace '[^\w\-]','_'))
$js = $js.Replace('__RA__', (JsNum $RaDeg)).Replace('__DEC__', (JsNum $DecDeg))
$js = $js.Replace('__FOCAL__', (JsNum $FocalMm)).Replace('__PIXEL__', (JsNum $PixelUm))
$js = $js.Replace('__GRADIENT__', $Gradient)
$js = $js.Replace('__USE_RC__', $(if ($NoRC) { 'false' } else { 'true' }))
$runjs = Join-Path $stage "finish_osc_run.js"
# BOM-less write: PowerShell's UTF8 adds a BOM that breaks PixInsight's parser
[System.IO.File]::WriteAllText($runjs, $js)

Write-Host "Launching PixInsight OSC finish for '$Name'..."
Write-Host "  crop -> gradient($Gradient) -> plate solve -> SPCC -> $(if ($NoRC) {'(no RC tools)'} else {'[BXT/NXT if installed]'}) -> stretch -> save"
Write-Host "Script: $runjs"
$proc = Start-Process -FilePath $PixInsight -ArgumentList @("-n", "-r=$runjs", "--run=$runjs") -PassThru
Start-Sleep -Seconds 4
try { $proc.PriorityClass = [System.Diagnostics.ProcessPriorityClass]::$Priority } catch {}
Write-Host ""
Write-Host "Watch: $stage\out\finish.log   Output: $stage\out\final\"
Write-Host "If no [FINISH] lines appear within ~15s: SCRIPT menu > Execute Script File... > $runjs"
