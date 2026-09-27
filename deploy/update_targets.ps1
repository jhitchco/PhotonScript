<#
  update_targets.ps1 - one-shot campaign update on the scope PC:
    1. Cat's Eye -> deep HOO + HDR budget (default 15h, priority 70)
    2. Crescent  -> parked (priority 0, inactive)
    3. PS_DARK_EXPOSURES gains 60 (for the 60s HDR short darks)

  RUN IN POWERSHELL on the scope PC, as the service account, AFTER deploying the
  per-target HDR feature (it needs the new ImagingProject.hdr field in the code).
  The service is stopped first so the projects.json edit is race-free; you
  restart it at the end. Safe to re-run (idempotent).

  Examples:
    .\update_targets.ps1
    .\update_targets.ps1 -CatsEyeHours 12
#>
[CmdletBinding()]
param(
  [string]$Repo        = "C:\astro\PhotonScript",
  [string]$VenvPython  = "C:\astro\venv\Scripts\python.exe",
  [string]$Exe         = "C:\astro\venv\Scripts\photonscript.exe",
  [string]$EnvFile     = "C:\astro\PhotonScript\.env",
  [double]$CatsEyeHours = 15.0
)
$ErrorActionPreference = "Stop"
function Info($m){ Write-Host "[update] $m" -ForegroundColor Cyan }
function Good($m){ Write-Host "[update] $m" -ForegroundColor Green }

# 1. Stop the running service so the projects.json edit is race-free.
Info "Stopping PhotonScript (graceful) before editing projects.json..."
& $Exe stop
Start-Sleep -Seconds 5

# 2. Cat's Eye -> deep HOO+HDR; Crescent -> parked. Single-quoted here-string so
#    PowerShell does NOT interpolate or need escaping. Run from the repo so the
#    app finds .env (pydantic reads '.env' relative to the current directory).
$py = @'
import sys
from photonscript.shared.config import PhotonScriptConfig
from photonscript.scheduler.project_store import ProjectStore, allocate_exposures, target_kind
hours = float(sys.argv[1])
cfg = PhotonScriptConfig()
store = ProjectStore(cfg)
cat = cres = None
for p in store.projects.values():
    n = (p.target.name or '').lower()
    if 'cat' in n and 'eye' in n:
        cat = p
    if 'crescent' in n:
        cres = p
if cres is not None:
    cres.priority = 0
    if hasattr(cres, 'active'):
        cres.active = False
    print('Parked Crescent (priority 0, inactive)')
else:
    print('WARNING: no Crescent project found (nothing to park)')
if cat is not None:
    cat.priority = 70
    cat.budget_hours = hours
    cat.filter_mix = {'Ha': 50, 'OIII': 50, 'SII': 0}
    cat.hdr = {'Ha': 60, 'OIII': 60}
    cat.exposure_plans = allocate_exposures(target_kind(cat.target), hours, cfg,
                                            custom_mix=cat.filter_mix, hdr=cat.hdr)
    cat.total_integration_hours = hours
    tot = sum(e.count * e.exposure_seconds
              + (e.hdr_short_count or 0) * (e.hdr_short_seconds or 0)
              for e in cat.exposure_plans) / 3600.0
    print("Cat's Eye -> {:.1f}h HOO+HDR, priority 70 ({:.2f}h allocated across Ha/OIII)".format(hours, tot))
else:
    print("WARNING: no Cat's Eye project found. Deploy the feature and let the seed create it, or add it in the dashboard, then re-run.")
store.save()
print('Saved', store.path)
'@
Info "Updating projects (Cat's Eye -> ${CatsEyeHours}h, Crescent -> parked)..."
Push-Location $Repo
try {
  $py | & $VenvPython - $CatsEyeHours
  if ($LASTEXITCODE -ne 0) { throw "project update failed (exit $LASTEXITCODE)" }
} finally { Pop-Location }

# 3. Add 60 to PS_DARK_EXPOSURES (60s HDR short darks), idempotent.
$lines = Get-Content $EnvFile
$found = $false
$out = foreach ($l in $lines) {
  if ($l -match '^\s*PS_DARK_EXPOSURES\s*=') {
    $found = $true
    $val = ($l -split '=', 2)[1].Trim().Trim('"')
    $list = @($val -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    if ($list -notcontains '60') { $list += '60' }
    "PS_DARK_EXPOSURES=$($list -join ',')"
  } else { $l }
}
if (-not $found) { $out += 'PS_DARK_EXPOSURES=600,180,300,60' }
Set-Content -Path $EnvFile -Value $out
Good "PS_DARK_EXPOSURES now includes 60"

Good "Done. Restart PhotonScript to pick it up:"
Write-Host "  powershell -ExecutionPolicy Bypass -File $Repo\deploy\run-photonscript.ps1"
