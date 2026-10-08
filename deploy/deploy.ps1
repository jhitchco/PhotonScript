# One-command deploy from the DESKTOP. Ships only what is COMMITTED (PS-56):
#   git add <your files>; git commit -m "what I changed"
#   .\deploy\deploy.ps1
#   .\deploy\deploy.ps1 -IncludeWorkingTree "msg"   # commit EVERYTHING in the
#                                                    # tree (shows it, asks first)
#   .\deploy\deploy.ps1 -SkipTests                   # emergency bypass of the gate
#
# Why: this checkout also holds other sessions' unfinished work. The old
# `git add -A` swept 24 files / ~1,700 lines of it into one deploy (6ec7149,
# 2026-09-26). Now a dirty tree is refused and listed instead.
#
# Order: on main + clean tree, rebase onto origin/main, test gate on exactly
# what ships, show the outgoing commits, push, POST /api/update, then wait for
# GET /api/health on the scope to report the pushed SHA (PS-57).
#
# PS-58: the scope stages and smoke-checks the new commit before switching,
# and rolls back to the previous SHA if /api/health does not come up on the
# new one. This script reads that outcome from /api/health ("update") and
# says so instead of just timing out. The scope refuses (409) while a night
# runs, while ARMED (-AllowArmed overrides: the armed night is restored) and
# while a grading job is writing.

param(
    [string]$Message = "",
    # Tailscale MagicDNS hostname on the DIRECT app port (:8100), NOT the serve
    # proxy on 443. This gets both: a stable name that follows the node when the
    # tailnet IP changes, AND a direct hit on uvicorn without the flaky serve
    # proxy in the middle (which returns 502 whenever the backend blips).
    [string]$Scope = "http://teles-feb25.lobster-bleak.ts.net:8100",
    [switch]$SkipTests,
    [switch]$IncludeWorkingTree,
    [switch]$AllowArmed,
    # graceful stop + staging import check + start + health verify
    [int]$VerifySeconds = 240
)
$repo = Split-Path $PSScriptRoot -Parent

# --- 1. On main, with a clean tree --------------------------------------------
$branch = "$(git -C $repo rev-parse --abbrev-ref HEAD)".Trim()
if ($branch -ne "main") {
    Write-Host "Refusing to deploy: on branch '$branch', not main (the scope PC pulls main)." -ForegroundColor Red
    Write-Host "Merge first:  git switch main; git merge $branch   then rerun."
    exit 1
}

$dirty = @(git -C $repo status --porcelain)
if ($dirty.Count -gt 0) {
    if (-not $IncludeWorkingTree) {
        Write-Host "Refusing to deploy: the working tree has $($dirty.Count) uncommitted change(s):" -ForegroundColor Red
        $dirty | ForEach-Object { Write-Host "  $_" }
        Write-Host ""
        Write-Host "deploy.ps1 ships only committed work. Either:"
        Write-Host "  git add <the files you mean to ship>; git commit -m `"what changed`"   then rerun"
        Write-Host "  git stash push -u -m `"not mine`"      to set aside work that is not part of this deploy"
        Write-Host "  .\deploy\deploy.ps1 -IncludeWorkingTree `"msg`"      to commit ALL of it (asks first)"
        exit 1
    }
    $unmerged = @($dirty | Where-Object { $_ -match '^(DD|AU|UD|UA|DU|AA|UU) ' })
    if ($unmerged.Count -gt 0) {
        Write-Host "Refusing: unresolved merge conflicts in the tree:" -ForegroundColor Red
        $unmerged | ForEach-Object { Write-Host "  $_" }
        exit 1
    }
    if (-not $Message) {
        Write-Host "-IncludeWorkingTree needs a commit message:  .\deploy\deploy.ps1 -IncludeWorkingTree `"what changed`"" -ForegroundColor Red
        exit 1
    }
    git -C $repo status --short | Out-Host
    $answer = Read-Host "Commit ALL $($dirty.Count) change(s) above as `"$Message`" and deploy them? Type yes"
    if ($answer -ne "yes") { Write-Host "Not deploying."; exit 1 }
    git -C $repo add -A
    git -C $repo commit -m $Message
    if ($LASTEXITCODE -ne 0) { Write-Host "git commit failed - not deploying." -ForegroundColor Red; exit 1 }
} elseif ($Message) {
    Write-Host "(The tree is clean, so `"$Message`" is not used: deploying the commits already made.)"
}

