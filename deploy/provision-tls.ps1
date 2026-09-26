<#
  provision-tls.ps1  — idempotent scope-PC setup for PhotonScript direct HTTPS
  (uvicorn TLS on :8443), replacing the flaky `tailscale serve` 443 proxy.

  RUN ON: the scope PC (teles-feb25), AS THE ACCOUNT THE PHOTONSCRIPT SERVICE
  RUNS AS (so the cert lands in that account's %USERPROFILE%\.photonscript\certs,
  which is where the app reads it). Elevated if the cert step 401s.

  Safe to re-run: every step checks state first and only changes what's missing.

  Examples:
    .\provision-tls.ps1                       # cert + firewall + .env (leaves 443 alone)
    .\provision-tls.ps1 -RetireServe          # ...and turn `tailscale serve --https=443 off`
    .\provision-tls.ps1 -EnvFile C:\astro\PhotonScript\.env
#>
[CmdletBinding()]
param(
  [string]$Hostname = "teles-feb25.lobster-bleak.ts.net",
  [int]$TlsPort     = 8443,
  [string]$EnvFile  = "C:\astro\PhotonScript\.env",
  [string]$CertDir  = "$env:USERPROFILE\.photonscript\certs",
  [switch]$RetireServe
)

$ErrorActionPreference = "Stop"
function Info($m){ Write-Host "[provision] $m" -ForegroundColor Cyan }
function Warn($m){ Write-Host "[provision] $m" -ForegroundColor Yellow }
function Good($m){ Write-Host "[provision] $m" -ForegroundColor Green }

# 1. tailscale present
$ts = Get-Command tailscale -ErrorAction SilentlyContinue
if (-not $ts) { throw "tailscale CLI not found on PATH" }
Info "tailscale: $($ts.Source)"

# 2. Cert mint/refresh (idempotent; tailscale only re-fetches near expiry).
#    The app also mints at startup — this validates identity BEFORE you deploy.
New-Item -ItemType Directory -Force -Path $CertDir | Out-Null
$crt = Join-Path $CertDir "$Hostname.crt"
$key = Join-Path $CertDir "$Hostname.key"
Info "Minting/refreshing cert for $Hostname -> $CertDir"
Push-Location $CertDir   # mint from a writable dir (system32 gives Access denied)
try {
  & tailscale cert --cert-file $crt --key-file $key $Hostname
  if ($LASTEXITCODE -ne 0) {
    throw "tailscale cert failed (exit $LASTEXITCODE) — run elevated, or as the tailscaled owner (teles-feb25\sleep)"
  }
} finally { Pop-Location }
if (-not (Test-Path $crt) -or -not (Test-Path $key)) { throw "cert/key not written to $CertDir" }
Good "cert present: $crt"

# 3. Firewall rule for the TLS port (idempotent), scoped to the Tailscale NIC.
$ruleName = "PhotonScript HTTPS $TlsPort"
if (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue) {
  Info "firewall rule '$ruleName' already exists"
} else {
  $ifAlias = (Get-NetAdapter |
    Where-Object { $_.InterfaceDescription -match 'Tailscale' -or $_.Name -match 'Tailscale' } |
    Select-Object -First 1).Name
  if ($ifAlias) {
    New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Protocol TCP `
      -LocalPort $TlsPort -Action Allow -InterfaceAlias $ifAlias | Out-Null
    Good "firewall rule added on interface '$ifAlias'"
  } else {
    New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Protocol TCP `
      -LocalPort $TlsPort -Action Allow | Out-Null
    Warn "Tailscale interface not found by name; rule added on ALL interfaces"
  }
}

# 4. Ensure .env carries the TLS flags (idempotent — append only if missing).
if (-not (Test-Path $EnvFile)) { throw ".env not found at $EnvFile" }
$envtext = Get-Content $EnvFile -Raw
function Ensure-EnvLine([string]$k, [string]$v) {
  if ($script:envtext -match "(?m)^\s*$([regex]::Escape($k))=") {
    Info "$k already set in .env"
  } else {
    Add-Content -Path $EnvFile -Value "$k=$v"
    Good "appended $k to .env"
    $script:envtext = Get-Content $EnvFile -Raw
  }
}
Ensure-EnvLine "PS_SCHEDULER_TLS_ENABLED"  "true"
Ensure-EnvLine "PS_SCHEDULER_TLS_HOSTNAME" $Hostname

# 5. Optionally retire the old 443 serve proxy (do this only AFTER :8443 verifies).
if ($RetireServe) {
  Info "retiring tailscale serve on :443"
  & tailscale serve --https=443 off
  & tailscale serve status
}

Good "Provisioning complete."
Write-Host ""
Write-Host "NEXT:" -ForegroundColor White
Write-Host "  1. Deploy the new code:  .\deploy\deploy.ps1 `"uvicorn TLS + watchdog`""
Write-Host "  2. In the service log, look for: 'Scheduler HTTPS on :$TlsPort (remote); HTTP on :8100 (internal)'"
Write-Host "  3. Verify from a tailnet node: https://$Hostname`:$TlsPort/api/runs  (and hit it a few times fast)"
Write-Host "  4. If not already done: repoint HANDBOOK + debrief SKILL to :$TlsPort"
