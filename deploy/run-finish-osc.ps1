# Finish an OSC master in PixInsight: crop -> gradient -> plate solve
# -> SPCC with Gaia DR3/SP (fallback BN + ColorCalibration) -> SCNR
# -> deconvolution (BlurXTerminator, else GraXpert, else skipped)
# -> noise reduction (NoiseXTerminator, else GraXpert, else built-in MLT)
# -> star reduction (StarNet2 if installed: starless stretched, stars screened back)
# -> stretch -> saturation -> core HDR -> save.
# Run AFTER run-integration-osc.ps1 (needs out\master\masterOSC.xisf).
#   .\deploy\run-finish-osc.ps1 -Name "M31_OSC2"                 # target guessed from Name
#   .\deploy\run-finish-osc.ps1 -Name "X" -RaDeg 10.685 -DecDeg 41.269
#   .\deploy\run-finish-osc.ps1 -Name "M31_OSC2" -Gradient none  # keep the raw background
#   .\deploy\run-finish-osc.ps1 -Name "M31_OSC4" -Master <xisf> -OutDir <new folder> -Wait
# Output: Staging\<Name>\out\final\<Name>_final.{xisf,tif,jpg} + <Name>_linear.xisf
#         + <Name>_starless.xisf (when StarNet2 ran)
#         + <Name>_final_steps.json (which step ran, tool, settings; tag also in
#         the PSFINISH FITS keyword)
# Log:    Staging\<Name>\out\finish.log (or <OutDir>\finish.log; ends EXIT OK, or an
#         ERROR line then EXIT WITH 1 FAILED MASTER(S))
# finish_osc.js is the one finish implementation: `photonscript integrate`
# renders the same script (photonscript/integration/pjsr.py render_finish).
param(
    [string]$Name = "M31_OSC2",
    [string]$StageRoot = "$env:USERPROFILE\Astrophotography\Staging",
    [string]$PixInsight = "C:\Program Files\PixInsight\bin\PixInsight.exe",
    [string]$Master = "",          # default <stage>\out\master\masterOSC.xisf
    [string]$OutDir = "",          # default <stage>\out\final; an explicit OutDir must be new or empty
    [string]$Target = "",          # catalog name for the solver seed; default = guessed from -Name
    [double]$RaDeg = [double]::NaN,
    [double]$DecDeg = [double]::NaN,
    [double]$FocalMm = 600,        # Piggy-600 (Askar FRA600)
    [double]$PixelUm = 3.76,       # AP26CC / IMX571
    [ValidateSet('auto','abe','none')][string]$Gradient = 'auto',
    # PS-46 color: auto = SPCC when solved and Gaia DR3/SP is configured in
    # PixInsight, else BN + ColorCalibration; spcc = try SPCC anyway; basic = never SPCC.
    [ValidateSet('auto','spcc','basic')][string]$Color = 'auto',
    # SPCC curves by name from PixInsight's library\filters.xspd / white-references.xspd
    [string]$SpccQE = "Sony IMX411/455/461/533/571",
    [string]$SpccRed = "Sony Color Sensor R-UVIRcut",
    [string]$SpccGreen = "Sony Color Sensor G-UVIRcut",
    [string]$SpccBlue = "Sony Color Sensor B-UVIRcut",
    [string]$SpccWhite = "Average Spiral Galaxy",
    # Look (defaults = the M31_OSC4 v4b finish)
    [double]$BgTarget = 0.12,      # stretched background level
    [double]$ShadowSigma = 2.0,    # black point = median - ShadowSigma * MAD
    [double]$Scnr = 0.60,          # SCNR green amount (0 = off)
    [double]$SatMid = 0.64,        # saturation S-curve midpoint (0.5 = off)
    [int]$HdrLayers = 7,           # core HDRMultiscaleTransform layers (0 = off)
    [string]$Frame = "",           # framing crop "left,top,right,bottom" fractions of the master; "" = none
    [switch]$NoRC,                 # don't use BlurXTerminator / NoiseXTerminator even if installed
    # PS-41 noise reduction + deconvolution on the linear image
    [double]$Denoise = 0.5,        # 0..1 strength (NXT denoise / GraXpert strength / MLT amount); 0 = off
    [ValidateSet('on','off')][string]$Deconv = 'on',   # needs BlurXTerminator or GraXpert; no built-in
    [double]$DeconvStrength = 0.5, # GraXpert deconv-obj strength (deconv-stellar gets half)
    [string]$GraXpert = "",        # GraXpert executable; "" = $env:PS_GRAXPERT, then the usual install paths
    [switch]$NoGraXpert,           # never use GraXpert even if found
    [string]$GraXpertAiVersion = "",   # passed as -ai_version; "" = GraXpert's default (latest downloaded)
    [ValidateSet('','true','false')][string]$GraXpertGpu = '',  # passed as -gpu; '' = GraXpert's default
    [int]$GraXpertTimeoutMin = 30, # per GraXpert command
    # PS-40 star reduction (needs the StarNet2 PixInsight module; skipped if absent)
    [ValidateSet('on','off')][string]$StarReduction = 'on',
    [double]$StarStrength = 0.7,   # stars come back at this strength (1 = unchanged)
    # PS-161 OSC HOO extraction: on = also <Name>_hoo.{xisf,jpg} (Ha = R, OIII = mean of G and B)
    [ValidateSet('on','off')][string]$Hoo = 'off',
    [switch]$Wait,                 # run PixInsight unattended (--automation-mode --force-exit), wait, check EXIT OK
    [ValidateSet('Idle','BelowNormal','Normal')][string]$Priority = 'BelowNormal'
)
$inv = [Globalization.CultureInfo]::InvariantCulture
$stage = Join-Path $StageRoot ($Name -replace '[^\w\- ]','_')
if (-not $Master) { $Master = "$stage\out\master\masterOSC.xisf" }
if (-not (Test-Path $Master)) {
    Write-Error "No master at $Master - run run-integration-osc.ps1 first."; exit 1
}
if (-not (Test-Path $PixInsight)) { Write-Error "PixInsight not found at $PixInsight"; exit 1 }
if ($OutDir) {
    # Never overwrite an earlier finish: an explicit output folder must be new or empty.
    if ((Test-Path $OutDir) -and (Get-ChildItem $OutDir -Force | Select-Object -First 1)) {
        Write-Error "OutDir $OutDir already has files - pick a new folder."; exit 1
    }
    New-Item -ItemType Directory -Force $OutDir | Out-Null
    $finalDir = (Resolve-Path $OutDir).Path
    $logDir = $finalDir
} else {
    $finalDir = "$stage\out\final"
    $logDir = "$stage\out"
}
foreach ($pair in @(@('BgTarget', $BgTarget, 0.01, 0.5), @('ShadowSigma', $ShadowSigma, 0, 10),
                    @('Scnr', $Scnr, 0, 1), @('SatMid', $SatMid, 0.3, 0.9),
                    @('Denoise', $Denoise, 0, 1), @('DeconvStrength', $DeconvStrength, 0, 1),
                    @('StarStrength', $StarStrength, 0, 1))) {
    if ($pair[1] -lt $pair[2] -or $pair[1] -gt $pair[3]) {
        Write-Error "-$($pair[0]) $($pair[1]) out of range $($pair[2])..$($pair[3])"; exit 1
    }
}
$frameJs = "null"
if ($Frame) {
    $f = @($Frame -split ',' | ForEach-Object { [double]::Parse($_.Trim(), $inv) })
    if ($f.Count -ne 4) { Write-Error "-Frame needs left,top,right,bottom"; exit 1 }
    $frameJs = "{ left: $($f[0].ToString($inv)), top: $($f[1].ToString($inv)), right: $($f[2].ToString($inv)), bottom: $($f[3].ToString($inv)) }"
}

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
function JsNum($x) { if ([double]::IsNaN($x)) { "NaN" } else { ([double]$x).ToString($inv) } }
function JsStr($s) { ($s -replace '\\','/') -replace '"','' }

