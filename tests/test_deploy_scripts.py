"""Guards for the PowerShell deploy scripts (no PowerShell in CI, so these
check the text for the behaviors the tickets depend on)."""

from __future__ import annotations

from pathlib import Path

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"


def _read(name: str) -> str:
    return (DEPLOY / name).read_bytes().decode("ascii")   # also: pure ASCII


def test_installer_defaults_to_interactive_logon():
    s = _read("install-autostart.ps1")
    assert '[string]$LogonType = "Interactive"' in s
    assert '[ValidateSet("Interactive", "S4U", "Password")]' in s
    assert "-AtLogOn -User $account" in s
    assert "-LogonType $LogonType" in s


def test_installer_tags_the_launcher():
    s = _read("install-autostart.ps1")
    assert '$launcher = "task-$LogonType"' in s
    assert "-Launcher $launcher" in s
    w = _read("run-photonscript.ps1")
    assert '[string]$Launcher = "console"' in w
    assert "$env:PS_LAUNCHER = $Launcher" in w
