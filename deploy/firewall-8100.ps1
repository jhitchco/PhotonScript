# PS-60: keep the PhotonScript API (:8100) off the observatory LAN.
#
# The scheduler binds 0.0.0.0:8100 with no auth on its write routes
# (POST /api/config, /api/update, /api/arm). This script adds an inbound
# Windows Firewall rule pair on the SCOPE PC for TCP 8100:
#
#   "PhotonScript 8100 allow tailnet"  Allow from 127.0.0.0/8 and the
#                                      Tailscale ranges (100.64.0.0/10,
#                                      fd7a:115c:a1e0::/48)
#   "PhotonScript 8100 block LAN"      Block from every other address
#
# Windows Firewall evaluates Block before Allow, so the block rule lists the
# complement of the allowed ranges instead of "Any"; a python.exe allow rule
# from the first-run prompt then cannot reopen :8100 to the LAN.
#
# What keeps working: the dashboard over Tailscale (desktop 100.x, and
# tailscale serve, which proxies from 127.0.0.1), deploy.ps1 (direct to
# 100.94.189.77:8100 or localhost), photonscript remote / status from the
# desktop. Outbound traffic is not touched: the scope PC still reaches the
# AARO power switch (192.168.23.50), NINA, PHD2, TheSky and Syncthing. Only
# LAN hosts opening :8100 on this PC are refused.
#
# Default is a dry run: prints the current rules and what it would do.
#   powershell -ExecutionPolicy Bypass -File C:\astro\PhotonScript\deploy\firewall-8100.ps1
#   ... -Apply      create or update the two rules (elevated PowerShell)
#   ... -Remove     delete both rules again (elevated PowerShell; add -Apply
#                   is not needed for -Remove)

param(
    [int]$Port = 8100,
    [string]$Prefix = "PhotonScript $Port",
    [switch]$Apply,
    [switch]$Remove
)

$ErrorActionPreference = "Stop"

# Allowed sources: loopback plus the Tailscale CGNAT and ULA ranges.
$AllowRemote = @(
    "127.0.0.0/8",
    "::1",
    "100.64.0.0/10",
    "fd7a:115c:a1e0::/48"
)

# Everything else, as ranges (the exact complement of $AllowRemote, checked
# by tests/test_deploy_scripts.py). "::" (unspecified) is never a source.
$BlockRemote = @(
    "0.0.0.0-100.63.255.255",
    "100.128.0.0-126.255.255.255",
    "128.0.0.0-255.255.255.255",
    "::2-fd7a:115c:a1df:ffff:ffff:ffff:ffff:ffff",
    "fd7a:115c:a1e1::-ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff"
)

$AllowName = "$Prefix allow tailnet"
$BlockName = "$Prefix block LAN"

function Show-Rules {
    Write-Host "Current inbound rules for TCP $Port"
    $found = $false
    # Port filters first (one query), then only the rules that name the port.
    $filters = Get-NetFirewallPortFilter -Protocol TCP -ErrorAction SilentlyContinue |
        Where-Object { @($_.LocalPort) -contains "$Port" }
    foreach ($pf in $filters) {
        $r = $pf | Get-NetFirewallRule
        if ($r.Direction -ne "Inbound") { continue }
        $af = $r | Get-NetFirewallAddressFilter
        $found = $true
        Write-Host ("  {0,-34} {1,-5} enabled={2,-5} remote={3}" -f $r.DisplayName, $r.Action, $r.Enabled, ($af.RemoteAddress -join ","))
    }
    if (-not $found) { Write-Host "  (none that name port $Port; program-wide python.exe rules may still allow it)" }
}

$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

Show-Rules
Write-Host ""

if ($Remove) {
    if (-not $isAdmin) { Write-Host "Run -Remove from an elevated (Administrator) PowerShell." -ForegroundColor Red; exit 1 }
    foreach ($name in @($AllowName, $BlockName)) {
        if (Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue) {
            Remove-NetFirewallRule -DisplayName $name
            Write-Host "Removed '$name'."
        } else {
            Write-Host "No rule named '$name'."
        }
    }
    exit 0
}

Write-Host "Plan (TCP $Port inbound, all profiles):"
Write-Host "  Allow '$AllowName' from $($AllowRemote -join ', ')"
Write-Host "  Block '$BlockName' from $($BlockRemote -join ', ')"

if (-not $Apply) {
    Write-Host ""
    Write-Host "Dry run: nothing changed. Add -Apply (elevated) to create the rules, -Remove to undo." -ForegroundColor Yellow
    exit 0
}
if (-not $isAdmin) { Write-Host "Run -Apply from an elevated (Administrator) PowerShell." -ForegroundColor Red; exit 1 }

# Idempotent: replace any earlier copy of either rule.
foreach ($name in @($AllowName, $BlockName)) {
    if (Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue) {
        Remove-NetFirewallRule -DisplayName $name
    }
}
New-NetFirewallRule -DisplayName $AllowName -Direction Inbound -Protocol TCP -LocalPort $Port `
    -RemoteAddress $AllowRemote -Action Allow -Profile Any | Out-Null
New-NetFirewallRule -DisplayName $BlockName -Direction Inbound -Protocol TCP -LocalPort $Port `
    -RemoteAddress $BlockRemote -Action Block -Profile Any | Out-Null
Write-Host "Applied. Check from the desktop over Tailscale: GET http://100.94.189.77:$Port/api/health" -ForegroundColor Green
Write-Host ""
Show-Rules