# ImageSolver ships with PixInsight under src\scripts\AdP. In library mode
# (USE_SOLVER_LIBRARY) it does NOT pull in its own dependencies or defines, so
# mirror what ImageSolver.js does for itself when run standalone (PI 1.9.3,
# solver 6.3.1): TITLE / SETTINGS_MODULE / STAR_CSV_FILE, then WCSmetadata,
# AstronomicalCatalogs, SearchCoordinatesDialog, CatalogDownloader, ImageSolver.
# finish_osc.js puts this block AFTER its pjsr includes (load-order fix).
$piRoot = Split-Path (Split-Path $PixInsight -Parent) -Parent
$piBin = Split-Path $PixInsight -Parent
$piLibrary = Join-Path $piRoot "library"
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

# What is installed (the script probes again inside PixInsight and logs what ran).
# Gaia DR3/SP: the Gaia module ships with PixInsight, but the database files are a
# separate download that must be selected once in Process > Gaia (wrench icon).
Write-Host "Optional tools:"
Write-Host ("  Gaia module:        " + $(if (Test-Path "$piBin\Gaia-pxm.dll") { "present (database selection is checked in PixInsight)" } else { "MISSING" }))

# GraXpert (free, https://graxpert.com): an external program, called through
# its command line from inside the PixInsight script.
$gxExe = ""; $gxVer = ""
if (-not $NoGraXpert) {
    $gxCands = @($GraXpert, $env:PS_GRAXPERT,
                 "$env:ProgramFiles\GraXpert\GraXpert-win64.exe", "$env:ProgramFiles\GraXpert\GraXpert.exe",
                 "$env:LOCALAPPDATA\Programs\GraXpert\GraXpert.exe", "$env:LOCALAPPDATA\Programs\GraXpert\GraXpert-win64.exe",
                 "$env:USERPROFILE\GraXpert\GraXpert-win64.exe", "$env:USERPROFILE\GraXpert\GraXpert.exe")
    foreach ($c in $gxCands) { if ($c -and (Test-Path $c -PathType Leaf)) { $gxExe = (Resolve-Path $c).Path; break } }
    if ($GraXpert -and -not $gxExe) { Write-Warning "-GraXpert $GraXpert not found - falling back to the built-in noise reduction." }
    if ($gxExe) {
        try { $gxVer = (Get-Item $gxExe).VersionInfo.ProductVersion } catch {}
        if (-not $gxVer) { $gxVer = "unknown" }
    }
}
Write-Host ("  StarNet2 module:    " + $(if (Get-ChildItem $piBin -Filter "StarNet2*.dll" -ErrorAction SilentlyContinue) { "present" } else { "not found (star reduction skipped)" }))
Write-Host ("  GraXpert:           " + $(if ($gxExe) { "$gxExe ($gxVer)" } elseif ($NoGraXpert) { "disabled (-NoGraXpert)" } else { "not found (built-in noise reduction, no deconvolution)" }))

