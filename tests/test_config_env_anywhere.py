"""PS-86: the repo .env is read whatever the current directory is."""
import importlib

from photonscript.shared import config as cfgmod


def test_repo_env_read_from_other_cwd(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text("PS_QUALITY_ECCENTRICITY_MAX=0.6\n")
    elsewhere = tmp_path / "home"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.delenv("PS_QUALITY_ECCENTRICITY_MAX", raising=False)
    files = (str(repo / ".env"), ".env")
    c = cfgmod.PhotonScriptConfig(_env_file=files)
    assert c.quality_eccentricity_max == 0.6


def test_cwd_env_overrides_repo_env(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text("PS_QUALITY_ECCENTRICITY_MAX=0.6\n")
    here = tmp_path / "here"
    here.mkdir()
    (here / ".env").write_text("PS_QUALITY_ECCENTRICITY_MAX=0.5\n")
    monkeypatch.chdir(here)
    monkeypatch.delenv("PS_QUALITY_ECCENTRICITY_MAX", raising=False)
    c = cfgmod.PhotonScriptConfig(_env_file=(str(repo / ".env"), ".env"))
    assert c.quality_eccentricity_max == 0.5


def test_default_env_file_includes_repo_root():
    files = cfgmod.PhotonScriptConfig.model_config["env_file"]
    assert str(cfgmod.REPO_ROOT / ".env") in files and ".env" in files
