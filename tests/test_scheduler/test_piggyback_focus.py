"""Self-seeding autofocus for the OSC piggyback rig."""

import json

import pytest

from photonscript.shared.config import PhotonScriptConfig
from photonscript.scheduler import piggyback_focus as pf


def _cfg(tmp_path, **kw):
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path), **kw)


def _fits_with_focus(path, focpos, foctemp):
    """Minimal FITS primary header carrying FOCPOS/FOCTEMP (2880-byte block)."""
    def card(k, v):
        return f"{k:<8}= {v:>20} / x".ljust(80)[:80]
    cards = [card("SIMPLE", "T"), card("FOCPOS", focpos), card("FOCTEMP", foctemp)]
    block = ("".join(cards) + "END".ljust(80))
    block = block.ljust(2880)
    path.write_bytes(block.encode("latin-1"))
    return str(path)


def _subs(monkeypatch, subs):
    import photonscript.scheduler.runs as runs
    monkeypatch.setattr(runs, "_load_subs", lambda config, date: subs)


def test_harvest_records_median_of_sharp_osc_subs(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    p1 = _fits_with_focus(tmp_path / "a.fits", 5180, 11.0)
    p2 = _fits_with_focus(tmp_path / "b.fits", 5200, 11.2)
    p3 = _fits_with_focus(tmp_path / "c.fits", 5220, 11.1)
    subs = [
        # sharp OSC subs -> contribute
        {"rig": "piggyback", "passed_qa": True, "hfr": 2.1, "abs_path": p1},
        {"rig": "piggyback", "passed_qa": True, "hfr": 2.4, "abs_path": p2},
        {"rig": "piggyback", "passed_qa": True, "hfr": 2.0, "abs_path": p3},
        # soft OSC sub -> excluded by HFR
        {"rig": "piggyback", "passed_qa": True, "hfr": 9.0, "abs_path": p1},
        # RC16 sub -> wrong rig, excluded
        {"rig": "rc16", "passed_qa": True, "hfr": 2.0, "abs_path": p2},
    ]
    _subs(monkeypatch, subs)

    assert pf.harvest_piggyback_night(cfg, "2026-09-21") == 1
    store = json.loads((tmp_path / "piggyback_focus_seeds.json").read_text())
    assert len(store) == 1
    assert store[0]["focpos"] == 5200        # median of 5180/5200/5220
    assert store[0]["n"] == 3                # the three sharp OSC frames


def test_harvest_is_idempotent_per_night(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    p = _fits_with_focus(tmp_path / "a.fits", 5000, 10.0)
    _subs(monkeypatch, [{"rig": "piggyback", "passed_qa": True,
                         "hfr": 2.0, "abs_path": p}])
    assert pf.harvest_piggyback_night(cfg, "2026-09-21") == 1
    assert pf.harvest_piggyback_night(cfg, "2026-09-21") == 0  # no duplicate
    store = json.loads((tmp_path / "piggyback_focus_seeds.json").read_text())
    assert len(store) == 1


def test_seed_falls_back_to_static_when_no_history(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, piggyback_focus_seed=4444)
    _subs(monkeypatch, [])  # nothing to harvest
    assert pf.piggyback_seed_for(cfg) == 4444


def test_seed_disabled_returns_zero(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, piggyback_focus_seed=0)  # explicitly disabled, no history
    _subs(monkeypatch, [])
    assert pf.piggyback_seed_for(cfg) == 0


def test_seed_uses_harvested_median_over_static(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, piggyback_focus_seed=4444)
    p1 = _fits_with_focus(tmp_path / "a.fits", 5200, 11.0)
    _subs(monkeypatch, [{"rig": "piggyback", "passed_qa": True,
                         "hfr": 2.0, "abs_path": p1}])
    # seed_for harvests today then seeds from the store, not the static fallback
    assert pf.piggyback_seed_for(cfg) == 5200


def test_clamp_applies_when_range_configured(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, piggyback_focpos_min=5000, piggyback_focpos_max=5100)
    p1 = _fits_with_focus(tmp_path / "a.fits", 5200, 11.0)  # above the max
    _subs(monkeypatch, [{"rig": "piggyback", "passed_qa": True,
                         "hfr": 2.0, "abs_path": p1}])
    assert pf.piggyback_seed_for(cfg) == 5100  # clamped to the OSC EAF ceiling
