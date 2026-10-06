"""Focus-seed harvest gate per rig, in arcsec (PS-144 follow-up).

harvest_night() gated every sub at HFR <= 4.0 px. At the RC16's 0.236"/px
that is 0.94", so in-focus RC16 subs (7-8 px, 1.7-1.9") never passed and no
RC16 record was harvested. The gate is now HFR px x the rig's pixel scale
against focus_harvest_max_hfr_arcsec (RC16); other rigs keep 4.0 px.
"""
import json

import pytest

from photonscript.scheduler import focus_seeds as fs
from photonscript.shared.config import PhotonScriptConfig


def _cfg(tmp_path, **kw):
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path), **kw)


def _fits(path, focpos, foctemp):
    def card(k, v):
        return f"{k:<8}= {v:>20} / x".ljust(80)[:80]
    cards = [card("SIMPLE", "T"), card("FOCPOS", focpos), card("FOCTEMP", foctemp)]
    path.write_bytes(("".join(cards) + "END".ljust(80)).ljust(2880).encode("latin-1"))
    return str(path)


def _subs(monkeypatch, subs):
    import photonscript.scheduler.runs as runs
    monkeypatch.setattr(runs, "_load_subs", lambda config, date: subs)


def _harvested(tmp_path):
    p = tmp_path / "focus_seeds.json"
    return json.loads(p.read_text()) if p.exists() else []


def test_default_and_limits(tmp_path):
    cfg = _cfg(tmp_path)
    assert cfg.focus_harvest_max_hfr_arcsec == 2.2
    lim, scale = fs.harvest_limit_arcsec(cfg, "rc16")
    assert (lim, scale) == (2.2, 0.236)
    lim, scale = fs.harvest_limit_arcsec(cfg, "piggyback")
    assert scale == 1.29 and lim == pytest.approx(4.0 * 1.29)
    assert fs.harvest_limit_arcsec(cfg, None)[1] == 0.236      # no rig = RC16


def test_rc16_in_focus_subs_are_harvested(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    a = _fits(tmp_path / "a.fits", 5680, 12.0)
    b = _fits(tmp_path / "b.fits", 5690, 12.2)
    c = _fits(tmp_path / "c.fits", 5700, 12.1)
    _subs(monkeypatch, [
        {"rig": "rc16", "filter": "L", "passed_qa": True, "hfr": 7.2, "abs_path": a},
        {"rig": "rc16", "filter": "L", "passed_qa": True, "hfr": 8.0, "abs_path": b},
        {"rig": "rc16", "filter": "L", "passed_qa": True, "hfr": 7.6, "abs_path": c},
        {"rig": "rc16", "filter": "L", "passed_qa": False, "hfr": 7.0, "abs_path": a},
    ])
    assert fs.harvest_night(cfg, "2026-10-05") == 1
    rec = _harvested(tmp_path)
    assert len(rec) == 1 and rec[0]["filter"] == "L"
    assert rec[0]["focpos"] == 5690 and rec[0]["source"] == "harvest"


def test_rc16_soft_night_is_rejected(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    a = _fits(tmp_path / "a.fits", 5500, 12.0)
    # the 2026-10-05 Ha subs: 10-12 px = 2.4-2.8", out of focus
    _subs(monkeypatch, [
        {"rig": "rc16", "filter": "Ha", "passed_qa": True, "hfr": h, "abs_path": a}
        for h in (10.0, 11.0, 12.0)])
    assert fs.harvest_night(cfg, "2026-10-05") == 0
    assert _harvested(tmp_path) == []
    # a tighter configured gate rejects a borderline in-focus night too
    _subs(monkeypatch, [{"rig": "rc16", "filter": "L", "passed_qa": True,
                         "hfr": 8.0, "abs_path": a}])
    assert fs.harvest_night(_cfg(tmp_path, focus_harvest_max_hfr_arcsec=1.8),
                            "2026-10-05") == 0


def test_piggyback_gate_unchanged(tmp_path, monkeypatch):
    """Piggy subs keep the old 4.0 px gate (5.16" at 1.29"/px)."""
    cfg = _cfg(tmp_path)
    a = _fits(tmp_path / "a.fits", 11000, 12.0)
    _subs(monkeypatch, [
        {"rig": "piggyback", "filter": "?", "passed_qa": True, "hfr": 3.9,
         "abs_path": a},
        {"rig": "piggyback", "filter": "OSC", "passed_qa": True, "hfr": 4.1,
         "abs_path": a}])
    assert fs.harvest_night(cfg, "2026-10-05") == 1
    assert [r["filter"] for r in _harvested(tmp_path)] == ["?"]


def test_explicit_pixel_gate_still_works(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    a = _fits(tmp_path / "a.fits", 5690, 12.0)
    _subs(monkeypatch, [{"rig": "rc16", "filter": "L", "passed_qa": True,
                         "hfr": 7.5, "abs_path": a}])
    assert fs.harvest_night(cfg, "2026-10-05", max_hfr_px=4.0) == 0
    assert fs.harvest_night(cfg, "2026-10-05", max_hfr_px=8.0) == 1


def test_system_page_field():
    from photonscript.scheduler.app import _CONFIG_FIELDS
    f = {x[1]: x for x in _CONFIG_FIELDS}["PS_FOCUS_HARVEST_MAX_HFR_ARCSEC"]
    assert f[0] == "focus_harvest_max_hfr_arcsec" and f[4] == "float"
