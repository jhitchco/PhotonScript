"""Scheduler tmp + replace writers go through shared/fileops.replace_retry,
so a brief Windows PermissionError (virus scanner / indexer holding the
fresh file, WinError 5) is ridden out instead of losing the write."""
import json
import os
import re
from pathlib import Path

import pytest

from photonscript.scheduler import focus_model
from photonscript.scheduler import night_pilot as npil
from photonscript.shared import fileops
from photonscript.shared.config import PhotonScriptConfig

ROOT = Path(__file__).resolve().parents[2]


def _cfg(tmp_path):
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path),
                              image_watch_dir=str(tmp_path / "fits"))


@pytest.fixture
def held(monkeypatch):
    """os.replace refused twice (a reader holds the file), then works."""
    real, tries = os.replace, []

    def flaky(src, dst):
        tries.append(Path(dst).name)
        if len(tries) <= 2:
            raise PermissionError(5, "Access is denied")
        return real(src, dst)
    monkeypatch.setattr(os, "replace", flaky)
    monkeypatch.setattr(fileops.time, "sleep", lambda s: None)
    return tries


def test_night_pilot_state_survives_a_held_replace(tmp_path, held):
    cfg = _cfg(tmp_path)
    npil.save_state(cfg, {"night": "2026-10-12", "actions": [{"kind": "x"}]})
    assert len(held) == 3
    st = npil.load_state(cfg, "2026-10-12")
    assert st["actions"] == [{"kind": "x"}]
    assert not list(npil.state_dir(cfg).glob("*.tmp"))


def test_focus_model_json_survives_a_held_replace(tmp_path, held):
    p = tmp_path / "focus" / "model.json"
    focus_model._write_json(p, {"slope": -3.5})
    assert len(held) == 3
    assert json.loads(p.read_text(encoding="utf-8")) == {"slope": -3.5}
    assert not (p.parent / "model.json.tmp").exists()


def test_a_lasting_refusal_still_raises_and_keeps_the_old_file(tmp_path, monkeypatch):
    p = tmp_path / "model.json"
    p.write_text('{"old": 1}', encoding="utf-8")

    def locked(src, dst):
        raise PermissionError(5, "Access is denied")
    monkeypatch.setattr(os, "replace", locked)
    monkeypatch.setattr(fileops.time, "sleep", lambda s: None)
    with pytest.raises(PermissionError):
        focus_model._write_json(p, {"new": 2})
    assert json.loads(p.read_text(encoding="utf-8")) == {"old": 1}


def test_no_bare_tmp_replace_left_in_the_scheduler():
    """A new tmp writer should use replace_retry too (file moves of FITS
    between folders are not tmp writers and are left alone)."""
    bad = []
    pat = re.compile(r"\btmp\w*\.replace\(|os\.replace\(\s*tmp")
    for f in sorted((ROOT / "photonscript" / "scheduler").rglob("*.py")):
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if pat.search(line):
                bad.append(f"{f.name}:{n}")
    assert not bad, bad


def test_arm_memory_survives_a_held_replace(tmp_path, held):
    from photonscript.scheduler import arm_memory
    cfg = _cfg(tmp_path)
    arm_memory.record_manual_arm(cfg, "guided")
    assert len(held) == 3
    assert arm_memory.last_manual_arm(cfg)["guiding"] == "guided"
    assert not list(tmp_path.glob("*.tmp"))
