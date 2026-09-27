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


# --- PS-58: staged update + rollback ---------------------------------------------

def test_wrapper_updates_through_self_update_and_can_roll_back():
    w = _read("run-photonscript.ps1")
    assert '$env:PS_WRAPPER_ROLLBACK = "1"' in w
    assert "& $Exe self-update" in w
    assert "pull --ff-only" not in w           # no unchecked pulls any more
    i_sup = w.index("& $Exe supervise --mode $Mode")
    i_reset = w.index("git -C $Repo reset --hard $prev")
    i_done = w.index("& $Exe rollback-done --reason $why")
    assert i_sup < i_reset < i_done
    assert "$rc -eq 43" in w and "$skipUpdate = $true" in w


def test_deploy_reports_scope_rollback_and_refusal():
    s = _read("deploy.ps1")
    assert "[switch]$AllowArmed" in s and "allow_armed=true" in s
    assert '$u.status -eq "rolled_back" -and $u.bad_sha -eq $head' in s
    assert '$u.status -eq "rejected" -and $u.target -eq $head' in s
    assert "ConvertFrom-Json).detail" in s    # 409 says why
