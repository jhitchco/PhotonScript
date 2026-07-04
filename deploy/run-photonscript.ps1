# PhotonScript wrapper for the SCOPE PC.
# Start PhotonScript with this instead of calling it directly:
#   powershell -ExecutionPolicy Bypass -File C:\astro\PhotonScript\deploy\run-photonscript.ps1
#
# When PhotonScript exits with code 42 (the "Pull latest & restart" button or
# POST /api/update), this pulls the latest code and starts it again in the
# same console, so the log stream continues where it left off.

$repo = "C:\astro\PhotonScript"
$exe  = "C:\astro\venv\Scripts\photonscript.exe"

while ($true) {
    git -C $repo pull --ff-only
    & $exe start --mode full
    if ($LASTEXITCODE -ne 42) {
        Write-Host "`nPhotonScript exited with code $LASTEXITCODE - not an update request, staying down." 
        break
    }
    Write-Host "`n=== Update requested - pulling latest and restarting ===`n"
}