# --- 2. Rebase onto origin/main (tree is clean: no autostash needed) ---------
# --rebase=merges keeps merged ticket branches intact. A plain --rebase
# flattens them and replays every branch commit, which conflicts.
git -C $repo pull --rebase=merges origin main
if ($LASTEXITCODE -ne 0) {
    Write-Host "Pull/rebase hit a conflict - resolve it (or git rebase --abort), then rerun." -ForegroundColor Red
    exit 1
}

# --- 3. Pre-deploy gate on exactly what ships ---------------------------------
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
                Write-Host "pytest FAILED - not deploying. Fix the tests, or rerun with -SkipTests." -ForegroundColor Red
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

# --- 4. Show what goes out, then push -------------------------------------------
$head = "$(git -C $repo rev-parse HEAD)".Trim()
$short = $head.Substring(0, [Math]::Min(7, $head.Length))
$outgoing = @(git -C $repo log --oneline origin/main..HEAD)
if ($outgoing.Count -eq 0) {
    Write-Host "Nothing new to push (HEAD $short = origin/main); asking the scope to pull anyway."
} else {
    Write-Host "Deploying $($outgoing.Count) commit(s), origin/main..HEAD:"
    $outgoing | ForEach-Object { Write-Host "  $_" }
    git -C $repo push origin HEAD:main
    if ($LASTEXITCODE -ne 0) { Write-Host "Push failed - not restarting the scope." -ForegroundColor Red; exit 1 }
}

# --- 5. Ask the scope to pull + restart ---------------------------------------
$posted = $false
$updateUrl = "$Scope/api/update"
if ($AllowArmed) { $updateUrl = "$updateUrl`?allow_armed=true" }
try {
    Invoke-RestMethod -Method Post $updateUrl | Out-Null
    $posted = $true
    Write-Host "Scope PC is staging the new code and restarting."
} catch {
    $status = 0
    try { $status = [int]$_.Exception.Response.StatusCode } catch {}
    if ($status -eq 409) {
        $detail = ""
        try { $detail = ($_.ErrorDetails.Message | ConvertFrom-Json).detail } catch {}
        Write-Warning "Pushed, but the scope REFUSED to restart: $detail"
        Write-Warning "It will pick the code up on the next restart - or rerun deploy when that clears (-AllowArmed if it is only ARMED)."
    } else {
        Write-Warning "Could not reach the scope PC at $Scope - update it manually."
    }
}

# --- 6. Verify: the scope reports the SHA we pushed (GET /api/health) --------
if ($posted) {
    Write-Host "Waiting up to $VerifySeconds s for the scope to report $short ..."
    $deadline = (Get-Date).AddSeconds($VerifySeconds)
    $ok = $false; $noHealth = $false; $h = $null; $outcome = ""
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 5
        try {
            $h = Invoke-RestMethod -TimeoutSec 10 "$Scope/api/health"
        } catch {
            $code = 0
            try { $code = [int]$_.Exception.Response.StatusCode } catch {}
            if ($code -eq 404) { $noHealth = $true }
            continue
        }
        if ($h.commit -eq $head) { $ok = $true; break }
        # PS-58: the scope kept or restored the old code for this SHA
        $u = $h.update
        if ($u -and $u.status -eq "rolled_back" -and $u.bad_sha -eq $head) { $outcome = "rolled_back"; break }
        if ($u -and $u.status -eq "rejected" -and $u.target -eq $head) { $outcome = "rejected"; break }
    }
    if ($ok) {
        Write-Host ("Scope is running {0}: pid {1}, up {2} s, loop lag {3} ms, armer {4}." -f $short, $h.pid, $h.uptime_s, $h.loop.lag_ms, $h.armer) -ForegroundColor Green
    } elseif ($outcome -eq "rolled_back") {
        Write-Host ("Scope ROLLED BACK {0}: it did not come up healthy ({1}). Running {2} again." -f $short, $h.update.reason, $h.update.target) -ForegroundColor Red
        Write-Host "Push a fix and deploy again (the scope skips $short until something newer is pushed). Details: supervisor.log / wrapper.log on the scope."
        exit 1
    } elseif ($outcome -eq "rejected") {
        Write-Host ("Scope REFUSED {0} before switching: {1}. Still running the previous code." -f $short, $h.update.reason) -ForegroundColor Red
        exit 1
    } elseif ($noHealth) {
        Write-Warning "The scope answers without /api/health (older code still running?). Check the nav version stamp."
    } else {
        Write-Warning "The scope did not report $short within $VerifySeconds s. Check 'photonscript status' and wrapper.log on the scope PC."
    }
}
