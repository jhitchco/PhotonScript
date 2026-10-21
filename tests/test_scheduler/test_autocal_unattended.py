from photonscript.telescope_agent import tpoint_autocal_live as tal
from photonscript.scheduler import remote_cli as rc


def test_unattended_says_yes_only_to_the_backup_prompt():
    said = []
    ask = tal.unattended_ask(said.append)
    assert ask("  Backup done, TPoint count noted, camera setup checked? Type yes to go on: ") == "yes"
    assert ask("  New sample there? Type yes to go on: ") == "no"
    assert ask("  Try the next point? Type yes to go on: ") == "no"
    assert all("[unattended:" in s for s in said)


def test_unattended_is_allowed_remotely():
    allow = rc.load_allowlist()
    assert "--unattended" in allow.commands["tpoint-autocal"].flags
    assert allow.commands["tpoint-autocal"].write_flags == ("--go",)
