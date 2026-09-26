# One-command deploy from the DESKTOP:
#   .\deploy\deploy.ps1 "what I changed"
#   .\deploy\deploy.ps1 "hotfix" -SkipTests    # emergency bypass of the gate
# Runs the test gate, commits everything, pushes, then tells the scope PC to
# pull + restart. Watch the version stamp in the web UI flip (~20 s).

param(
    [string]$Message = "update",
    [string]$Scope = "http://100.94.189.77:8100",
    [switch]$SkipTests
)
$repo = Split-Path $PSScriptRoot -Parent

# --- Pre-deploy gate: never ship a change that fails tests. ------------------
# Best-effort: only gates when the test tooling is actually installed here (the
# desktop deploy box may lack the dev deps). If pytest is missing, it WARNS and
# continues rather than bricking the deploy - it only hard-blocks on a real test
# FAILURE. Install the tooling to make the gate real:
#   python -m pip install pytest pytest-asyncio ruff scipy
if (-not $SkipTests) {
    Write-Host "Pre-deploy checks (pytest, ruff)...  (-SkipTests to bypass)"
    Push-Location $repo
    try {
        python -m pytest --version *> $null
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "pytest not installed here - SKIPPING the test gate. To enable it: python -m pip install pytest pytest-asyncio ruff scipy  (or set up CI)."
        } else {
            python -m pytest -q
            if ($LASTEXITCODE -ne 0) {
                Write-Error "pytest FAILED - not deploying. Fix the tests, or rerun with -SkipTests."
                Pop-Location; exit 1
            }
            python -m ruff --version *> $null
            if ($LASTEXITCODE -eq 0) {
                python -m ruff check photonscript 2>&1 | Out-Host
                if ($LASTEXITCODE -ne 0) {
                    Write-Warning "ruff reported lint issues (not blocking) - consider cleaning them up."
                }
            }
            Write-Host "Gate passed."
        }
    } finally { Pop-Location }
}

git -C $repo add -A
git -C $repo commit -m $Message
if ($LASTEXITCODE -ne 0) { Write-Host "Nothing to commit - pushing/updating anyway." }
git -C $repo pull --rebase --autostash origin main
if ($LASTEXITCODE -ne 0) { Write-Error "Pull/rebase hit a conflict - resolve it, then rerun."; exit 1 }
git -C $repo push --set-upstream origin HEAD
if ($LASTEXITCODE -ne 0) { Write-Error "Push failed - not restarting the scope."; exit 1 }
try {
    Invoke-RestMethod -Method Post "$Scope/api/update" | Out-Null
    Write-Host "Pushed. Scope PC is pulling and restarting - check the nav version stamp."
} catch {
    $status = 0
    try { $status = [int]$_.Exception.Response.StatusCode } catch {}
    if ($status -eq 409) {
        Write-Warning "Pushed, but the scope REFUSED to restart: a night is armed/active."
        Write-Warning "It will pick the code up on the next restart - or Disarm, rerun deploy, re-Arm."
    } else {
        Write-Warning "Could not reach the scope PC at $Scope - update it manually."
    }
}
