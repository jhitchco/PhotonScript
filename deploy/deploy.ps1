# One-command deploy from the DESKTOP:
#   .\deploy\deploy.ps1 "what I changed"
# Commits everything, pushes, then tells the scope PC to pull + restart.
# Watch the version stamp in the web UI flip to the new hash (~20 s).

param(
    [string]$Message = "update",
    [string]$Scope = "http://100.94.189.77:8100"
)
$repo = Split-Path $PSScriptRoot -Parent
git -C $repo add -A
git -C $repo commit -m $Message
if ($LASTEXITCODE -ne 0) { Write-Host "Nothing to commit - pushing/updating anyway." }
git -C $repo push
if ($LASTEXITCODE -ne 0) { Write-Error "Push failed - not restarting the scope."; exit 1 }
try {
    Invoke-RestMethod -Method Post "$Scope/api/update" | Out-Null
    Write-Host "Pushed. Scope PC is pulling and restarting - check the nav version stamp."
} catch {
    Write-Warning "Could not reach the scope PC at $Scope - update it manually."
}
