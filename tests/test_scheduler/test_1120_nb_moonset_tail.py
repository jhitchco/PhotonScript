"""2026-11-20: an 85% moon up at dusk that sets about 2.2 h before dawn.
night_moon tags the night "NB" (illum >= 45, moon-free < 2.5 h), so
_fit_by_moon("NB") emptied NGC 1514 (LRGB only) and the plan skipped it
("broadband deferred by the moon"), while the campaign gave it 1.7 h in the
moonless tail and the shared rule (moon.broadband_deferred) says broadband
is not deferred when the moon sets before dawn. The RC16 sat idle after
moonset.

Fix: the planner fits an "NB" night as "NB+OIII" when the shared rule does
not defer broadband; broadband-only targets then get windows from moonset.
A moon up all night still defers; a "BB" night is unchanged.
"""
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from photonscript.scheduler import moon
from photonscript.scheduler import nina_sequence_json as nsj
from photonscript.scheduler import target_planner as tp
from photonscript.shared.astronomy import get_twilight_times
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import ImagingProject

FIX = Path(__file__).parent / "fixtures" / "deep_night" / "projects2_2026-10-14.json"
PRECONFIG = datetime(2026, 10, 16, 20, 0)      # 14:00 MDT, before dusk
DAWN = datetime(2026, 10, 17, 12, 0)           # astro dawn about 12:00Z
MOONSET = DAWN - timedelta(hours=2, minutes=12)   # about 2.2 h moonless tail
GOALS = ["Andromeda Galaxy", "Heart Nebula", "NGC 604"]   # all RC16 LRGB only


def _cfg(tmp_path, **kw):
    return PhotonScriptConfig(_env_file=None, data_dir=tmp_path / "data",
                              image_watch_dir=str(tmp_path / "fits"),
                              nina_logs_dir=str(tmp_path / "logs"), **kw)


def _goals(names=GOALS):
    return [ImagingProject(**p) for p in json.loads(FIX.read_text())
            if p["target"]["name"] in names]


def _nb_goal():
    """Crescent Nebula as an active narrowband-only goal (lower priority)."""
    p = next(p for p in json.loads(FIX.read_text())
             if p["target"]["name"] == "Crescent Nebula")
    p = {**p, "active": True, "priority": 10,
         "exposure_plans": [e for e in p["exposure_plans"]
                            if e["filter_type"] in ("Ha", "OIII", "SII")]}
    return ImagingProject(**p)


def _mw(cfg, **kw):
    tw = get_twilight_times(cfg.get_observatory(), datetime(2026, 10, 16))
    mw = {"available": True, "down_at_dusk": False, "illum_pct": 85,
          "alt_at_dusk_deg": 40.0, "dusk_utc": tw["astro_dark_start"],
          "dawn_utc": tw["astro_dark_end"], "now_utc": PRECONFIG,
          "rise_local_hh": None, "rise_local_mm": None, "rise_utc": None,
          "set_utc": MOONSET, "set_local": "03:48"}
    mw.update(kw)
    return mw


def _night(monkeypatch, cfg, mw, tag):
    monkeypatch.setattr(moon, "moon_window_tonight",
                        lambda config, now=None: dict(mw))
    monkeypatch.setattr(nsj, "_moon_window", lambda: dict(mw))
    monkeypatch.setattr(moon, "night_moon", lambda *a, **k: {
        "illum_pct": mw["illum_pct"], "moon_free_h": 2.2, "tag": tag})


@pytest.fixture(autouse=True)
def _clear_cache():
    moon._cache.clear()
    yield
    moon._cache.clear()


# --- the fit tag ---------------------------------------------------------------

def test_fit_tag_follows_the_shared_rule(tmp_path):
    mw = _mw(_cfg(tmp_path))
    assert tp.fit_moon_tag("NB", mw) == "NB+OIII"
    assert tp.fit_moon_tag("NB", {**mw, "set_utc": None,
                                  "set_local": None}) == "NB"
    assert tp.fit_moon_tag("NB", {"available": False}) == "NB"
    assert tp.fit_moon_tag("NB", {}) == "NB"
    for tag in ("BB", "NB+OIII", None):
        assert tp.fit_moon_tag(tag, mw) == tag


# --- the planner ---------------------------------------------------------------

def test_nb_night_moon_sets_before_dawn_keeps_broadband_from_moonset(
        tmp_path, monkeypatch, caplog):
    cfg = _cfg(tmp_path)
    _night(monkeypatch, cfg, _mw(cfg), "NB")
    with caplog.at_level(logging.INFO, "photonscript.scheduler.target_planner"):
        ts = tp.plan_night_sequence(_goals(), cfg, PRECONFIG)
    assert "fitting as NB+OIII" in caplog.text
    assert ts, caplog.text
    assert all(t.exposures for t in ts)
    assert all(t.window_start_utc >= MOONSET for t in ts)
    assert min(t.window_start_utc for t in ts) == MOONSET


def test_narrowband_keeps_the_moonlit_part(tmp_path, monkeypatch, caplog):
    """Higher-priority broadband-only goals book only the moonless tail,
    so the narrowband goal still plans and takes the moonlit start."""
    cfg = _cfg(tmp_path)
    mw = _mw(cfg)
    _night(monkeypatch, cfg, mw, "NB")
    with caplog.at_level(logging.INFO, "photonscript.scheduler.target_planner"):
        bb = _goals()
        for g in bb:
            for e in g.exposure_plans:
                e.count = e.acquired + 500     # owes far more than the night
        ts = tp.plan_night_sequence(bb + [_nb_goal()], cfg, PRECONFIG)
    by = {t.name: t for t in ts}
    assert "Crescent Nebula" in by, caplog.text
    assert by["Crescent Nebula"].window_start_utc < MOONSET
    assert all(t.window_start_utc >= MOONSET for n, t in by.items()
               if n != "Crescent Nebula")
    assert all(e.filter_type.value in ("Ha", "OIII", "SII")
               for e in by["Crescent Nebula"].exposures)


def test_broadband_moonless_seconds(tmp_path):
    mw = _mw(_cfg(tmp_path))
    dusk, dawn = mw["dusk_utc"], mw["dawn_utc"]
    assert tp.broadband_moonless_s(mw, dusk, dawn) == pytest.approx(
        (dawn - MOONSET).total_seconds())
    assert tp.broadband_moonless_s({**mw, "set_utc": None}, dusk, dawn) is None
    rise = dusk + timedelta(hours=1)
    down = {**mw, "down_at_dusk": True, "set_utc": None, "rise_utc": rise}
    assert tp.broadband_moonless_s(down, dusk, dawn) == pytest.approx(3600)


def test_nb_night_moon_up_all_night_still_skips_broadband(
        tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    info = {}
    _night(monkeypatch, cfg, _mw(cfg, set_utc=None, set_local=None), "NB")
    ts = tp.plan_night_sequence(_goals(), cfg, PRECONFIG, info=info)
    assert not any(t.name in GOALS for t in ts)


def test_bb_night_unchanged(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    mw = _mw(cfg, illum_pct=10, down_at_dusk=True, set_utc=None,
             set_local=None, alt_at_dusk_deg=-20.0)
    _night(monkeypatch, cfg, mw, "BB")
    calls = []
    real = tp._fit_by_moon
    monkeypatch.setattr(tp, "_fit_by_moon",
                        lambda e, s, tag: calls.append(tag) or real(e, s, tag))
    ts = tp.plan_night_sequence(_goals(), cfg, PRECONFIG)
    assert sorted(t.name for t in ts) == sorted(GOALS)
    assert calls and set(calls) == {"BB"}