$js = [System.IO.File]::ReadAllText((Join-Path $PSScriptRoot "finish_osc.js"))
$js = $js.Replace('//__SOLVER_INCLUDE__', $include)
$js = $js.Replace('__STAGING__', (JsStr $stage))
$js = $js.Replace('__NAME__', ($Name -replace '[^\w\-]','_'))
$js = $js.Replace('__MASTER__', (JsStr $Master)).Replace('__FINAL__', (JsStr $finalDir))
$js = $js.Replace('__LOGDIR__', (JsStr $logDir)).Replace('__PI_LIBRARY__', (JsStr $piLibrary))
$js = $js.Replace('__RA__', (JsNum $RaDeg)).Replace('__DEC__', (JsNum $DecDeg))
$js = $js.Replace('__FOCAL__', (JsNum $FocalMm)).Replace('__PIXEL__', (JsNum $PixelUm))
$js = $js.Replace('__GRADIENT__', $Gradient)
$js = $js.Replace('__USE_RC__', $(if ($NoRC) { 'false' } else { 'true' }))
$js = $js.Replace('__COLOR__', $Color)
$js = $js.Replace('__SPCC_QE__', (JsStr $SpccQE)).Replace('__SPCC_RED__', (JsStr $SpccRed))
$js = $js.Replace('__SPCC_GREEN__', (JsStr $SpccGreen)).Replace('__SPCC_BLUE__', (JsStr $SpccBlue))
$js = $js.Replace('__SPCC_WHITE__', (JsStr $SpccWhite))
$js = $js.Replace('__BG_TARGET__', (JsNum $BgTarget)).Replace('__SHADOW_SIGMA__', (JsNum $ShadowSigma))
$js = $js.Replace('__SCNR__', (JsNum $Scnr)).Replace('__SAT_MID__', (JsNum $SatMid))
$js = $js.Replace('__HDR_LAYERS__', [string]$HdrLayers).Replace('__FRAME__', $frameJs)
$js = $js.Replace('__DENOISE__', (JsNum $Denoise)).Replace('__DECONV__', $Deconv)
$js = $js.Replace('__DECONV_STRENGTH__', (JsNum $DeconvStrength))
$js = $js.Replace('__GRAXPERT__', (JsStr $gxExe)).Replace('__GRAXPERT_VERSION__', (JsStr $gxVer))
$js = $js.Replace('__GRAXPERT_AI__', (JsStr $GraXpertAiVersion)).Replace('__GRAXPERT_GPU__', $GraXpertGpu)
$js = $js.Replace('__GRAXPERT_TIMEOUT_MIN__', [string]$GraXpertTimeoutMin)
$js = $js.Replace('__STARS__', $StarReduction).Replace('__STAR_STRENGTH__', (JsNum $StarStrength))
$js = $js.Replace('__HOO__', $Hoo)
$js = $js.Replace('__MASTERS__', 'null')   # one master; photonscript integrate passes a list
$runjs = Join-Path $(if ($OutDir) { $finalDir } else { $stage }) "finish_osc_run.js"
# BOM-less write: PowerShell's UTF8 adds a BOM that breaks PixInsight's parser
[System.IO.File]::WriteAllText($runjs, $js)

