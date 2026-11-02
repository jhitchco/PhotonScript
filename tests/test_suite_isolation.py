"""2026-11-02: the suite never sees the machine it runs on (tests/conftest.py).

The scope PC ran 112 failures against the desktop's handful: its .env, its
~/.photonscript (the live service's data dir, which tests also wrote into:
cover_gate.jsonl, runs/*_events.jsonl) and the C:\\astro\\PhotonScript\\deploy
scripts the config points at all leaked into the tests."""
import os
from pathlib import Path

from photonscript.shared import envfile
from photonscript.shared.config import PhotonScriptConfig

from tests import conftest


def test_no_env_file_and_no_shell_ps_vars(tmp_path, monkeypatch):
    assert PhotonScriptConfig.model_config["env_file"] is None
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("PS_PIGGYBACK_ENABLED=true\n", encoding="utf-8")
    assert PhotonScriptConfig().piggyback_enabled is False


def test_home_and_data_dir_are_throwaway():
    cfg = PhotonScriptConfig()
    real = Path(os.environ.get("HOMEDRIVE", "") + os.environ.get("HOMEPATH", ""))
    assert Path.home() != real
    assert Path(cfg.data_dir) != real / ".photonscript"
    assert Path(cfg.data_dir).parent == Path.home()
    assert Path(cfg.db_path).parent == Path(cfg.data_dir)
    assert Path(PhotonScriptConfig.model_fields["data_dir"].default) != real / ".photonscript"


def test_machine_paths_point_at_nothing():
    cfg = PhotonScriptConfig()
    assert conftest._MACHINE_PATH_FIELDS          # reentry_gate_script, ...
    for name in conftest._MACHINE_PATH_FIELDS:
        v = str(getattr(cfg, name))
        assert not v.lower().startswith(r"c:\astro") and not Path(v).exists(), name


def test_env_writers_aim_at_a_scratch_file():
    assert envfile.env_path().parent != Path.cwd()
