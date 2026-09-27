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


# --- PS-56: deploy.ps1 ships only committed work --------------------------------

def test_deploy_refuses_dirty_tree_before_anything_else():
    s = _read("deploy.ps1")
    i_status = s.index("git -C $repo status --porcelain")
    i_refuse = s.index("if (-not $IncludeWorkingTree)")
    i_pull = s.index("git -C $repo pull --rebase origin main")
    i_tests = s.index("python -m pytest -q")
    i_push = s.index("git -C $repo push origin HEAD:main")
    i_post = s.index('"$Scope/api/update"')
    assert i_status < i_refuse < i_pull < i_tests < i_push < i_post


def test_deploy_add_all_only_behind_explicit_switch_and_confirmation():
    s = _read("deploy.ps1")
    assert s.count("git -C $repo add -A") == 1
    block = s[s.index("if (-not $IncludeWorkingTree)"):s.index("git -C $repo pull --rebase")]
    assert "add -A" in block and "Read-Host" in block
    assert block.index("Read-Host") < block.index("add -A")
    assert '$answer -ne "yes"' in block
    assert "--autostash" not in s


def test_deploy_requires_main_and_verifies_via_health():
    s = _read("deploy.ps1")
    assert '$branch -ne "main"' in s
    assert "origin/main..HEAD" in s
    assert '"$Scope/api/health"' in s and "$h.commit -eq $head" in s


def test_deploy_include_working_tree_refuses_unmerged_paths():
    s = _read("deploy.ps1")
    block = s[s.index("if (-not $IncludeWorkingTree)"):s.index("git -C $repo add -A")]
    assert "^(DD|AU|UD|UA|DU|AA|UU) " in block