Write-Host "Launching PixInsight OSC finish for '$Name'..."
Write-Host "  crop -> gradient($Gradient) -> plate solve -> color($Color) -> deconv($Deconv) -> denoise($Denoise)$(if ($NoRC) {' (no RC tools)'}) -> stars($StarReduction, $StarStrength) -> stretch -> save"
Write-Host "Script: $runjs"
$logFile = Join-Path $logDir "finish.log"
if ($Wait) {
    if (Get-Process PixInsight -ErrorAction SilentlyContinue) {
        Write-Error "PixInsight is already running - close it (or run without -Wait)."; exit 2
    }
    $proc = Start-Process -FilePath $PixInsight -ArgumentList @("-n", "--automation-mode", "--run=$runjs", "--force-exit") -PassThru
    Start-Sleep -Seconds 4
    try { $proc.PriorityClass = [System.Diagnostics.ProcessPriorityClass]::$Priority } catch {}
    $proc.WaitForExit()
    $tail = Get-Content $logFile -ErrorAction SilentlyContinue
    if ($tail -match "EXIT OK") { Write-Host "Finish OK. Log: $logFile   Output: $finalDir"; exit 0 }
    Write-Error "Finish did not log EXIT OK - see $logFile"; exit 1
}
$proc = Start-Process -FilePath $PixInsight -ArgumentList @("-n", "-r=$runjs", "--run=$runjs") -PassThru
Start-Sleep -Seconds 4
try { $proc.PriorityClass = [System.Diagnostics.ProcessPriorityClass]::$Priority } catch {}
Write-Host ""
Write-Host "Watch: $logFile   Output: $finalDir"
Write-Host "If no [FINISH] lines appear within ~15s: SCRIPT menu > Execute Script File... > $runjs"
